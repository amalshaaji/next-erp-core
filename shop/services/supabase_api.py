import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from django.conf import settings
from .tls import verified_ssl_context

class SupabaseApiError(Exception): pass

def create_user(email, password):
    """Create and confirm an Auth user using the server-only Admin API."""
    if not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise SupabaseApiError("Supabase account creation is not configured.")
    request = Request(
        f"{settings.SUPABASE_URL}/auth/v1/admin/users",
        data=json.dumps({"email": email, "password": password, "email_confirm": True}).encode(),
        method="POST",
        headers={
            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=8, context=verified_ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Do not surface the response body: it may contain submitted account data.
        raise SupabaseApiError(
            "Could not create the account. The email may already be registered, or the password may not meet the shop’s password rules."
        ) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise SupabaseApiError("Supabase could not create this account. Please try again.") from exc
    user_id = result.get("id") if isinstance(result, dict) else None
    if not user_id:
        raise SupabaseApiError("Supabase did not return a new account ID.")
    return user_id
