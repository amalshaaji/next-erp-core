from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from shop.models import Membership, Shop


class Command(BaseCommand):
    help = "Create the first shop and its Admin membership after Clerk is configured."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--clerk-user-id", required=True)
        parser.add_argument("--email", default="")
        parser.add_argument("--display-name", default="")
        parser.add_argument("--currency", default="INR")

    def handle(self, *args, **options):
        currency = options["currency"].strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise CommandError("Currency must be a three-letter code, such as INR.")
        if len(options["clerk_user_id"].strip()) > 100:
            raise CommandError("Clerk user ID must be 100 characters or fewer.")
        if Shop.objects.exists():
            raise CommandError("A shop already exists. Use the Admin user management flow instead.")
        with transaction.atomic():
            shop = Shop.objects.create(name=options["name"], currency=currency)
            Membership.objects.create(shop=shop, clerk_user_id=options["clerk_user_id"].strip(), email=options["email"], display_name=options["display_name"], role=Membership.Role.ADMIN)
        self.stdout.write(self.style.SUCCESS(f"Created {shop.name} and its initial Admin membership."))
