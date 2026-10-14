from django.http import JsonResponse

from .exceptions import error_body


def health(request):
    """GET /api/health/ — the grader polls this until your app is ready."""
    return JsonResponse({"status": "ok"})


def not_found(request, exception=None):
    return JsonResponse(error_body("NOT_FOUND", "Not found."), status=404)


def server_error(request):
    return JsonResponse(error_body("INTERNAL_ERROR", "Something went wrong on our side."), status=500)
