"""Shared conversion helpers for values stored or measured as JSON."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder


def _json_key(value: Any) -> str:
    """Convert a mapping key to the string representation used by JSON objects."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _to_jsonable_complex(value: Any) -> Any:
    """Convert non-scalar values to JSON-compatible values."""
    # DataFrame serialization handles pandas and NumPy dtypes, timestamps, and missing values.
    to_json = getattr(value, "to_json", None)
    if callable(to_json) and hasattr(value, "columns"):
        return json.loads(to_json(orient="records", date_format="iso"))

    if isinstance(value, Mapping):
        return {_json_key(key): to_jsonable(item) for key, item in value.items()}

    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]

    # Handle NumPy arrays and scalars without importing NumPy at module load.
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return to_jsonable(tolist())

    item = getattr(value, "item", None)
    if callable(item):
        return to_jsonable(item())

    try:
        return DjangoJSONEncoder().default(value)
    except TypeError:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable") from None


def to_jsonable(value: Any) -> Any:
    """Recursively convert collector output to values accepted by JSONField."""
    if value is None or isinstance(value, (str, int, bool)):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else None

    return _to_jsonable_complex(value)


def json_size_bytes(value: Any) -> int:
    """Return the UTF-8 JSON size after normalizing a value."""
    return len(json.dumps(to_jsonable(value)).encode("utf-8"))
