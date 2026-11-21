from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0014_remove_clerk_auth_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="shop",
            name="phone",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="sale",
            name="shop_phone_snapshot",
            field=models.CharField(blank=True, max_length=40),
        ),
    ]
