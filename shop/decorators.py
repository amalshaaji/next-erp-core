from functools import wraps

from django.http import HttpResponseForbidden


def permission_required(capability):
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.membership is None or not request.membership.can(capability):
                return HttpResponseForbidden("You do not have permission to do that. Ask your Admin if you need access.")
            return view(request, *args, **kwargs)
        return wrapped
    return decorate
