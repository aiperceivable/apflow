"""Tests for scanner_bridge module"""

from types import SimpleNamespace
from unittest.mock import patch

from apflow.bridge.scanner_bridge import _create_adapter_from_metadata, discover_executor_modules


class TestDiscoverExecutorModules:
    def test_returns_list(self):
        adapters = discover_executor_modules()
        assert isinstance(adapters, list)
        assert len(adapters) > 0

    def test_has_rest_executor(self):
        adapters = discover_executor_modules()
        ids = [a.executor_id for a in adapters]
        assert "rest_executor" in ids

    def test_has_aggregate_results_executor(self):
        adapters = discover_executor_modules()
        ids = [a.executor_id for a in adapters]
        assert "aggregate_results_executor" in ids

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

    def test_type_error_during_import_is_skipped_not_crashed(self):
        """Regression: importing a discovered executor's module can trigger
        its @executor_register() decorator, whose _register_extension raises
        TypeError for a class that doesn't implement the Extension interface.
        The narrow except (ImportError, AttributeError) let that TypeError
        propagate and crash the entire discovery/bootstrap for every other
        executor too, instead of skipping just the one broken module.
        (Review CRITICAL #10)
        """
        metadata = SimpleNamespace(module_path="apflow.some.broken.module", class_name="Foo")
        with patch("importlib.import_module", side_effect=TypeError("bad decorator")):
            result = _create_adapter_from_metadata("broken_executor", metadata)
        assert result is None
