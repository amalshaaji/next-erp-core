from django.http import HttpResponse
from django.template.loader import render_to_string


def service_unavailable(request):
    # Deliberately omit request context processors, database queries, and assets.
    content = render_to_string("shop/unavailable.html", {
        "was_submission": request.method not in {"GET", "HEAD", "OPTIONS"},
    })
    response = HttpResponse(content, status=503)
    response["Retry-After"] = "30"
    response["Cache-Control"] = "no-store"
    return response
