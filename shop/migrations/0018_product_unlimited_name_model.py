from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0017_product_default_reorder_level_one'),
    ]

    operations = [
        migrations.AlterField(
            model_name='product',
            name='name',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='product',
            name='brand_model',
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name='saleline',
            name='description',
            field=models.TextField(),
        ),
    ]
