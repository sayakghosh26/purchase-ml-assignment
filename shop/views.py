"""HTTP adapters for the purchase, history and return-risk services."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.utils.dateparse import parse_date, parse_datetime
from joblib import load
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import HistoricalOrder, Item, Order, OutboxMessage
from .services import PurchaseError, record_purchase

_MODEL = None
_METRICS = None
_MODEL_PATH = None
FEATURES = ["is_stool", "has_extra", "channel_app", "channel_web", "hour", "prior_orders"]


def get_ml_model():
    """Load artifacts once per worker (cache only successful loads)."""
    global _MODEL, _METRICS, _MODEL_PATH
    directory = Path(settings.BASE_DIR) / "models"
    model_path = directory / "return_model.joblib"
    metrics_path = directory / "metrics.json"
    if _MODEL is not None and _MODEL_PATH == model_path:
        return _MODEL, _METRICS
    if not model_path.is_file() or not metrics_path.is_file():
        return None, None
    model = load(model_path)  # Load only locally trained, trusted joblib artifacts.
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    _MODEL, _METRICS, _MODEL_PATH = model, metrics, model_path
    return model, metrics


def utc_date_parameter(request, name):
    value = request.query_params.get(name)
    if value is None:
        return None
    if len(value) != 10 or parse_date(value) is None:
        raise ValueError(f"invalid {name} date")
    return parse_date(value)


def filter_history_dates(queryset, request):
    # Explicit UTC boundaries avoid the active timezone changing calendar dates.
    start = utc_date_parameter(request, "from")
    end = utc_date_parameter(request, "to")
    if start is not None:
        queryset = queryset.filter(created_at__gte=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc))
    if end is not None:
        next_day = end + timedelta(days=1)
        queryset = queryset.filter(created_at__lt=datetime.combine(next_day, datetime.min.time(), tzinfo=timezone.utc))
    return queryset


class PurchaseAPIView(APIView):
    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"error": "missing_fields"}, status=400)
        try:
            order = record_purchase(
                email=data.get("email"), sku=data.get("sku"),
                external_charge_id=data.get("externalChargeId"),
                extra_code=data.get("extraCode"),
            )
        except PurchaseError as exc:
            code = 409 if exc.code in {"sold_out", "duplicate_purchase"} else 400
            return Response({"error": exc.code}, status=code)
        # A different concurrent request may have committed the same charge ID
        # or customer/item pair. Database constraints remain the final guard.
        except IntegrityError:
            charge = data.get("externalChargeId")
            if isinstance(charge, str):
                existing = (Order.objects.select_related("customer", "item", "extra")
                            .filter(external_charge_id=charge).first())
                if existing is not None:
                    order = existing
                    order.replayed = True
                else:
                    return Response({"error": "duplicate_purchase"}, status=409)
            else:
                return Response({"error": "duplicate_purchase"}, status=409)

        return Response({
            "replayed": order.replayed,
            "order": {
                "id": order.pk,
                "email": order.customer.email,
                "sku": order.item_id,
                "extraCode": order.extra.code if order.extra_id else None,
                "externalChargeId": order.external_charge_id,
                "totalCents": order.total_cents,
            },
        }, status=200 if order.replayed else 201)


class CatalogAPIView(APIView):
    def get(self, request, sku):
        item = Item.objects.filter(sku=sku).first()
        if item is None:
            return Response({"error": "unknown_item"}, status=404)
        return Response({
            "sku": item.sku, "name": item.name,
            "priceCents": item.price_cents, "remaining": item.remaining,
        })


class OutboxAPIView(APIView):
    def get(self, request):
        rows = OutboxMessage.objects.order_by("created_at", "id")
        return Response([{
            "toEmail": row.to_email, "kind": row.kind,
            "externalChargeId": row.external_charge_id,
        } for row in rows])


class HistoryAPIView(APIView):
    def get(self, request):
        qs = HistoricalOrder.objects.all()
        sku = request.query_params.get("sku")
        channel = request.query_params.get("channel")
        if sku:
            qs = qs.filter(sku=sku.strip().lower())
        if channel:
            qs = qs.filter(channel=channel.strip().lower())
        try:
            qs = filter_history_dates(qs, request)
        except (ValueError, OverflowError):
            return Response({"error": "bad_date"}, status=400)
        qs = qs.order_by("created_at", "order_id")
        page = Paginator(qs, 10).get_page(request.query_params.get("page", "1"))
        results = [{
            "orderId": order.order_id,
            "email": order.email,
            "sku": order.sku,
            "extraCode": order.extra_code,
            "channel": order.channel,
            "createdAt": order.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "totalCents": order.total_cents,
            "returned": order.returned,
        } for order in page.object_list]
        return Response({"count": page.paginator.count, "page": page.number, "results": results})


class HistoryStatsAPIView(APIView):
    def get(self, request):
        try:
            qs = filter_history_dates(HistoricalOrder.objects.all(), request)
        except (ValueError, OverflowError):
            return Response({"error": "bad_date"}, status=400)
        stats = (qs.values("sku").annotate(
            orders=Count("id"),
            revenue=Sum("total_cents"),
            returned_count=Count("id", filter=Q(returned=True)),
        ))
        result = {sku: {"orders": 0, "revenueCents": 0, "returnRate": None}
                  for sku in ("lamp", "stool")}
        for item in stats:
            if item["sku"] in result:
                count = item["orders"]
                result[item["sku"]] = {
                    "orders": count,
                    "revenueCents": item["revenue"] or 0,
                    "returnRate": round(item["returned_count"] / count, 2) if count else None,
                }
        return Response(result)


class PredictionAPIView(APIView):
    def post(self, request):
        data = request.data
        if not isinstance(data, dict):
            return Response({"error": "missing_fields"}, status=400)
        sku = data.get("sku")
        if sku not in ("lamp", "stool"):
            return Response({"error": "unknown_item"}, status=400)

        model, metrics = get_ml_model()
        if model is None:
            return Response({"error": "model_not_trained"}, status=503)

        email = data.get("email")
        channel = data.get("channel")
        extra = data.get("extraCode")
        if not isinstance(email, str) or not email.strip():
            return Response({"error": "missing_fields"}, status=400)
        email = email.strip().lower()
        if channel not in ("app", "web", "store"):
            return Response({"error": "bad_channel"}, status=400)
        if extra not in (None, "", "cushion") or (extra == "cushion" and sku != "stool"):
            return Response({"error": "extra_not_allowed"}, status=400)
        value = data.get("createdAt")
        if not isinstance(value, str):
            return Response({"error": "bad_date"}, status=400)
        try:
            stamp = parse_datetime(value)
            if stamp is None or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("timezone required")
            stamp = stamp.astimezone(timezone.utc)
        except (ValueError, OverflowError, TypeError):
            return Response({"error": "bad_date"}, status=400)

        earlier = HistoricalOrder.objects.filter(email=email, created_at__lt=stamp).count()
        vector = pd.DataFrame([[
            int(sku == "stool"), int(bool(extra)),
            int(channel == "app"), int(channel == "web"),
            stamp.hour, earlier,
        ]], columns=FEATURES)
        probability = float(model.predict_proba(vector)[0][1])
        return Response({"probability": round(probability, 3), "modelVersion": metrics["version"]})
