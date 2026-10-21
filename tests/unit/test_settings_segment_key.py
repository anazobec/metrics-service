"""
Unit tests for segment write key loading in apps.core.segment.

Covers read_segment_key_from_path (single file, plaintext).
"""

import os
from pathlib import Path
from unittest import mock

import pytest


@pytest.mark.unit
class TestReadSegmentKeyFromPath:
    """Tests for read_segment_key_from_path (low-level file reading)."""

    def test_reads_key_successfully(self, tmp_path):
        """Successfully reads and strips key from file."""
        from apps.core.segment import read_segment_key_from_path

        key_file = tmp_path / "key"
        key_file.write_text("  test-key-123  \n")

        result = read_segment_key_from_path(key_file)

        assert result == "test-key-123"

    def test_returns_none_when_not_a_file(self, tmp_path):
        """Returns None when path is a directory."""
        from apps.core.segment import read_segment_key_from_path

        result = read_segment_key_from_path(tmp_path)

        assert result is None

    def test_returns_none_when_file_empty(self, tmp_path):
        """Returns None when file is empty after strip."""
        from apps.core.segment import read_segment_key_from_path

        key_file = tmp_path / "key"
        key_file.write_text("   \n  ")

        result = read_segment_key_from_path(key_file)

        assert result is None

    def test_returns_none_on_os_error(self, tmp_path):
        """Returns None and suppresses OSError when reading file fails."""
        from apps.core.segment import read_segment_key_from_path

        path_mock = mock.MagicMock(spec=Path)
        path_mock.is_file.return_value = True
        os_error = OSError("Permission denied")
        os_error.filename = "/test/file"
        path_mock.read_text.side_effect = os_error

        result = read_segment_key_from_path(path_mock)

        assert result is None
