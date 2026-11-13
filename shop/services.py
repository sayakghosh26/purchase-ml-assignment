"""Purchase business rules. The API is deliberately thin."""
import re
from django.db import IntegrityError, transaction
from django.db.models import F
from .models import Customer, Extra, Item, Order, OutboxMessage

CHARGE_PATTERN = re.compile(r"ch_[A-Za-z0-9]{8,}\Z")


class PurchaseError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@transaction.atomic
def record_purchase(email, sku, external_charge_id, extra_code=None) -> Order:
    # Charge validation precedes all lookups; invalid IDs must never write anything.
    if external_charge_id is None or external_charge_id == "":
        raise PurchaseError("missing_fields")
    if not isinstance(external_charge_id, str) or not CHARGE_PATTERN.fullmatch(external_charge_id):
        raise PurchaseError("invalid_charge_id")

    # Replays take priority over changed email, item, extra and stock.
    existing = (Order.objects.select_related("customer", "item", "extra")
                .filter(external_charge_id=external_charge_id).first())
    if existing is not None:
        existing.replayed = True
        return existing

    if not isinstance(email, str) or not email.strip():
        raise PurchaseError("missing_fields")
    if not isinstance(sku, str) or not sku.strip():
        raise PurchaseError("missing_fields")
    email = email.strip().lower()
    sku = sku.strip()

    # Lock supported on PostgreSQL. SQLite ignores SELECT FOR UPDATE; the
    # conditional F-expression update below also prevents negative inventory.
    item = Item.objects.select_for_update().filter(sku=sku).first()
    if item is None:
        raise PurchaseError("unknown_item")

    extra = None
    if extra_code is not None and extra_code != "":
        if not isinstance(extra_code, str):
            raise PurchaseError("extra_not_allowed")
        extra_code = extra_code.strip()
        if extra_code:
            extra = Extra.objects.filter(item=item, code=extra_code).first()
            if extra is None:
                raise PurchaseError("extra_not_allowed")

    if item.remaining == 0:
        raise PurchaseError("sold_out")

    customer = Customer.objects.filter(email=email).first()
    if customer is not None and Order.objects.filter(customer=customer, item=item).exists():
        raise PurchaseError("duplicate_purchase")

    # Atomic conditional update defends stock even when SELECT FOR UPDATE is ignored.
    updated = Item.objects.filter(sku=sku, remaining__gt=0).update(remaining=F("remaining") - 1)
    if updated != 1:
        raise PurchaseError("sold_out")

    created_customer = customer is None
    if created_customer:
        customer = Customer.objects.create(email=email)

    order = Order.objects.create(
        customer=customer,
        item=item,
        extra=extra,
        external_charge_id=external_charge_id,
        total_cents=item.price_cents + (extra.price_cents if extra else 0),
    )
    if created_customer:
        OutboxMessage.objects.create(
            to_email=email,
            kind=OutboxMessage.Kind.ACCOUNT_CREATED,
            external_charge_id=external_charge_id,
        )
    OutboxMessage.objects.create(
        to_email=email,
        kind=OutboxMessage.Kind.PURCHASE_RECORDED,
        external_charge_id=external_charge_id,
    )
    order.replayed = False
    return order
