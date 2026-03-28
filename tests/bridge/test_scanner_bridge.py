"""Tests for scanner_bridge module"""

from apflow.bridge.scanner_bridge import discover_executor_modules


class TestDiscoverExecutorModules:
    def test_returns_list(self):
        adapters = discover_executor_modules()
        assert isinstance(adapters, list)
        assert len(adapters) > 0

    def test_has_rest_executor(self):
        adapters = discover_executor_modules()
        ids = [a._executor_id for a in adapters]
        assert "rest_executor" in ids

    def test_has_system_info_executor(self):
        adapters = discover_executor_modules()
        ids = [a._executor_id for a in adapters]
        assert "system_info_executor" in ids

    def test_adapters_have_schemas(self):
        adapters = discover_executor_modules()
        for adapter in adapters:
            assert isinstance(adapter.input_schema, dict)
            assert isinstance(adapter.output_schema, dict)
            assert "type" in adapter.input_schema

    def test_adapters_have_descriptions(self):
        adapters = discover_executor_modules()
        for adapter in adapters:
            assert isinstance(adapter.description, str)
            assert len(adapter.description) > 0
