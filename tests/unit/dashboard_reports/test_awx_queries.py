from unittest.mock import MagicMock, patch

import pytest

from apps.dashboard_reports import awx_queries
from apps.dashboard_reports.awx_queries import AWXQuery, _execute_count_query, _execute_db_query


@pytest.mark.unit
class TestExecuteDbQuery:
    """Unit tests for _execute_db_query helper."""

    def test_returns_columns_and_rows(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.description = [("id",), ("name",)]
        mock_cursor.fetchall.return_value = [(1, "Org")]
        mock_conn.cursor.return_value = mock_cursor

        columns, data = _execute_db_query(mock_conn, "SELECT id, name FROM t", [])

        assert columns == ["id", "name"]
        assert data == [(1, "Org")]
        mock_cursor.execute.assert_called_once_with("SELECT id, name FROM t", [])

    def test_passes_params_to_execute(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.description = []
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor

        _execute_db_query(mock_conn, "SELECT 1 WHERE id = %s", [42])

        mock_cursor.execute.assert_called_once_with("SELECT 1 WHERE id = %s", [42])


@pytest.mark.unit
class TestExecuteCountQuery:
    """Unit tests for _execute_count_query helper."""

    def test_returns_integer_count(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (7,)
        mock_conn.cursor.return_value = mock_cursor

        result = _execute_count_query(mock_conn, "SELECT COUNT(*) FROM t", [])

        assert result == 7
        assert isinstance(result, int)

    def test_returns_zero_for_empty_table(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.cursor.return_value = mock_cursor

        result = _execute_count_query(mock_conn, "SELECT COUNT(*) FROM t", [])

        assert result == 0


@pytest.mark.unit
class TestAWXQueries:
    def test_build_where_clause_none(self):
        clause, params = awx_queries._build_where_clause("", None, None)
        assert clause == ""
        assert params == []

    def test_build_where_clause_search(self):
        clause, params = awx_queries._build_where_clause("x.", "foo", None)
        assert clause == " WHERE x.name ilike %s ESCAPE E'\\\\'"
        assert params == ["%foo%"]

    def test_build_where_clause_pk(self):
        clause, params = awx_queries._build_where_clause("y.", None, 42)
        assert clause == " WHERE y.id = %s"
        assert params == [42]

    def test_build_where_clause_both(self):
        clause, params = awx_queries._build_where_clause("z.", "bar", 7)
        assert clause == " WHERE z.name ilike %s ESCAPE E'\\\\' AND z.id = %s"
        assert params == ["%bar%", 7]

    def test_format_id_name_rows(self):
        rows = [(1, "A"), (2, "B")]
        result = awx_queries.format_id_name_rows(rows)
        assert result == [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]

    @patch("apps.dashboard_reports.awx_queries._execute_db_query")
    def test_fetch_data_from_db(self, mock_exec):
        mock_exec.return_value = (["id", "name"], [(1, "X")])
        db_conn = MagicMock()
        rows, total = awx_queries.fetch_data_from_db(
            AWXQuery.ORGANIZATIONS, join_alias="", db_connection=db_conn, search_str=None, pk=None
        )
        assert rows == [(1, "X")]
        assert total == 1  # len(rows) when no limit
        mock_exec.assert_called()

    @patch("apps.dashboard_reports.awx_queries._execute_db_query")
    @patch("apps.dashboard_reports.awx_queries._execute_count_query")
    def test_fetch_data_from_db_with_limit(self, mock_count, mock_exec):
        mock_count.return_value = 42
        mock_exec.return_value = (["id", "name"], [(1, "X"), (2, "Y")])
        db_conn = MagicMock()
        rows, total = awx_queries.fetch_data_from_db(
            AWXQuery.ORGANIZATIONS,
            join_alias="",
            db_connection=db_conn,
            search_str=None,
            pk=None,
            limit=10,
            offset=0,
        )
        assert rows == [(1, "X"), (2, "Y")]
        assert total == 42
        mock_count.assert_called_once()
        mock_exec.assert_called_once()

    @patch("apps.dashboard_reports.awx_queries.fetch_data_from_db")
    def test_fetch_id_name_success(self, mock_fetch):
        mock_fetch.return_value = ([(3, "C")], 1)
        items, total = awx_queries.fetch_id_name(AWXQuery.ORGANIZATIONS, error_msg="err", db_connection=MagicMock())
        assert items == [{"id": 3, "name": "C"}]
        assert total == 1

    @patch("apps.dashboard_reports.awx_queries.fetch_data_from_db")
    def test_fetch_id_name_error(self, mock_fetch):
        mock_fetch.side_effect = Exception("fail")
        with pytest.raises(Exception, match="fail"):
            awx_queries.fetch_id_name(AWXQuery.ORGANIZATIONS, error_msg="err", db_connection=MagicMock())

    @patch("apps.dashboard_reports.awx_queries.fetch_id_name")
    def test_fetch_organizations(self, mock_fetch):
        mock_fetch.return_value = ([{"id": 1, "name": "Org"}], 1)
        items, total = awx_queries.fetch_organizations(db_connection=MagicMock())
        assert items == [{"id": 1, "name": "Org"}]
        assert total == 1

    @patch("apps.dashboard_reports.awx_queries.fetch_id_name")
    def test_fetch_templates(self, mock_fetch):
        mock_fetch.return_value = ([{"id": 2, "name": "Tpl"}], 1)
        items, total = awx_queries.fetch_templates(db_connection=MagicMock())
        assert items == [{"id": 2, "name": "Tpl"}]
        assert total == 1

    @patch("apps.dashboard_reports.awx_queries.fetch_id_name")
    def test_fetch_projects(self, mock_fetch):
        mock_fetch.return_value = ([{"id": 3, "name": "Prj"}], 1)
        items, total = awx_queries.fetch_projects(db_connection=MagicMock())
        assert items == [{"id": 3, "name": "Prj"}]
        assert total == 1

    def test_format_label_rows_unique_names(self):
        rows = [(1, "Lbl", "OrgA"), (2, "Other", "OrgB")]
        result = awx_queries.format_label_rows(rows, duplicate_names=set())
        assert result == [{"id": 1, "name": "Lbl"}, {"id": 2, "name": "Other"}]

    def test_format_label_rows_disambiguates_duplicates(self):
        rows = [(1, "prod", "OrgA"), (2, "prod", "OrgB"), (3, "prod", "OrgC"), (4, "staging", "OrgA")]
        result = awx_queries.format_label_rows(rows, duplicate_names={"prod"})
        assert result == [
            {"id": 1, "name": "prod (OrgA)"},
            {"id": 2, "name": "prod (OrgB)"},
            {"id": 3, "name": "prod (OrgC)"},
            {"id": 4, "name": "staging"},
        ]

    def test_format_label_rows_uses_provided_duplicate_names_not_page_contents(self):
        """A single-row page must still be disambiguated if its name is a known duplicate overall."""
        rows = [(2, "prod", "OrgB")]
        result = awx_queries.format_label_rows(rows, duplicate_names={"prod"})
        assert result == [{"id": 2, "name": "prod (OrgB)"}]

    @patch("apps.dashboard_reports.awx_queries._fetch_duplicate_label_names")
    @patch("apps.dashboard_reports.awx_queries.fetch_data_from_db")
    def test_fetch_labels(self, mock_fetch, mock_dupes):
        mock_fetch.return_value = ([(4, "Lbl", "OrgA")], 1)
        mock_dupes.return_value = set()
        items, total = awx_queries.fetch_labels(db_connection=MagicMock())
        assert items == [{"id": 4, "name": "Lbl"}]
        assert total == 1

    @patch("apps.dashboard_reports.awx_queries._fetch_duplicate_label_names")
    @patch("apps.dashboard_reports.awx_queries.fetch_data_from_db")
    def test_fetch_labels_dedupes_duplicate_names_by_organization(self, mock_fetch, mock_dupes):
        mock_fetch.return_value = ([(1, "prod", "OrgA"), (2, "prod", "OrgB")], 2)
        mock_dupes.return_value = {"prod"}
        items, total = awx_queries.fetch_labels(db_connection=MagicMock())
        assert items == [{"id": 1, "name": "prod (OrgA)"}, {"id": 2, "name": "prod (OrgB)"}]
        assert total == 2

    @patch("apps.dashboard_reports.awx_queries._fetch_duplicate_label_names")
    @patch("apps.dashboard_reports.awx_queries.fetch_data_from_db")
    def test_fetch_labels_dedupe_stays_consistent_across_pages(self, mock_fetch, mock_dupes):
        """Regression test: a duplicate name split across pages (limit=1) must be disambiguated on
        every page, since duplicate detection is computed from the full dataset, not the page."""
        mock_dupes.return_value = {"prod"}

        mock_fetch.return_value = ([(1, "prod", "OrgA")], 2)
        page_one, total_one = awx_queries.fetch_labels(db_connection=MagicMock(), limit=1, offset=0)
        assert page_one == [{"id": 1, "name": "prod (OrgA)"}]
        assert total_one == 2

        mock_fetch.return_value = ([(2, "prod", "OrgB")], 2)
        page_two, total_two = awx_queries.fetch_labels(db_connection=MagicMock(), limit=1, offset=1)
        assert page_two == [{"id": 2, "name": "prod (OrgB)"}]
        assert total_two == 2

    @patch("apps.dashboard_reports.awx_queries._fetch_duplicate_label_names")
    @patch("apps.dashboard_reports.awx_queries.fetch_data_from_db")
    def test_fetch_labels_error(self, mock_fetch, mock_dupes):
        mock_fetch.side_effect = Exception("fail")
        with pytest.raises(Exception, match="fail"):
            awx_queries.fetch_labels(db_connection=MagicMock())

    def test_fetch_duplicate_label_names(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [("prod",)]
        mock_conn.cursor.return_value = mock_cursor

        result = awx_queries._fetch_duplicate_label_names(mock_conn, "l.", None, None)

        assert result == {"prod"}
        executed_query = mock_cursor.execute.call_args[0][0]
        assert "GROUP BY name HAVING COUNT(*) > 1" in executed_query
        assert "LIMIT" not in executed_query

    def test_labels_query_joins_organization(self):
        """LABELS must join main_organization so org name is available to disambiguate duplicates."""
        assert "main_label l" in AWXQuery.LABELS.value
        assert "JOIN main_organization o" in AWXQuery.LABELS.value
        assert "organization_name" in AWXQuery.LABELS.value
