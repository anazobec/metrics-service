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

    @patch("apps.dashboard_reports.awx_queries.fetch_id_name")
    def test_fetch_labels(self, mock_fetch):
        mock_fetch.return_value = ([{"id": 4, "name": "Lbl"}], 1)
        items, total = awx_queries.fetch_labels(db_connection=MagicMock())
        assert items == [{"id": 4, "name": "Lbl"}]
        assert total == 1

    def test_labels_query_has_no_inline_group_by(self):
        """GROUP BY must not be baked into the LABELS SELECT literal, or WHERE clauses appended by
        fetch_data_from_db would land after it, producing invalid SQL. See AAP-85133."""
        assert "GROUP BY" not in AWXQuery.LABELS.value
        assert awx_queries.GROUP_BY_CLAUSES[AWXQuery.LABELS] == " GROUP BY name"

    @patch("apps.dashboard_reports.awx_queries._execute_db_query")
    def test_fetch_data_from_db_labels_search_orders_where_before_group_by(self, mock_exec):
        """Regression test: searching labels must produce 'WHERE ... GROUP BY ... ORDER BY',
        not 'GROUP BY ... WHERE ...' (invalid SQL)."""
        mock_exec.return_value = (["id", "name"], [(1, "labelTest1")])
        db_conn = MagicMock()

        awx_queries.fetch_data_from_db(
            AWXQuery.LABELS, join_alias="", db_connection=db_conn, search_str="label", pk=None
        )

        executed_query = mock_exec.call_args[0][1]
        where_pos = executed_query.index("WHERE")
        group_by_pos = executed_query.index("GROUP BY")
        order_by_pos = executed_query.index("ORDER BY")
        assert where_pos < group_by_pos < order_by_pos

    @patch("apps.dashboard_reports.awx_queries._execute_count_query")
    @patch("apps.dashboard_reports.awx_queries._execute_db_query")
    def test_fetch_data_from_db_labels_with_limit_and_search_dedupes(self, mock_exec, mock_count):
        """Paginated label search (the code path hit by the API list endpoint) must also compose
        valid SQL with WHERE before GROUP BY, in both the COUNT subquery and the main query."""
        mock_count.return_value = 1
        mock_exec.return_value = (["id", "name"], [(5, "labelTest1")])
        db_conn = MagicMock()

        rows, total = awx_queries.fetch_data_from_db(
            AWXQuery.LABELS, join_alias="", db_connection=db_conn, search_str="label", limit=10, offset=0
        )

        assert rows == [(5, "labelTest1")]
        assert total == 1
        count_query = mock_count.call_args[0][1]
        assert count_query.index("WHERE") < count_query.index("GROUP BY")
        main_query = mock_exec.call_args[0][1]
        assert main_query.index("WHERE") < main_query.index("GROUP BY") < main_query.index("ORDER BY")

    @patch("apps.dashboard_reports.awx_queries._execute_db_query")
    def test_fetch_data_from_db_labels_dedupe_by_name(self, mock_exec):
        """MIN(id) GROUP BY name should return one canonical row per label name."""
        mock_exec.return_value = (["id", "name"], [(5, "labelTest1")])
        db_conn = MagicMock()

        rows, _ = awx_queries.fetch_data_from_db(AWXQuery.LABELS, join_alias="", db_connection=db_conn)

        executed_query = mock_exec.call_args[0][1]
        assert "MIN(id)" in executed_query
        assert "GROUP BY name" in executed_query
        assert rows == [(5, "labelTest1")]
