import json
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand

from store.models import BosItem


class Command(BaseCommand):
    help = ("Loads the BOS item list (from the quotation work-out sheet) into the database, but only when it is empty, so a deploy never "
            "overwrites your edits.  Use --reset to replace the list with the original one.")

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="delete the BOS list and load the original one again")

    def handle(self, *args, **options):
        if BosItem.objects.exists() and not options["reset"]:
            self.stdout.write("The BOS list is already there (use --reset to load the original list again).")
            return
        data = json.loads((Path(__file__).resolve().parents[2] / "bos_items.json").read_text(encoding="utf-8"))
        BosItem.objects.all().delete()
        for position, item in enumerate(data):
            BosItem.objects.create(
                name=item["name"], make=item["make"], unit=item["unit"], position=position,
                quantity=Decimal(str(item["quantity"])), rate=Decimal(str(item["rate"])),
                per_panel=Decimal(str(item.get("per_panel", 0))))
        self.stdout.write("%d BOS items loaded." % len(data))
