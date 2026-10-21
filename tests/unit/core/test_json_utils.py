"""Tests for shared JSON normalization helpers."""

from datetime import UTC, date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import numpy as np
import pandas as pd
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


def test_to_jsonable_keeps_integer_columns_with_nulls_as_integers():
    frame = pd.DataFrame(
        {
            "host_id": [31, None],
            "ratio": [0.5, None],
            "whole_float": [2.0, 3.0],
            "empty": [None, None],
        }
    )

    assert to_jsonable(frame) == [
        {"host_id": 31, "ratio": 0.5, "whole_float": 2, "empty": None},
        {"host_id": None, "ratio": None, "whole_float": 3, "empty": None},
    ]
    assert isinstance(to_jsonable(frame)[0]["host_id"], int)


def test_to_jsonable_serializes_date_columns_without_a_time_component():
    frame = pd.DataFrame(
        {
            "date": [date(2026, 10, 1), date(2026, 11, 1), None],
            "created": [datetime(2026, 10, 1, 12, 30, tzinfo=UTC)] * 3,
            "empty": [None, None, None],
        }
    )

    assert to_jsonable(frame) == [
        {"date": "2026-10-01", "created": "2026-10-01T12:30:00.000Z", "empty": None},
        {"date": "2026-11-01", "created": "2026-10-01T12:30:00.000Z", "empty": None},
        {"date": None, "created": "2026-10-01T12:30:00.000Z", "empty": None},
    ]


def test_to_jsonable_leaves_timestamp_columns_at_midnight_as_timestamps():
    frame = pd.DataFrame({"started": [datetime(2026, 10, 1, 0, 0, tzinfo=UTC)]})

    assert to_jsonable(frame) == [{"started": "2026-10-01T00:00:00.000Z"}]


def test_to_jsonable_does_not_mutate_the_caller_frame():
    frame = pd.DataFrame({"date": [date(2026, 10, 1), None], "host_id": [31, None]})
    before = frame.copy(deep=True)

    to_jsonable(frame)

    pd.testing.assert_frame_equal(frame, before)
