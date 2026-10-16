from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Creates the two groups: Project (makes the material list) and Store (keeps the warehouse and purchases)."

    def handle(self, *args, **options):
        for name in ("Project", "Store"):
            _, created = Group.objects.get_or_create(name=name)
            self.stdout.write("%s group %s" % (name, "created" if created else "already exists"))
