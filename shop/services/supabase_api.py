import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from django.conf import settings

class SupabaseApiError(Exception): pass

def invite_user(email, redirect_to):
    if not settings.SUPABASE_SERVICE_ROLE_KEY: raise SupabaseApiError("Supabase service-role key is not configured.")
    request = Request(f"{settings.SUPABASE_URL}/auth/v1/admin/invite", data=json.dumps({"email": email, "redirect_to": redirect_to}).encode(), method="POST", headers={"apikey": settings.SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=8) as response: return json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, OSError) as exc: raise SupabaseApiError("Supabase could not send the invitation.") from exc
