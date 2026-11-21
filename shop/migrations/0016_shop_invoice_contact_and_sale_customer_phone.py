from django.db import migrations, models


def backfill_existing_addresses(apps, schema_editor):
    Shop = apps.get_model("shop", "Shop")
    Sale = apps.get_model("shop", "Sale")
    address = "Panoor, Pallimukku, Alappuzha"
    Shop.objects.filter(address="").update(address=address)
    Sale.objects.filter(shop_address_snapshot="").update(shop_address_snapshot=address)


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0015_shop_phone_sale_shop_phone_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="shop",
            name="address",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="shop",
            name="email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="sale",
            name="shop_address_snapshot",
            field=models.CharField(blank=True, max_length=240),
        ),
        migrations.AddField(
            model_name="sale",
            name="shop_email_snapshot",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="sale",
            name="customer_phone",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.RunPython(backfill_existing_addresses, migrations.RunPython.noop),
    ]
