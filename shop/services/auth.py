import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import jwt
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from jwt import PyJWKClient

from ..models import Membership, StaffInvitation
from .audit import record_event
from .tls import verified_ssl_context

_jwks = None


class SupabaseAuthError(ValueError):
    """Raised when a Supabase session cannot be verified."""


def get_authenticated_user(token):
    if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
        raise SupabaseAuthError("Supabase Auth is not configured")
    request = Request(
        f"{settings.SUPABASE_URL}/auth/v1/user",
        headers={"apikey": settings.SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=8, context=verified_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise SupabaseAuthError("Supabase could not verify the session.") from exc


def _verify_hs256_session(token, issuer):
    """Verify legacy HS256 sessions through Supabase Auth, never with a shared secret."""
    cache_key = "supabase-session-claims:" + hashlib.sha256(token.encode()).hexdigest()
    claims = cache.get(cache_key)
    if claims is not None:
        return claims
    user = get_authenticated_user(token)
    claims = jwt.decode(
        token,
        options={
            "verify_signature": False,
            "verify_exp": True,
            "verify_iss": True,
            "verify_aud": True,
            "require": ["exp", "sub", "iss"],
        },
        algorithms=["HS256"],
        issuer=issuer,
        audience="authenticated",
    )
    if user.get("id") != claims["sub"]:
        raise SupabaseAuthError("Supabase session identity did not match.")
    cache.set(cache_key, claims, timeout=30)
    return claims


def verify_access_token(token):
    global _jwks
    if not settings.SUPABASE_JWKS_URL or not settings.SUPABASE_ISSUER:
        raise SupabaseAuthError("Supabase Auth is not configured")
    header = jwt.get_unverified_header(token)
    algorithm = header.get("alg")
    if algorithm == "HS256":
        return _verify_hs256_session(token, settings.SUPABASE_ISSUER)
    if algorithm not in {"ES256", "RS256"}:
        raise SupabaseAuthError("Unsupported Supabase session signing algorithm")
    if _jwks is None:
        _jwks = PyJWKClient(
            settings.SUPABASE_JWKS_URL,
            cache_keys=True,
            ssl_context=verified_ssl_context(),
        )
    key = _jwks.get_signing_key_from_jwt(token).key
    return jwt.decode(
        token,
        key,
        algorithms=[algorithm],
        issuer=settings.SUPABASE_ISSUER,
        audience="authenticated",
        options={"require": ["exp", "sub", "iss"]},
    )


def resolve_membership(user_id, token):
    membership = Membership.objects.select_related("shop").filter(
        supabase_user_id=user_id, is_active=True
    ).first()
    if membership:
        return membership

    user = get_authenticated_user(token)
    email = (user.get("email") or "").strip().casefold()
    if user.get("id") != user_id or not email or not user.get("email_confirmed_at"):
        return None

    try:
        with transaction.atomic():
            existing = list(
                Membership.objects.select_for_update()
                .filter(email__iexact=email, is_active=True, supabase_user_id__isnull=True)
                .exclude(email="")[:2]
            )
            if len(existing) == 1:
                membership = existing[0]
                membership.supabase_user_id = user_id
                membership.save(update_fields=["supabase_user_id"])
                return membership
            if len(existing) > 1:
                return None
            return accept_pending_invitation(user_id, email, user)
    except IntegrityError:
        return Membership.objects.select_related("shop").filter(
            supabase_user_id=user_id, is_active=True
        ).first()


def accept_pending_invitation(user_id, email, user):
    if not email or not user.get("email_confirmed_at"):
        return None
    try:
        with transaction.atomic():
            invitation = StaffInvitation.objects.select_for_update().filter(
                status=StaffInvitation.Status.PENDING, email__iexact=email
            ).first()
            if invitation is None:
                return None
            metadata = user.get("user_metadata") or {}
            membership = Membership.objects.create(
                shop=invitation.shop,
                supabase_user_id=user_id,
                display_name=metadata.get("full_name", ""),
                email=email,
                role=Membership.Role.STAFF,
                permissions=invitation.permissions,
            )
            invitation.supabase_user_id = user_id
            invitation.status = StaffInvitation.Status.ACCEPTED
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["supabase_user_id", "status", "accepted_at"])
            record_event(invitation.shop, membership, "access.staff_joined", "membership", membership.pk, {"email": email})
            return membership
    except IntegrityError:
        return Membership.objects.select_related("shop").filter(
            supabase_user_id=user_id, is_active=True
        ).first()
