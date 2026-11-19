from django.conf import settings


def shop_context(request):
    return {
        "clerk_publishable_key": settings.CLERK_PUBLISHABLE_KEY,
        "clerk_frontend_api_url": settings.CLERK_FRONTEND_API_URL,
    }
