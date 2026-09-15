from unittest.mock import MagicMock, patch

import pytest

from apps.core.gateway_queries import _MEMBER_ROLE_NAMES, fetch_member_organizations


@pytest.mark.unit
class TestFetchMemberOrganizations:
    """Unit tests for fetch_member_organizations (gateway DB query helper)."""

    def _mock_connection(self, rows):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = rows
        mock_conn.cursor.return_value = mock_cursor
        return mock_conn, mock_cursor

    def test_returns_formatted_organizations(self):
        mock_conn, mock_cursor = self._mock_connection([(1, "Org A"), (2, "Org B")])

        result = fetch_member_organizations("ana", mock_conn)

        assert result == [{"id": 1, "name": "Org A"}, {"id": 2, "name": "Org B"}]
        mock_cursor.execute.assert_called_once()
        (query, params), _ = mock_cursor.execute.call_args
        assert params == ["ana", list(_MEMBER_ROLE_NAMES)]
        assert "dab_rbac_roleuserassignment" in query

    def test_returns_empty_list_when_no_rows(self):
        mock_conn, _ = self._mock_connection([])

        assert fetch_member_organizations("ana", mock_conn) == []

    def test_logs_and_reraises_on_db_error(self):
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = RuntimeError("gateway db unreachable")

        with patch("apps.core.gateway_queries.logger") as mock_logger, pytest.raises(RuntimeError):
            fetch_member_organizations("ana", mock_conn)

        mock_logger.exception.assert_called_once()
