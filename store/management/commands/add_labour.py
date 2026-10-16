from decimal import Decimal

from django.core.management.base import BaseCommand

from store import services
from store.models import Customer, Labour

ZERO = Decimal("0")


class Command(BaseCommand):
    help = ("Fills the structure wage and electrical work of customers from the quotation website: adds the Labour rows when they are missing "
            "and fills in the amounts that are still 0.  Amounts you typed are never changed.")

    def handle(self, *args, **options):
        for customer in Customer.objects.exclude(quotation_id=None):
            rows = {w.kind: w for w in customer.labour.all()}
            if rows and all(w.amount > 0 for w in rows.values()):
                continue
            try:
                data = services.get_quotation(customer.quotation_id)
            except services.QuotationApiError as problem:
                self.stdout.write("%s: could not read the quotation (%s)." % (customer.name, problem))
                continue
            wanted = {"structure": ("Structure wage", data.get("structure_wage")), "electrical": ("Electrical work", data.get("electrical_work"))}
            if "structure_wage" not in data:
                self.stdout.write("%s: the quotation website did not send the wages. Deploy its latest version first." % customer.name)
                continue
            for kind, (label, raw) in wanted.items():
                amount = services.to_decimal(raw, ZERO)
                labour = rows.get(kind)
                if labour is None:
                    Labour.objects.create(customer=customer, kind=kind, label=label, amount=amount)
                elif labour.amount <= 0:
                    labour.amount = amount
                    labour.save()
            self.stdout.write("%s: labour amounts filled." % customer.name)
