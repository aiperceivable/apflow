"""
Tests for ExtensionScanner (AST-based executor discovery)
"""

import json
import sys
from pathlib import Path

from apflow.core.extensions.scanner import ExtensionScanner, ExecutorMetadata

# The 4 core executors in v2
CORE_EXECUTORS = [
    "rest_executor",
    "aggregate_results_executor",
    "apflow_api_executor",
    "send_email_executor",
]


class TestExtensionScanner:
    """Test suite for ExtensionScanner"""

    def setup_method(self) -> None:
        """Reset scanner state before each test."""
        ExtensionScanner.clear_cache()
        ExtensionScanner._scanned = False
        ExtensionScanner._metadata_cache.clear()

    def teardown_method(self) -> None:
        """Clean up after each test."""
        ExtensionScanner.clear_cache()

    # --- Discovery ---

    def test_scan_discovers_core_executors(self) -> None:
        """Verify scanner finds the 4 retained executors."""
        metadata = ExtensionScanner.scan_builtin_executors()

        for executor_id in CORE_EXECUTORS:
            assert executor_id in metadata, f"Expected executor '{executor_id}' not found"

        assert len(metadata) >= len(CORE_EXECUTORS)

    def test_get_executor_metadata_returns_valid_data(self) -> None:
        """Verify metadata structure for REST executor."""
        metadata = ExtensionScanner.get_executor_metadata("rest_executor")

        assert metadata is not None
        assert metadata.id == "rest_executor"
        assert metadata.name
        assert metadata.module_path == "apflow.extensions.http.rest_executor"
        assert metadata.class_name == "RestExecutor"
        assert "httpx" in metadata.dependencies
        assert metadata.always_available is False

    def test_nonexistent_executor_returns_none(self) -> None:
        """Verify get_executor_metadata returns None for unknown executor."""
        metadata = ExtensionScanner.get_executor_metadata("nonexistent_executor")
        assert metadata is None

    def test_get_all_executor_ids(self) -> None:
        """All executor IDs are non-empty strings."""
        executor_ids = ExtensionScanner.get_all_executor_ids()

        assert isinstance(executor_ids, list)
        assert len(executor_ids) >= len(CORE_EXECUTORS)
        for eid in executor_ids:
            assert isinstance(eid, str)
            assert len(eid) > 0

    def test_get_all_metadata(self) -> None:
        """All metadata entries have required fields and correct type."""
        all_metadata = ExtensionScanner.get_all_metadata()

        assert isinstance(all_metadata, dict)
        assert len(all_metadata) >= len(CORE_EXECUTORS)

        for executor_id, meta in all_metadata.items():
            assert isinstance(meta, ExecutorMetadata)
            assert meta.id == executor_id

    # --- Metadata completeness ---

    def test_metadata_structure_is_complete(self) -> None:
        """Verify ExecutorMetadata has all required fields."""
        metadata = ExtensionScanner.get_executor_metadata("aggregate_results_executor")

        assert metadata is not None
        assert hasattr(metadata, "id")
        assert hasattr(metadata, "name")
        assert hasattr(metadata, "description")
        assert hasattr(metadata, "module_path")
        assert hasattr(metadata, "class_name")
        assert hasattr(metadata, "file_path")
        assert hasattr(metadata, "dependencies")
        assert hasattr(metadata, "always_available")
        assert hasattr(metadata, "tags")

        assert isinstance(metadata.dependencies, list)
        assert isinstance(metadata.tags, list)
        assert isinstance(metadata.always_available, bool)

    def test_always_available_executors_have_no_dependencies(self) -> None:
        """Executors marked always_available must have no external dependencies."""
        all_metadata = ExtensionScanner.get_all_metadata()

        for executor_id, meta in all_metadata.items():
            if meta.always_available:
                assert len(meta.dependencies) == 0, (
                    f"Executor '{executor_id}' is marked always_available "
                    f"but has dependencies: {meta.dependencies}"
                )

    def test_tags_are_extracted_from_ast(self) -> None:
        """Verify tags are extracted correctly from executor classes."""
        metadata = ExtensionScanner.get_executor_metadata("rest_executor")

        assert metadata is not None
        assert isinstance(metadata.tags, list)

    # --- Caching ---

    def test_cache_is_created_after_scan(self) -> None:
        """Verify cache file is created after scanning."""
        ExtensionScanner.clear_cache()
        ExtensionScanner.scan_builtin_executors()

        cache_file = ExtensionScanner._cache_file
        assert cache_file.exists(), "Cache file should exist after scanning"

        with open(cache_file, "r") as f:
            cache_data = json.load(f)

        assert len(cache_data) > 0, "Cache should contain executor metadata"
        assert "rest_executor" in cache_data

    def test_cache_is_loaded_on_second_scan(self) -> None:
        """Verify cache is used on subsequent scans (no re-parsing)."""
        ExtensionScanner.scan_builtin_executors()
        first_scan_count = len(ExtensionScanner._metadata_cache)

        ExtensionScanner._scanned = False
        ExtensionScanner._metadata_cache.clear()

        ExtensionScanner.scan_builtin_executors()
        second_scan_count = len(ExtensionScanner._metadata_cache)

        assert first_scan_count == second_scan_count
        assert ExtensionScanner._scanned is True

    def test_force_rescan_bypasses_cache(self) -> None:
        """Verify force_rescan parameter bypasses cache."""
        ExtensionScanner.scan_builtin_executors()

        ExtensionScanner._metadata_cache.clear()

        ExtensionScanner.scan_builtin_executors(force_rescan=True)

        assert len(ExtensionScanner._metadata_cache) > 0

    def test_clear_cache_removes_file(self) -> None:
        """Verify clear_cache removes cache file and resets state."""
        ExtensionScanner.scan_builtin_executors()
        cache_file = ExtensionScanner._cache_file

        assert cache_file.exists()

        ExtensionScanner.clear_cache()

        assert not cache_file.exists()
        assert len(ExtensionScanner._metadata_cache) == 0
        assert ExtensionScanner._scanned is False

    # --- Lazy loading ---

    def test_lazy_loading_no_imports(self) -> None:
        """Verify scanning does not import heavy dependencies."""
        modules_before = set(sys.modules.keys())

        ExtensionScanner.clear_cache()
        ExtensionScanner.scan_builtin_executors()

        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before

        heavy_deps = ["docker", "asyncssh", "httpx", "websockets", "grpclib"]

        for dep in heavy_deps:
            modules_imported = [m for m in new_modules if m.startswith(dep)]
            assert len(modules_imported) == 0, (
                f"Heavy dependency '{dep}' should not be imported during scanning, "
                f"found: {modules_imported}"
            )

    # --- Utilities ---

    def test_file_to_module_path_conversion(self) -> None:
        """Verify file path to module path conversion."""
        test_file = Path("/path/to/apflow/extensions/http/rest_executor.py")
        module_path = ExtensionScanner._file_to_module_path(test_file)

        assert module_path == "apflow.extensions.http.rest_executor"

    def test_scanner_is_singleton(self) -> None:
        """Verify scanner uses singleton pattern."""
        from apflow.core.extensions.scanner import get_scanner

        scanner1 = get_scanner()
        scanner2 = get_scanner()

        assert scanner1 is scanner2

    def test_scan_returns_dict_after_cache_clear(self) -> None:
        """Verify scan returns a valid dict after cache is cleared."""
        ExtensionScanner.clear_cache()

        result = ExtensionScanner.scan_builtin_executors()

        assert isinstance(result, dict)

    def test_rescan_drops_stale_cache_entries(self) -> None:
        """A stale executor id (deleted/renamed) must not survive a rescan."""
        from types import SimpleNamespace

        # Inject a ghost that no longer exists on disk.
        ExtensionScanner._metadata_cache["ghost_executor"] = SimpleNamespace(id="ghost_executor")

        metadata = ExtensionScanner.scan_builtin_executors(force_rescan=True)

        assert "ghost_executor" not in metadata
        assert "rest_executor" in metadata  # real executors still discovered
