import logging
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import redirect
from django.utils import timezone
from .models import Membership
from .services.auth import verify_access_token, accept_pending_invitation
from .services.audit import record_event

logger = logging.getLogger(__name__)

class SupabaseShopMiddleware:
    PUBLIC_PREFIXES = ("/sign-in/", "/auth/", "/health/", "/static/")
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        request.membership = None
        if any(request.path_info.startswith(prefix) for prefix in self.PUBLIC_PREFIXES): return self.get_response(request)
        token = request.COOKIES.get("supabase_access_token")
        if not token: return redirect("sign_in")
        try: claims = verify_access_token(token)
        except Exception as exc:
            logger.warning("Rejected Supabase session (%s).", type(exc).__name__)
            return redirect("sign_in")
        user_id = claims["sub"]
        request.membership = Membership.objects.select_related("shop").filter(supabase_user_id=user_id, is_active=True).first()
        if request.membership is None: request.membership = accept_pending_invitation(user_id, claims.get("email", ""), claims)
        if request.membership is None: return redirect("sign_in")
        session_id = claims.get("session_id") or claims.get("sid")
        if session_id and cache.add(f"shop-audit-session:{request.membership.shop_id}:{session_id}", True, timeout=86400):
            record_event(request.membership.shop, request.membership, "auth.session_started", "supabase_session", session_id, {})
        with timezone.override(request.membership.shop.timezone): return self.get_response(request)

# Compatibility name for integrations importing the previous middleware class.
ClerkShopMiddleware = SupabaseShopMiddleware
