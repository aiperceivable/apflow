"""Tests for ConfigManager v2 (YAML + env var support)"""

import os
import tempfile


from apflow.core.config_manager import ConfigManager, _flatten_dict, _key_to_env


class TestFlattenDict:
    def test_flat(self):
        assert _flatten_dict({"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_nested(self):
        result = _flatten_dict({"api": {"timeout": 30, "url": "http://x"}})
        assert result == {"api.timeout": 30, "api.url": "http://x"}

    def test_deep_nested(self):
        result = _flatten_dict({"a": {"b": {"c": 1}}})
        assert result == {"a.b.c": 1}

    def test_empty(self):
        assert _flatten_dict({}) == {}


class TestKeyToEnv:
    def test_simple(self):
        assert _key_to_env("api.server_url") == "APFLOW_API_SERVER_URL"

    def test_nested(self):
        assert _key_to_env("durability.max_attempts") == "APFLOW_DURABILITY_MAX_ATTEMPTS"


class TestConfigManagerDefaults:
    def test_default_api_timeout(self):
        cm = ConfigManager()
        assert cm.get("api.timeout") == 30.0

    def test_default_api_server_url_none(self):
        cm = ConfigManager()
        assert cm.get("api.server_url") is None

    def test_default_durability(self):
        cm = ConfigManager()
        assert cm.get("durability.max_attempts") == 3
        assert cm.get("durability.backoff_strategy") == "exponential"

    def test_get_missing_key_returns_default(self):
        cm = ConfigManager()
        assert cm.get("nonexistent", "fallback") == "fallback"

    def test_is_api_not_configured_by_default(self):
        cm = ConfigManager()
        assert not cm.is_api_configured()


class TestConfigManagerYAML:
    def test_load_yaml(self):
        yaml_content = """
api:
  server_url: http://localhost:9000
  timeout: 60.0
governance:
  default_policy: strict
  downgrade_chain:
    - opus
    - sonnet
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            path = f.name

        try:
            cm = ConfigManager()
            cm._load_yaml(path)
            assert cm.get("api.server_url") == "http://localhost:9000"
            assert cm.get("api.timeout") == 60.0
            assert cm.get("governance.default_policy") == "strict"
            assert cm.get("governance.downgrade_chain") == ["opus", "sonnet"]
        finally:
            os.unlink(path)

    def test_yaml_overrides_defaults(self):
        yaml_content = "durability:\n  max_attempts: 10\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            path = f.name

        try:
            cm = ConfigManager()
            cm._load_yaml(path)
            assert cm.get("durability.max_attempts") == 10
            # Other defaults preserved
            assert cm.get("api.timeout") == 30.0
        finally:
            os.unlink(path)

    def test_invalid_yaml_warns(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(": invalid: yaml: {{{}}")
            path = f.name

        try:
            cm = ConfigManager()
            cm._load_yaml(path)
            # Should not crash, defaults preserved
            assert cm.get("api.timeout") == 30.0
        finally:
            os.unlink(path)


class TestConfigManagerEnvOverride:
    def test_env_overrides_value(self, monkeypatch):
        monkeypatch.setenv("APFLOW_API_SERVER_URL", "http://from-env:8000")
        cm = ConfigManager()
        assert cm.get("api.server_url") == "http://from-env:8000"
        assert cm.api_server_url == "http://from-env:8000"
        assert cm.is_api_configured()

    def test_env_coerces_float(self, monkeypatch):
        monkeypatch.setenv("APFLOW_API_TIMEOUT", "120.5")
        cm = ConfigManager()
        assert cm.get("api.timeout") == 120.5
        assert cm.api_timeout == 120.5

    def test_env_coerces_int(self, monkeypatch):
        monkeypatch.setenv("APFLOW_API_RETRY_ATTEMPTS", "5")
        cm = ConfigManager()
        assert cm.get("api.retry_attempts") == 5

    def test_env_coerces_list(self, monkeypatch):
        monkeypatch.setenv("APFLOW_GOVERNANCE_DOWNGRADE_CHAIN", "opus,sonnet,haiku")
        cm = ConfigManager()
        assert cm.get("governance.downgrade_chain") == ["opus", "sonnet", "haiku"]

    def test_env_overrides_yaml(self, monkeypatch):
        yaml_content = "api:\n  timeout: 60.0\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            path = f.name

        try:
            monkeypatch.setenv("APFLOW_API_TIMEOUT", "999")
            monkeypatch.setenv("APFLOW_CONFIG", path)
            cm = ConfigManager()
            # Env var wins over YAML
            assert cm.get("api.timeout") == 999.0
        finally:
            os.unlink(path)


class TestConfigManagerSetReload:
    def test_set_and_get(self):
        cm = ConfigManager()
        cm.set("custom.key", "custom_value")
        assert cm.get("custom.key") == "custom_value"

    def test_reload_resets(self):
        cm = ConfigManager()
        cm.set("api.timeout", 999)
        assert cm.get("api.timeout") == 999
        cm.reload()
        assert cm.get("api.timeout") == 30.0  # Back to default

    def test_clear_resets_all(self):
        cm = ConfigManager()
        cm.set("custom.key", "value")
        cm.clear()
        assert cm.get("custom.key") is None
        assert cm.get("api.timeout") == 30.0


class TestConfigManagerProperties:
    def test_api_properties(self, monkeypatch):
        monkeypatch.setenv("APFLOW_API_SERVER_URL", "http://test:8000")
        monkeypatch.setenv("APFLOW_API_JWT_SECRET", "secret123")
        cm = ConfigManager()
        assert cm.api_server_url == "http://test:8000"
        assert cm.jwt_secret == "secret123"
        assert cm.api_timeout == 30.0
        assert cm.api_retry_attempts == 3
        assert cm.api_retry_backoff == 1.0
