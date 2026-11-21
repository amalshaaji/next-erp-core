from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0016_shop_invoice_contact_and_sale_customer_phone"),
    ]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="reorder_level",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
