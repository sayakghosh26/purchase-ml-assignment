"""Core assignment regression tests."""
import csv
import io
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Customer, Extra, HistoricalOrder, Item, Order, OutboxMessage
from .services import PurchaseError, record_purchase
from . import views


class PurchaseTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_new_purchase_normalizes_and_adds_both_messages(self):
        order = record_purchase("  Ada@Example.COM ", "stool", "ch_12345678", "cushion")
        self.assertFalse(order.replayed)
        self.assertEqual(order.total_cents, 14500)
        self.assertEqual(order.customer.email, "ada@example.com")
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Item.objects.get(sku="stool").remaining, 0)
        self.assertEqual(list(OutboxMessage.objects.values_list("kind", flat=True)),
                         ["account_created", "purchase_recorded"])

    def test_replay_ignores_changed_payload_and_stock(self):
        first = record_purchase("one@example.com", "lamp", "ch_12345678")
        replay = record_purchase("other@example.com", "stool", "ch_12345678", "cushion")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(Item.objects.get(sku="lamp").remaining, 1)
        self.assertEqual(Item.objects.get(sku="stool").remaining, 1)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(OutboxMessage.objects.count(), 2)

    def test_sold_out_creates_no_new_customer(self):
        record_purchase("first@example.com", "stool", "ch_12345678")
        before = (Customer.objects.count(), Order.objects.count(), OutboxMessage.objects.count())
        with self.assertRaises(PurchaseError) as ctx:
            record_purchase("second@example.com", "stool", "ch_87654321")
        self.assertEqual(ctx.exception.code, "sold_out")
        self.assertEqual(before, (Customer.objects.count(), Order.objects.count(), OutboxMessage.objects.count()))

    def test_duplicate_is_case_insensitive_and_does_not_change_state(self):
        record_purchase("Ada@Example.com", "lamp", "ch_12345678")
        before = (Item.objects.get(sku="lamp").remaining, Customer.objects.count(),
                  Order.objects.count(), OutboxMessage.objects.count())
        with self.assertRaises(PurchaseError) as ctx:
            record_purchase(" ADA@example.COM ", "lamp", "ch_87654321")
        self.assertEqual(ctx.exception.code, "duplicate_purchase")
        self.assertEqual(before, (Item.objects.get(sku="lamp").remaining, Customer.objects.count(),
                                  Order.objects.count(), OutboxMessage.objects.count()))

    def test_invalid_extra_and_invalid_id_leave_state_unchanged(self):
        for charge, extra, error in [("ch_12345678", "cushion", "extra_not_allowed"),
                                     ("wrong", None, "invalid_charge_id")]:
            with self.subTest(error=error):
                with self.assertRaises(PurchaseError) as ctx:
                    record_purchase("new@example.com", "lamp", charge, extra)
                self.assertEqual(ctx.exception.code, error)
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OutboxMessage.objects.count(), 0)
        self.assertEqual(Item.objects.get(sku="lamp").remaining, 2)

    def test_http_contract(self):
        data = {"email": "Ada@Example.COM", "sku": "lamp", "externalChargeId": "ch_12345678"}
        response = self.client.post("/api/purchases/", data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data["replayed"])
        self.assertEqual(response.data["order"]["email"], "ada@example.com")
        response = self.client.post("/api/purchases/", {**data, "sku": "stool"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["order"]["sku"], "lamp")
        self.assertEqual(self.client.get("/api/outbox").status_code, 200)
        self.assertEqual(self.client.get("/api/catalog/lamp").data["remaining"], 1)


class HistoricalImportTests(TestCase):
    def setUp(self):
        self.csv = Path(settings.BASE_DIR) / "order_history.csv"

    def test_import_twice_and_may_stats(self):
        if not self.csv.is_file():
            self.skipTest("Assignment CSV is not available")
        output = io.StringIO()
        call_command("import_history", str(self.csv), stdout=output)
        first = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(first["created"], 112)
        self.assertEqual(first["skippedExisting"], 0)
        self.assertEqual(first["rejected"], {
            "invalid_email": 2, "unknown_sku": 1, "extra_not_allowed": 1,
            "bad_total": 1, "bad_date": 2, "bad_returned": 1,
            "duplicate_order_id": 2,
        })
        self.assertEqual(HistoricalOrder.objects.count(), 112)

        output = io.StringIO()
        call_command("import_history", str(self.csv), stdout=output)
        again = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skippedExisting"], 112)
        self.assertEqual(again["rejected"], first["rejected"])
        self.assertEqual(HistoricalOrder.objects.count(), 112)

        response = self.client.get("/api/history/stats?from=2026-05-01&to=2026-05-31")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "lamp": {"orders": 43, "revenueCents": 344000, "returnRate": 0.28},
            "stool": {"orders": 20, "revenueCents": 265000, "returnRate": 0.3},
        })
        listing = self.client.get("/api/history/?page=1")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(set(listing.json()), {"count", "page", "results"})
        self.assertEqual(listing.json()["count"], 112)
        self.assertEqual(len(listing.json()["results"]), 10)

    def test_validation_before_existing_skip(self):
        HistoricalOrder.objects.create(
            order_id="X1", email="first@example.com", sku="lamp", extra_code=None,
            channel="web", created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            total_cents=8000, returned=False,
        )
        content = ("order_id,email,sku,extra_code,channel,created_at,total_cents,returned\n"
                   "X1,invalid-email,lamp,,web,2026-05-01T00:00:00Z,8000,0\n")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", encoding="utf-8", delete=False) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        try:
            output = io.StringIO()
            call_command("import_history", str(temp_path), stdout=output)
            result = json.loads(output.getvalue())
            self.assertEqual(result["rejected"], {"invalid_email": 1})
            self.assertEqual(result["skippedExisting"], 0)
        finally:
            temp_path.unlink()


class PredictionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.csv = Path(settings.BASE_DIR) / "order_history.csv"

    def test_prediction_before_training_and_after_training(self):
        if not self.csv.is_file():
            self.skipTest("Assignment CSV is not available")
        payload = {"email": "new@example.com", "sku": "lamp", "extraCode": None,
                   "channel": "app", "createdAt": "2026-07-01T22:15:00Z"}
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            with override_settings(BASE_DIR=temp_dir):
                views._MODEL = None
                views._METRICS = None
                views._MODEL_PATH = None
                before = self.client.post("/api/predictions/return-risk/", payload, format="json")
                self.assertEqual(before.status_code, 503)
                self.assertEqual(before.json()["error"], "model_not_trained")
                call_command("import_history", str(self.csv), stdout=io.StringIO())
                call_command("train_return_model", stdout=io.StringIO())
                after = self.client.post("/api/predictions/return-risk/", payload, format="json")
                self.assertEqual(after.status_code, 200)
                self.assertGreaterEqual(after.json()["probability"], 0)
                self.assertLessEqual(after.json()["probability"], 1)
                self.assertTrue(after.json()["modelVersion"])
                self.assertEqual(self.client.post("/api/predictions/return-risk/",
                                                  {**payload, "sku": "unknown"}, format="json").status_code, 400)
                views._MODEL = None
                views._METRICS = None
                views._MODEL_PATH = None
