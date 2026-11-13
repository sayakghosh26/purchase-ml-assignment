
from django.db import migrations


def seed_catalog(apps, schema_editor):
    Item = apps.get_model("shop", "Item")
    Extra = apps.get_model("shop", "Extra")

    lamp, _ = Item.objects.get_or_create(
        sku="lamp",
        defaults={
            "name": "Maple lamp",
            "price_cents": 8000,
            "remaining": 2,
        },
    )

    stool, _ = Item.objects.get_or_create(
        sku="stool",
        defaults={
            "name": "Cedar stool",
            "price_cents": 12000,
            "remaining": 1,
        },
    )

    Extra.objects.get_or_create(
        item=stool,
        code="cushion",
        defaults={
            "name": "Cushion",
            "price_cents": 2500,
        },
    )


def reverse_seed_catalog(apps, schema_editor):
    Item = apps.get_model("shop", "Item")
    Item.objects.filter(sku__in=["lamp", "stool"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_catalog, reverse_seed_catalog),
    ]
