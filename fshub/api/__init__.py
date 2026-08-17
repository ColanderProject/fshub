"""Shared helpers for the API blueprints."""

from flask import request


def json_body():
    """Return the request body as a dict.

    Anything that is not a JSON object (missing body, a list, a bare string)
    becomes an empty dict, so every endpoint can read its fields directly and
    answer with its own 400 instead of raising AttributeError.
    """
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def validate_group_filters(filter_in, filter_out):
    """Return an error message unless both filters are lists of group names."""
    filters = (filter_in, filter_out)
    if not all(isinstance(value, list) for value in filters):
        return 'Filters must be lists of group names'
    if not all(isinstance(name, str) and name for value in filters for name in value):
        return 'Every filter group name must be a non-empty string'
    return None
