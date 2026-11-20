import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from shop.models import Membership, Shop


class Command(BaseCommand):
    help = "Create the first shop and its Admin membership after Supabase Auth is configured."

    def add_arguments(self, parser):
        parser.add_argument("--name", default=os.environ.get("SEED_SHOP_NAME", ""))
        parser.add_argument("--supabase-user-id", default=os.environ.get("SEED_ADMIN_SUPABASE_USER_ID", ""))
        parser.add_argument("--email", default=os.environ.get("SEED_ADMIN_EMAIL", ""))
        parser.add_argument("--display-name", default=os.environ.get("SEED_ADMIN_DISPLAY_NAME", ""))
        parser.add_argument("--currency", default=os.environ.get("SEED_SHOP_CURRENCY", "INR"))

    def handle(self, *args, **options):
        name = options["name"].strip()
        supabase_user_id = options["supabase_user_id"].strip()
        currency = options["currency"].strip().upper()
        if not name:
            raise CommandError("Set SEED_SHOP_NAME or pass --name.")
        if not supabase_user_id:
            raise CommandError("Set SEED_ADMIN_SUPABASE_USER_ID or pass --supabase-user-id.")
        if len(currency) != 3 or not currency.isalpha():
            raise CommandError("Currency must be a three-letter code, such as INR.")
        if len(supabase_user_id) > 100:
            raise CommandError("Supabase user ID must be 100 characters or fewer.")
        if Shop.objects.exists():
            raise CommandError("A shop already exists. Use the Admin user management flow instead.")
        with transaction.atomic():
            shop = Shop.objects.create(name=name, currency=currency)
            Membership.objects.create(shop=shop, supabase_user_id=supabase_user_id, email=options["email"].strip().casefold(), display_name=options["display_name"].strip(), role=Membership.Role.ADMIN)
        self.stdout.write(self.style.SUCCESS(f"Created {shop.name} and its initial Admin membership."))
