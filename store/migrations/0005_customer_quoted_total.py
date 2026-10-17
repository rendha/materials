import decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0004_customer_agreed_amount_payment"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="quoted_total",
            field=models.DecimalField(decimal_places=2, default=decimal.Decimal("0"), max_digits=12),
        ),
    ]
