import jwt
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from jwt import PyJWKClient
from ..models import Membership, StaffInvitation
from .audit import record_event

_jwks = None

def verify_access_token(token):
    global _jwks
    if not settings.SUPABASE_JWKS_URL or not settings.SUPABASE_ISSUER: raise ValueError("Supabase Auth is not configured")
    if _jwks is None: _jwks = PyJWKClient(settings.SUPABASE_JWKS_URL, cache_keys=True)
    key = _jwks.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["ES256", "RS256"], issuer=settings.SUPABASE_ISSUER, audience="authenticated", options={"require": ["exp", "sub", "iss"]})

def accept_pending_invitation(user_id, email, claims):
    if not email: return None
    try:
        with transaction.atomic():
            invitation = StaffInvitation.objects.select_for_update().filter(status=StaffInvitation.Status.PENDING, email__iexact=email).first()
            if invitation is None: return None
            metadata = claims.get("user_metadata") or {}
            membership = Membership.objects.create(shop=invitation.shop, supabase_user_id=user_id, display_name=metadata.get("full_name", ""), email=email, role=Membership.Role.STAFF, permissions=invitation.permissions)
            invitation.status = StaffInvitation.Status.ACCEPTED; invitation.supabase_user_id = user_id; invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["status", "supabase_user_id", "accepted_at"])
            record_event(invitation.shop, membership, "access.staff_joined", "membership", membership.pk, {"email": email})
            return membership
    except IntegrityError:
        return Membership.objects.select_related("shop").filter(supabase_user_id=user_id, is_active=True).first()
