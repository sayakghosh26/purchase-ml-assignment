"""Train the exactly six-feature return-risk classifier."""
import json
import uuid
from datetime import timezone
from pathlib import Path

import pandas as pd
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from joblib import dump
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split

from shop.models import HistoricalOrder

FEATURES = ["is_stool", "has_extra", "channel_app", "channel_web", "hour", "prior_orders"]


class Command(BaseCommand):
    help = "Train and persist a LogisticRegression return-risk model."

    def handle(self, *args, **options):
        # Tie handling: orders with the same email and timestamp do not count
        # each other as prior orders (strict created_at comparison).
        orders = HistoricalOrder.objects.order_by("created_at", "order_id").iterator(chunk_size=2000)
        counts = {}
        prior_timestamp = None
        group = []
        rows = []

        def process_group():
            for o in group:
                stamp = o.created_at.astimezone(timezone.utc)
                rows.append({
                    "is_stool": int(o.sku == "stool"),
                    "has_extra": int(bool(o.extra_code)),
                    "channel_app": int(o.channel == "app"),
                    "channel_web": int(o.channel == "web"),
                    "hour": stamp.hour,
                    "prior_orders": counts.get(o.email, 0),
                    "returned": int(o.returned),
                })
            for o in group:
                counts[o.email] = counts.get(o.email, 0) + 1
            group.clear()

        for order in orders:
            if prior_timestamp is not None and order.created_at != prior_timestamp:
                process_group()
            group.append(order)
            prior_timestamp = order.created_at
        process_group()

        frame = pd.DataFrame(rows)
        if frame.empty or frame["returned"].nunique() < 2:
            raise CommandError("Training requires historical orders from both return classes")
        X, y = frame[FEATURES], frame["returned"]
        if y.value_counts().min() < 2:
            raise CommandError("Need at least two examples of each return class to stratify")

        try:
            X_train, X_holdout, y_train, y_holdout = train_test_split(
                X, y, test_size=0.2, random_state=0, stratify=y,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        model = LogisticRegression(max_iter=1000, random_state=0)
        model.fit(X_train, y_train)
        common_class = y_train.mode().iloc[0]
        metrics = {
            "version": uuid.uuid4().hex,
            "rows": len(frame),
            "trainRows": len(X_train),
            "holdoutAccuracy": round(float(model.score(X_holdout, y_holdout)), 3),
            "baselineAccuracy": round(float((y_holdout == common_class).mean()), 3),
            "features": FEATURES,
        }
        directory = Path(settings.BASE_DIR) / "models"
        directory.mkdir(parents=True, exist_ok=True)
        dump(model, directory / "return_model.joblib")
        (directory / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        self.stdout.write(f"Model trained: {metrics['version']}; holdout={metrics['holdoutAccuracy']}; baseline={metrics['baselineAccuracy']}")
