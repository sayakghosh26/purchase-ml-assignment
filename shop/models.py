
from django.db import models


class Item(models.Model):
    sku = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=255)
    price_cents = models.PositiveIntegerField()
    remaining = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sku"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(remaining__gte=0),
                name="item_remaining_non_negative",
            ),
        ]

    def __str__(self):
        return self.sku


class Extra(models.Model):
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="extras",
    )
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=255)
    price_cents = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["item", "code"],
                name="unique_extra_per_item",
            ),
        ]

    def __str__(self):
        return f"{self.item_id} - {self.code}"


class Customer(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email


class Order(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    extra = models.ForeignKey(
        Extra,
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    external_charge_id = models.CharField(max_length=100, unique=True)
    total_cents = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer", "item"],
                name="unique_customer_item_order",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return self.external_charge_id


class OutboxMessage(models.Model):

    class Kind(models.TextChoices):
        ACCOUNT_CREATED = "account_created", "Account Created"
        PURCHASE_RECORDED = "purchase_recorded", "Purchase Recorded"

    to_email = models.EmailField()
    kind = models.CharField(max_length=30, choices=Kind.choices)
    external_charge_id = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.kind} - {self.to_email}"


class HistoricalOrder(models.Model):
    order_id = models.CharField(max_length=50, unique=True)
    email = models.EmailField()
    sku = models.CharField(max_length=50)
    extra_code = models.CharField(max_length=50, null=True, blank=True)
    channel = models.CharField(max_length=50)
    created_at = models.DateTimeField()
    total_cents = models.PositiveIntegerField()
    returned = models.BooleanField()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.order_id