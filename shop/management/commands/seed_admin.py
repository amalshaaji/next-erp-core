import os

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from shop.models import Membership, Shop
from shop.services.audit import record_event


class Command(BaseCommand):
    help = "Idempotently seed or link the Supabase Auth Admin for this shop."

    def add_arguments(self, parser):
        parser.add_argument("--supabase-user-id", default=os.environ.get("SEED_ADMIN_SUPABASE_USER_ID", ""))
        parser.add_argument("--email", default=os.environ.get("SEED_ADMIN_EMAIL", ""))
        parser.add_argument("--display-name", default=os.environ.get("SEED_ADMIN_DISPLAY_NAME", ""))

    def handle(self, *args, **options):
        user_id = options["supabase_user_id"].strip()
        email = options["email"].strip().casefold()
        display_name = options["display_name"].strip()
        if not user_id:
            raise CommandError("Set SEED_ADMIN_SUPABASE_USER_ID or pass --supabase-user-id.")
        if not email:
            raise CommandError("Set SEED_ADMIN_EMAIL or pass --email.")
        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommandError("SEED_ADMIN_EMAIL must be a valid email address.") from exc
        if len(user_id) > 100:
            raise CommandError("Supabase user ID must be 100 characters or fewer.")

        shops = list(Shop.objects.all()[:2])
        if len(shops) != 1:
            raise CommandError("Admin seeding requires exactly one shop. Create the first shop with bootstrap_shop.")
        shop = shops[0]

        try:
            with transaction.atomic():
                membership = Membership.objects.select_for_update().filter(supabase_user_id=user_id).first()
                if membership and membership.shop_id != shop.pk:
                    raise CommandError("This Supabase user is already linked to another shop.")
                if membership and not membership.is_active:
                    raise CommandError("This membership is inactive. Reactivate it through the Admin access flow first.")

                if membership is None:
                    matches = list(
                        Membership.objects.select_for_update()
                        .filter(shop=shop, email__iexact=email)[:2]
                    )
                    if len(matches) > 1:
                        raise CommandError("More than one membership uses this email. Resolve the duplicate before seeding.")
                    membership = matches[0] if matches else None
                    if membership and not membership.is_active:
                        raise CommandError("This membership is inactive. Reactivate it through the Admin access flow first.")
                    if membership and membership.supabase_user_id not in (None, "", user_id):
                        raise CommandError("This email is already linked to another Supabase user.")

                created = membership is None
                if created:
                    membership = Membership.objects.create(
                        shop=shop,
                        supabase_user_id=user_id,
                        email=email,
                        display_name=display_name,
                        role=Membership.Role.ADMIN,
                    )
                else:
                    changed_fields = []
                    if membership.supabase_user_id != user_id:
                        membership.supabase_user_id = user_id
                        changed_fields.append("supabase_user_id")
                    if membership.email != email:
                        membership.email = email
                        changed_fields.append("email")
                    if display_name and membership.display_name != display_name:
                        membership.display_name = display_name
                        changed_fields.append("display_name")
                    if membership.role != Membership.Role.ADMIN:
                        membership.role = Membership.Role.ADMIN
                        changed_fields.append("role")
                    if changed_fields:
                        membership.save(update_fields=changed_fields)

                if created or changed_fields:
                    record_event(
                        shop,
                        membership,
                        "access.admin_seeded",
                        "membership",
                        membership.pk,
                        {"email": email, "created": created},
                    )
        except IntegrityError as exc:
            raise CommandError("Could not link this Supabase user. Check for a duplicate membership.") from exc

        action = "Created" if created else ("Linked or updated" if changed_fields else "Already seeded")
        self.stdout.write(self.style.SUCCESS(f"{action} Admin access for {email} in {shop.name}."))
