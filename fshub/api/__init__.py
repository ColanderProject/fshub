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
