"""One error shape for the whole API: {"error": {"code", "message", "details"}} (contract section 3)."""
from django.http import Http404
from rest_framework import exceptions as drf_exc
from rest_framework.response import Response


class ApiError(Exception):
    """Raised by services and views; turned into the contract's error body by api_exception_handler."""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []


def validation_error(message, details=None):
    return ApiError(400, "VALIDATION_ERROR", message, details)


def not_found(message="Not found."):
    return ApiError(404, "NOT_FOUND", message)


def error_body(code, message, details=None):
    return {"error": {"code": code, "message": message, "details": details or []}}


def _flatten_drf_detail(detail, prefix=""):
    """DRF parse/validation details -> a list of {"field", "message"}."""
    out = []
    if isinstance(detail, dict):
        for key, value in detail.items():
            out += _flatten_drf_detail(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(detail, list):
        for value in detail:
            out += _flatten_drf_detail(value, prefix)
    else:
        out.append({"field": prefix or None, "message": str(detail)})
    return out


def api_exception_handler(exc, context):
    if isinstance(exc, ApiError):
        return Response(error_body(exc.code, exc.message, exc.details), status=exc.status)
    if isinstance(exc, Http404):
        return Response(error_body("NOT_FOUND", "Not found."), status=404)
    if isinstance(exc, (drf_exc.ValidationError, drf_exc.ParseError, drf_exc.UnsupportedMediaType)):
        details = _flatten_drf_detail(exc.detail)
        message = details[0]["message"] if details else "Invalid request."
        return Response(error_body("VALIDATION_ERROR", message, details), status=400)
    if isinstance(exc, drf_exc.NotFound):
        return Response(error_body("NOT_FOUND", str(exc.detail)), status=404)
    if isinstance(exc, drf_exc.MethodNotAllowed):
        return Response(error_body("METHOD_NOT_ALLOWED", str(exc.detail)), status=405)
    if isinstance(exc, drf_exc.APIException):
        code = str(exc.default_code).upper()
        return Response(error_body(code, str(exc.detail)), status=exc.status_code)
    return None  # unexpected: Django's handler500 renders it in the same shape
