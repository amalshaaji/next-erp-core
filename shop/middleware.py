import logging

from django.core.cache import cache
from django.shortcuts import redirect
from django.utils import timezone

from .models import Membership
from .services.audit import record_event
from .services.auth import resolve_membership, verify_access_token

logger = logging.getLogger(__name__)


class SupabaseShopMiddleware:
    PUBLIC_PREFIXES = ("/sign-in/", "/auth/", "/sign-out/", "/health/", "/static/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.membership = None
        if any(request.path_info.startswith(prefix) for prefix in self.PUBLIC_PREFIXES):
            return self.get_response(request)

        token = request.COOKIES.get("supabase_access_token")
        if not token:
            return redirect("sign_in")
        try:
            claims = verify_access_token(token)
        except Exception as exc:
            logger.warning("Rejected Supabase session (%s).", type(exc).__name__)
            return redirect("sign_in")

        request.membership = resolve_membership(claims["sub"], token)
        if request.membership is None:
            return redirect("sign_in")

        session_id = claims.get("session_id") or claims.get("sid")
        if session_id and cache.add(
            f"shop-audit-session:{request.membership.shop_id}:{session_id}", True, timeout=86400
        ):
            record_event(request.membership.shop, request.membership, "auth.session_started", "supabase_session", session_id, {})
        with timezone.override(request.membership.shop.timezone):
            return self.get_response(request)
