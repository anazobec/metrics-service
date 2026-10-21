"""Tests for withholding collector columns from data leaving the service."""

import pandas as pd
import pytest

from apps.analytics.exclusions import apply_exclusions

pytestmark = pytest.mark.unit


def test_no_exclusions_returns_input_unchanged():
    df = pd.DataFrame([{"id": 1, "launched_by_username": "admin"}])

    assert apply_exclusions(df, ()) is df


def test_none_is_passed_through():
    assert apply_exclusions(None, ("launched_by_username",)) is None


def test_dataframe_column_is_dropped():
    df = pd.DataFrame([{"id": 1, "launched_by_id": 7, "launched_by_username": "admin"}])

    filtered = apply_exclusions(df, ("launched_by_username",))

    assert list(filtered.columns) == ["id", "launched_by_id"]


def test_dataframe_is_not_mutated():
    """The caller's frame still feeds the dashboard sync, so it must survive intact."""
    df = pd.DataFrame([{"id": 1, "launched_by_username": "admin"}])

    apply_exclusions(df, ("launched_by_username",))

    assert "launched_by_username" in df.columns


def test_dataframe_without_the_column_is_returned_as_is():
    df = pd.DataFrame([{"id": 1}])

    assert apply_exclusions(df, ("launched_by_username",)) is df


def test_dict_key_is_dropped():
    payload = {"version": "1.2.3", "launched_by_username": "admin"}

    assert apply_exclusions(payload, ("launched_by_username",)) == {"version": "1.2.3"}


def test_list_of_dicts_drops_the_key_from_every_row():
    rows = [{"id": 1, "launched_by_username": "admin"}, {"id": 2, "launched_by_username": "dev"}]

    assert apply_exclusions(rows, ("launched_by_username",)) == [{"id": 1}, {"id": 2}]


def test_unsupported_shape_is_returned_unchanged():
    assert apply_exclusions("not-a-collector-payload", ("launched_by_username",)) == "not-a-collector-payload"
