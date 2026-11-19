import logging

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import redirect
from django.utils import timezone

from .models import Membership, StaffInvitation
from .services.clerk_api import ClerkApiError, clerk_request
from .services.audit import record_event

logger = logging.getLogger(__name__)


class ClerkShopMiddleware:
    """Verify Clerk's session JWT and attach the active shop membership."""

    PUBLIC_PREFIXES = ("/sign-in/", "/health/", "/static/")

    def __init__(self, get_response):
        self.get_response = get_response
        self._jwt_module = None
        self._jwk_client = None
        self._static_signing_key = None
        if settings.CLERK_JWT_PUBLIC_KEY_BASE64:
            try:
                import base64
                from cryptography.hazmat.primitives import serialization
                self._static_signing_key = serialization.load_pem_public_key(
                    base64.b64decode(settings.CLERK_JWT_PUBLIC_KEY_BASE64, validate=True)
                )
            except (ValueError, TypeError):
                logger.error("Configured Clerk public key is invalid.")
        if settings.CLERK_JWKS_URL or self._static_signing_key:
            try:
                import jwt
                from jwt import PyJWKClient
                self._jwt_module = jwt
                if not self._static_signing_key and settings.CLERK_JWKS_URL:
                    self._jwk_client = PyJWKClient(settings.CLERK_JWKS_URL, cache_keys=True)
            except ImportError:
                logger.error("PyJWT is required to verify Clerk sessions. Install requirements.txt.")

    def __call__(self, request):
        request.membership = None
        path = request.path_info
        is_public = any(path.startswith(prefix) for prefix in self.PUBLIC_PREFIXES)
        if is_public:
            return self.get_response(request)

        token = request.COOKIES.get("__session")
        if not token:
            logger.info("Protected request has no Clerk session cookie.")
            return redirect("sign_in")
        if not self._jwt_module or not (self._static_signing_key or self._jwk_client) or not settings.CLERK_ISSUER:
            logger.error("Clerk session verification is unavailable because its verifier is not configured.")
            return redirect("sign_in")

        try:
            signing_key = self._static_signing_key or self._jwk_client.get_signing_key_from_jwt(token).key
            claims = self._jwt_module.decode(token, signing_key, algorithms=["RS256"], issuer=settings.CLERK_ISSUER, options={"require": ["exp", "sub", "iss"]})
            authorized_parties = settings.CLERK_AUTHORIZED_PARTIES
            if not authorized_parties or claims.get("azp") not in authorized_parties:
                raise self._jwt_module.InvalidTokenError("Unexpected authorized party")
        except Exception as exc:
            logger.warning("Rejected Clerk session (%s).", type(exc).__name__)
            return redirect("sign_in")

        request.membership = Membership.objects.select_related("shop").filter(clerk_user_id=claims["sub"], is_active=True).first()
        if request.membership is None:
            request.membership = self._accept_invitation(claims["sub"])

        if request.membership is None:
            logger.warning("Verified Clerk session has no active shop membership.")
            return redirect("sign_in")
        session_id = claims.get("sid")
        if session_id and cache.add(f"shop-audit-session:{request.membership.shop_id}:{session_id}", True, timeout=86400):
            record_event(request.membership.shop, request.membership, "auth.session_started", "clerk_session", session_id, {})
        with timezone.override(request.membership.shop.timezone):
            return self.get_response(request)

    @staticmethod
    def _accept_invitation(clerk_user_id):
        if not settings.CLERK_SECRET_KEY:
            return None
        negative_cache_key = f"clerk-shop-membership-miss:{clerk_user_id}"
        if cache.get(negative_cache_key):
            return None
        try:
            user = clerk_request(f"users/{clerk_user_id}")
        except ClerkApiError:
            return None
        verified_emails = {
            item.get("email_address", "").casefold()
            for item in user.get("email_addresses", [])
            if item.get("verification", {}).get("status") == "verified"
        }
        if not verified_emails:
            cache.set(negative_cache_key, True, timeout=45)
            return None
        from django.db import IntegrityError, transaction
        from django.utils import timezone

        try:
            with transaction.atomic():
                invitation = StaffInvitation.objects.select_for_update().filter(
                    status=StaffInvitation.Status.PENDING,
                    email__in=verified_emails,
                ).first()
                if invitation is None:
                    cache.set(negative_cache_key, True, timeout=45)
                    return None
                membership = Membership.objects.create(
                    shop=invitation.shop,
                    clerk_user_id=clerk_user_id,
                    display_name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
                    email=invitation.email,
                    role=Membership.Role.STAFF,
                    permissions=invitation.permissions,
                )
                invitation.status = StaffInvitation.Status.ACCEPTED
                invitation.clerk_user_id = clerk_user_id
                invitation.accepted_at = timezone.now()
                invitation.save(update_fields=["status", "clerk_user_id", "accepted_at"])
                record_event(invitation.shop, membership, "access.staff_joined", "membership", membership.pk, {"email": invitation.email})
                cache.delete(negative_cache_key)
                return membership
        except IntegrityError:
            return Membership.objects.select_related("shop").filter(clerk_user_id=clerk_user_id, is_active=True).first()
