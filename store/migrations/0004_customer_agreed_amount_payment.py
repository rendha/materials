import decimal

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0003_alter_stock_options_stock_make"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="agreed_amount",
            field=models.DecimalField(decimal_places=2, default=decimal.Decimal("0"), max_digits=12),
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("paid_on", models.DateField(default=django.utils.timezone.localdate)),
                ("mode", models.CharField(choices=[("cash", "Cash"), ("bank", "Bank transfer"), ("upi", "UPI"), ("cheque", "Cheque"), ("other", "Other")], default="bank", max_length=10)),
                ("note", models.CharField(blank=True, max_length=200)),
                ("recorded_by", models.CharField(blank=True, max_length=150)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to="store.customer")),
            ],
            options={
                "ordering": ["-paid_on", "-id"],
            },
        ),
    ]
