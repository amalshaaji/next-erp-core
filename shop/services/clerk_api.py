import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class ClerkApiError(Exception):
    """Safe-to-display Clerk API failure without leaking credentials."""


def clerk_request(path, method="GET", payload=None):
    if not settings.CLERK_SECRET_KEY:
        raise ClerkApiError("Clerk server API is not configured. Add CLERK_SECRET_KEY to the server environment.")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"https://api.clerk.com/v1/{path.lstrip('/')}",
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("errors", [{}])[0].get("long_message", "")
        except (ValueError, AttributeError, IndexError):
            detail = ""
        raise ClerkApiError(detail or f"Clerk could not complete the request (HTTP {exc.code}).") from None
    except (URLError, TimeoutError, OSError):
        raise ClerkApiError("Clerk could not be reached. Please try again in a moment.") from None
