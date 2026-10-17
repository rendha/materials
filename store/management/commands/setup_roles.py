from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Creates the groups: Project (makes the material list), Store (warehouse and purchases), Accounts (payments received) and Boss (dashboard, view only)."

    def handle(self, *args, **options):
        for name in ("Project", "Store", "Accounts", "Boss"):
            _, created = Group.objects.get_or_create(name=name)
            self.stdout.write("%s group %s" % (name, "created" if created else "already exists"))
