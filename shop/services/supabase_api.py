import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from .tls import verified_ssl_context

class SupabaseApiError(Exception): pass

logger = logging.getLogger(__name__)

ACCOUNT_ERROR_MESSAGES = {
    "email_exists": "This email already has a Supabase account. Use password recovery or ask your Admin to check its shop access.",
    "user_already_exists": "This email already has a Supabase account. Use password recovery or ask your Admin to check its shop access.",
    "weak_password": "Supabase rejected this password. Check the password requirements in the Supabase project's Auth settings.",
    "email_address_invalid": "Supabase rejected this email address. Check the address and try again.",
    "validation_failed": "Supabase could not validate the account details. Check the email and password and try again.",
    "not_admin": "Supabase denied account creation. Check the server's Supabase Admin key and project configuration.",
    "no_authorization": "Supabase denied account creation. Check the server's Supabase Admin key and project configuration.",
    "over_request_rate_limit": "Too many account creation attempts. Wait a few minutes and try again.",
    "unexpected_failure": "Supabase could not save the account. Check the Supabase Auth logs for a database or service error.",
}


def account_creation_error(exc):
    # Only recognized codes may leave this boundary; never return or log raw messages.
    try:
        body = json.loads(exc.read(32768))
    except (OSError, ValueError):
        body = {}
    code = (body.get("error_code") or body.get("code")) if isinstance(body, dict) else None
    known_code = code if isinstance(code, str) and code in ACCOUNT_ERROR_MESSAGES else "unknown"
    logger.warning("Supabase account creation rejected (HTTP %s, code %s).", exc.code, known_code)
    if known_code != "unknown":
        return ACCOUNT_ERROR_MESSAGES[known_code]
    if exc.code in (401, 403):
        return "Supabase denied account creation. Check the server's Supabase Admin key and project configuration."
    if exc.code == 429:
        return "Too many account creation attempts. Wait a few minutes and try again."
    if exc.code >= 500:
        return "Supabase could not save the account. Check the Supabase Auth logs for a database or service error."
    return f"Supabase rejected account creation (HTTP {exc.code}). Check the Supabase Auth logs for details."


@sensitive_variables()
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
        raise SupabaseApiError(account_creation_error(exc)) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise SupabaseApiError("Supabase could not create this account. Please try again.") from exc
    user_id = result.get("id") if isinstance(result, dict) else None
    if not user_id:
        raise SupabaseApiError("Supabase did not return a new account ID.")
    return user_id
