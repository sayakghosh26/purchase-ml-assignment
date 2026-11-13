"""Stream and validate historical CSV records; write them in batches."""
import csv
import json
import re
from collections import Counter
from datetime import timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from shop.models import HistoricalOrder

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\Z")
BATCH_SIZE = 2000


class Command(BaseCommand):
    help = "Import historical orders from a CSV, rejecting the first failing rule per row."

    def add_arguments(self, parser):
        parser.add_argument("csv_file")

    def handle(self, *args, **options):
        path = Path(options["csv_file"])
        if not path.is_file():
            raise CommandError(f"CSV not found: {path}")

        seen_in_file = set()
        rejected = Counter()
        created = skipped = 0
        pending = []
        pending_ids = set()

        def flush():
            nonlocal created, skipped
            if not pending:
                return
            ids = [o.order_id for o in pending]
            existing = set(HistoricalOrder.objects.filter(order_id__in=ids)
                           .values_list("order_id", flat=True))
            additions = [o for o in pending if o.order_id not in existing]
            skipped += len(pending) - len(additions)
            # Atomic batch; only query once for all IDs, not once per record.
            with transaction.atomic():
                HistoricalOrder.objects.bulk_create(additions, batch_size=BATCH_SIZE)
            created += len(additions)
            pending.clear()
            pending_ids.clear()

        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"order_id", "email", "sku", "extra_code", "channel",
                        "created_at", "total_cents", "returned"}
            if not required.issubset(reader.fieldnames or []):
                raise CommandError("CSV header is missing required columns")

            for row in reader:
                # The duplicate check is first, even when the first occurrence
                # was invalid; the file's earlier occurrence still counts.
                order_id = (row.get("order_id") or "").strip()
                if order_id in seen_in_file:
                    rejected["duplicate_order_id"] += 1
                    continue
                seen_in_file.add(order_id)

                email = (row.get("email") or "").strip().lower()
                if not EMAIL.fullmatch(email):
                    rejected["invalid_email"] += 1
                    continue

                sku = (row.get("sku") or "").strip().lower()
                if sku not in ("lamp", "stool"):
                    rejected["unknown_sku"] += 1
                    continue

                extra = (row.get("extra_code") or "").strip().lower() or None
                if extra and not (sku == "stool" and extra == "cushion"):
                    rejected["extra_not_allowed"] += 1
                    continue

                try:
                    total = int((row.get("total_cents") or "").strip())
                except ValueError:
                    total = None
                expected = 8000 if sku == "lamp" else (14500 if extra else 12000)
                if total != expected:
                    rejected["bad_total"] += 1
                    continue

                try:
                    parsed = parse_datetime((row.get("created_at") or "").strip())
                    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("timezone required")
                    created_at = parsed.astimezone(timezone.utc)
                except (ValueError, OverflowError, TypeError):
                    rejected["bad_date"] += 1
                    continue

                returned_value = (row.get("returned") or "").strip().lower()
                if returned_value not in {"0", "1", "true", "false", "yes", "no"}:
                    rejected["bad_returned"] += 1
                    continue

                channel = (row.get("channel") or "").strip().lower()
                if channel not in {"web", "app", "store"}:
                    rejected["bad_channel"] += 1
                    continue

                pending.append(HistoricalOrder(
                    order_id=order_id, email=email, sku=sku, extra_code=extra,
                    channel=channel, created_at=created_at, total_cents=total,
                    returned=returned_value in {"1", "true", "yes"},
                ))
                pending_ids.add(order_id)
                if len(pending) >= BATCH_SIZE:
                    flush()

        flush()
        self.stdout.write(json.dumps({
            "created": created, "skippedExisting": skipped,
            "rejected": dict(rejected),
        }))
