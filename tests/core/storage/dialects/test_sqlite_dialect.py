"""Tests for SQLite dialect"""

import pytest
from apflow.core.storage.dialects.sqlite import SQLiteDialect


class TestGetConnectionString:
    def test_memory(self):
        assert SQLiteDialect.get_connection_string(":memory:") == "sqlite:///:memory:"

    def test_shared_memory(self):
        path = "file:shared?mode=memory&cache=shared&uri=true"
        result = SQLiteDialect.get_connection_string(path)
        assert result == f"sqlite:///{path}"

    def test_file_path(self):
        result = SQLiteDialect.get_connection_string("apflow.db")
        assert result.startswith("sqlite:///")
        assert result.endswith("apflow.db")
        # Should be absolute path
        assert "/" in result.replace("sqlite:///", "")

    def test_absolute_file_path(self):
        result = SQLiteDialect.get_connection_string("/tmp/test.db")
        assert result == "sqlite:////tmp/test.db"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            SQLiteDialect.get_connection_string("")

    def test_default_is_memory(self):
        assert SQLiteDialect.get_connection_string() == "sqlite:///:memory:"


class TestNormalizeData:
    def test_dict_serialized(self):
        result = SQLiteDialect.normalize_data({"config": {"key": "val"}})
        assert result["config"] == '{"key": "val"}'

    def test_list_serialized(self):
        result = SQLiteDialect.normalize_data({"items": [1, 2, 3]})
        assert result["items"] == "[1, 2, 3]"

    def test_scalar_passthrough(self):
        result = SQLiteDialect.normalize_data({"name": "test", "count": 5})
        assert result == {"name": "test", "count": 5}

    def test_none_passthrough(self):
        result = SQLiteDialect.normalize_data({"value": None})
        assert result == {"value": None}

    def test_empty_dict(self):
        result = SQLiteDialect.normalize_data({})
        assert result == {}


class TestDenormalizeData:
    def test_json_string_parsed(self):
        result = SQLiteDialect.denormalize_data({"config": '{"key": "val"}'})
        assert result["config"] == {"key": "val"}

    def test_json_array_parsed(self):
        result = SQLiteDialect.denormalize_data({"items": "[1, 2, 3]"})
        assert result["items"] == [1, 2, 3]

    def test_plain_string_passthrough(self):
        result = SQLiteDialect.denormalize_data({"name": "hello"})
        assert result["name"] == "hello"

    def test_non_string_passthrough(self):
        result = SQLiteDialect.denormalize_data({"count": 5, "active": True})
        assert result == {"count": 5, "active": True}

    def test_roundtrip(self):
        original = {"config": {"nested": True}, "name": "test", "items": [1, 2]}
        normalized = SQLiteDialect.normalize_data(original)
        denormalized = SQLiteDialect.denormalize_data(normalized)
        assert denormalized == original


class TestGetPragmaStatements:
    def test_returns_five_pragmas(self):
        pragmas = SQLiteDialect.get_pragma_statements()
        assert len(pragmas) == 5

    def test_wal_mode(self):
        pragmas = SQLiteDialect.get_pragma_statements()
        assert any("journal_mode=WAL" in p for p in pragmas)

    def test_foreign_keys(self):
        pragmas = SQLiteDialect.get_pragma_statements()
        assert any("foreign_keys=ON" in p for p in pragmas)

    def test_busy_timeout(self):
        pragmas = SQLiteDialect.get_pragma_statements()
        assert any("busy_timeout" in p for p in pragmas)

    def test_synchronous_normal(self):
        pragmas = SQLiteDialect.get_pragma_statements()
        assert any("synchronous=NORMAL" in p for p in pragmas)

    def test_cache_size(self):
        pragmas = SQLiteDialect.get_pragma_statements()
        assert any("cache_size" in p for p in pragmas)


class TestGetEngineKwargs:
    def test_pool_pre_ping(self):
        kwargs = SQLiteDialect.get_engine_kwargs()
        assert kwargs["pool_pre_ping"] is True
