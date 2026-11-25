import logging

import jwt
from django.db import InterfaceError, OperationalError, transaction
from django.shortcuts import redirect
from django.utils import timezone

from .models import AuditEvent, Shop
from .services import optional_cache
from .services.audit import record_event
from .services.auth import SupabaseAuthError, SupabaseAuthUnavailable, resolve_membership, verify_access_token
from .services.availability import service_unavailable

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
            request.membership = resolve_membership(claims["sub"], token)
            if request.membership is None:
                return redirect("sign_in")
            session_id = claims.get("session_id") or claims.get("sid")
            if session_id:
                self.record_session(request.membership, session_id)
        except (SupabaseAuthUnavailable, jwt.PyJWKClientConnectionError, OperationalError, InterfaceError) as exc:
            logger.warning("Shop access temporarily unavailable (%s).", type(exc).__name__)
            return service_unavailable(request)
        except (SupabaseAuthError, jwt.PyJWTError, KeyError) as exc:
            logger.warning("Rejected Supabase session (%s).", type(exc).__name__)
            return redirect("sign_in")
        with timezone.override(request.membership.shop.timezone):
            return self.get_response(request)

    @staticmethod
    def record_session(member, session_id):
        key = f"shop-audit-session:{member.shop_id}:{session_id}"
        if optional_cache.get(key):
            return
        # The database is authoritative even when Redis restarts or is unavailable.
        with transaction.atomic():
            Shop.objects.select_for_update().get(pk=member.shop_id)
            if not AuditEvent.objects.filter(
                shop=member.shop, event_type="auth.session_started",
                target_type="supabase_session", target_id=session_id,
            ).exists():
                record_event(member.shop, member, "auth.session_started", "supabase_session", session_id, {})
        # Only mark the session as recorded after the audit transaction succeeds.
        optional_cache.set(key, True, timeout=86400)

    def process_exception(self, request, exception):
        if isinstance(exception, (OperationalError, InterfaceError)):
            logger.warning("Shop database temporarily unavailable (%s).", type(exception).__name__)
            return service_unavailable(request)
        return None
