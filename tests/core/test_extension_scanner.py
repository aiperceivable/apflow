"""Tests for extension scanner with v2 executor set"""

from apflow.core.extensions.scanner import ExtensionScanner


class TestExtensionScanner:
    def test_scan_discovers_core_executors(self):
        """Verify scanner finds the 4 retained executors."""
        metadata = ExtensionScanner.scan_builtin_executors()

        expected = [
            "rest_executor",
            "aggregate_results_executor",
            "apflow_api_executor",
            "send_email_executor",
        ]

        for executor_id in expected:
            assert executor_id in metadata, f"Expected executor '{executor_id}' not found"

    def test_get_executor_metadata_returns_valid_data(self):
        """Verify metadata structure for REST executor."""
        metadata = ExtensionScanner.get_executor_metadata("rest_executor")

        assert metadata is not None
        assert metadata.id == "rest_executor"
        assert metadata.name
        assert metadata.module_path == "apflow.extensions.http.rest_executor"
        assert metadata.class_name == "RestExecutor"

    def test_get_all_executor_ids(self):
        """All executor IDs are non-empty strings."""
        metadata = ExtensionScanner.scan_builtin_executors()
        for executor_id in metadata:
            assert isinstance(executor_id, str)
            assert len(executor_id) > 0

    def test_get_all_metadata(self):
        """All metadata entries have required fields."""
        metadata = ExtensionScanner.scan_builtin_executors()
        for executor_id, meta in metadata.items():
            assert meta.id == executor_id
            assert meta.name
            assert meta.module_path
            assert meta.class_name
            assert meta.description
