"""Tests for core/extensions/manager.py"""

from apflow.core.extensions.manager import get_allowed_executor_ids


class TestGetAllowedExecutorIds:
    """Regression: get_allowed_executor_ids() failed open (returned None,
    meaning "allow all") when every configured allow-list entry was
    invalid, silently defeating the security control the administrator
    explicitly configured. (Review CRITICAL #34)
    """

    def test_neither_env_var_set_allows_all(self, monkeypatch):
        monkeypatch.delenv("APFLOW_EXTENSIONS", raising=False)
        monkeypatch.delenv("APFLOW_EXTENSIONS_IDS", raising=False)
        assert get_allowed_executor_ids() is None

    def test_all_invalid_extension_names_fail_closed(self, monkeypatch):
        monkeypatch.setenv("APFLOW_EXTENSIONS", "definitely_not_a_real_extension")
        monkeypatch.delenv("APFLOW_EXTENSIONS_IDS", raising=False)

        result = get_allowed_executor_ids()

        # Must fail closed (deny all) — not None (allow all).
        assert result == set()

    def test_all_invalid_executor_ids_fail_closed(self, monkeypatch):
        monkeypatch.delenv("APFLOW_EXTENSIONS", raising=False)
        monkeypatch.setenv("APFLOW_EXTENSIONS_IDS", "typo_executor_that_does_not_exist")

        result = get_allowed_executor_ids()

        assert result == set()

    def test_mixed_invalid_and_valid_entries_keep_the_valid_ones(self, monkeypatch):
        from apflow.core.extensions.scanner import ExtensionScanner

        real_id = ExtensionScanner.get_all_executor_ids()[0]

        monkeypatch.delenv("APFLOW_EXTENSIONS", raising=False)
        monkeypatch.setenv("APFLOW_EXTENSIONS_IDS", f"typo_executor_that_does_not_exist,{real_id}")

        result = get_allowed_executor_ids()

        assert result == {real_id}
