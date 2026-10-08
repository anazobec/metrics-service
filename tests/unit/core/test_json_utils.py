"""Tests for shared JSON normalization helpers."""

from datetime import date
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest

from apps.core.json_utils import json_size_bytes, to_jsonable

pytestmark = pytest.mark.unit


def test_to_jsonable_normalizes_nested_collector_values():
    value = {
        "decimal": Decimal("1.0"),
        "numpy_int": np.int64(2),
        "numpy_bool": np.bool_(True),
        "array": np.array([3, 4]),
        "tuple": (5, 6),
        "nan": float("nan"),
        "infinity": float("inf"),
        "nested": {"value": Decimal("2.5")},
    }

    assert to_jsonable(value) == {
        "decimal": "1.0",
        "numpy_int": 2,
        "numpy_bool": True,
        "array": [3, 4],
        "tuple": [5, 6],
        "nan": None,
        "infinity": None,
        "nested": {"value": "2.5"},
    }


def test_to_jsonable_uses_django_encodings_for_dates_and_uuids():
    value = {"date": date(2026, 10, 2), "uuid": UUID("00000000-0000-0000-0000-000000000000")}

    assert to_jsonable(value) == {"date": "2026-10-02", "uuid": "00000000-0000-0000-0000-000000000000"}


def test_json_size_bytes_normalizes_before_encoding():
    assert json_size_bytes({"value": Decimal("1.0")}) == len(b'{"value": "1.0"}')
