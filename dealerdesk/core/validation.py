"""Small strict validators for JSON input. JSON booleans are never accepted as numbers."""
from .exceptions import validation_error


def require_object(data):
    if not isinstance(data, dict):
        raise validation_error("Request body must be a JSON object.")
    return data


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def clean_text(data, field, *, required=False, max_length=255, errors):
    """Returns the stripped string, '' when absent and optional, or records an error."""
    value = data.get(field)
    if value is None:
        if required:
            errors.append({"field": field, "message": f"{field} is required."})
        return ""
    if not isinstance(value, str):
        errors.append({"field": field, "message": f"{field} must be a string."})
        return ""
    value = value.strip()
    if required and not value:
        errors.append({"field": field, "message": f"{field} must not be empty."})
    if len(value) > max_length:
        errors.append({"field": field, "message": f"{field} must be at most {max_length} characters."})
    return value


def raise_if(errors):
    if errors:
        raise validation_error(errors[0]["message"], errors)
