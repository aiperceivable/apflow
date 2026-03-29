"""
Configuration manager for apflow.

Loads configuration from: defaults → YAML file → environment variables.
Uses a simple get(key, default) interface for future apcore Config migration.

Configuration file search order:
1. APFLOW_CONFIG env var (explicit path)
2. ./apflow.yaml (project-local)
3. ~/.aiperceivable/apflow/apflow.yaml (user-global)

Environment variable override:
  YAML key "api.server_url" → env var "APFLOW_API_SERVER_URL"
  (prefix APFLOW_, dots become underscores, uppercase)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional

from apflow.core.config.registry import ConfigRegistry, get_config
from apflow.core.types import TaskPostHook, TaskPreHook
from apflow.logger import get_logger

if TYPE_CHECKING:
    from apflow.core.storage.sqlalchemy.models import TaskModelType

logger = get_logger(__name__)

# Default configuration values
_DEFAULTS: Dict[str, Any] = {
    "api.server_url": None,
    "api.timeout": 30.0,
    "api.retry_attempts": 3,
    "api.retry_backoff": 1.0,
    "api.jwt_secret": None,
    "storage.dialect": "sqlite",
    "storage.path": None,
    "governance.default_policy": None,
    "governance.downgrade_chain": [],
    "durability.max_attempts": 3,
    "durability.backoff_strategy": "exponential",
    "durability.circuit_breaker_threshold": 5,
}


def _flatten_dict(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Flatten nested dict: {"api": {"timeout": 30}} → {"api.timeout": 30}."""
    result: Dict[str, Any] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            result.update(_flatten_dict(value, f"{full_key}."))
        else:
            result[full_key] = value
    return result


def _key_to_env(key: str) -> str:
    """Convert dot-separated key to env var name: "api.server_url" → "APFLOW_API_SERVER_URL"."""
    return "APFLOW_" + key.replace(".", "_").upper()


@dataclass
class ConfigManager:
    """Configuration manager with YAML + env var support.

    Interface is get(key, default) — compatible with future apcore Config migration.
    When apcore Config adds namespace support, migration is:
      config.get("api.server_url") → apcore_config.get("apflow.api.server_url")
    """

    _registry: ConfigRegistry = field(default_factory=get_config)
    _data: Dict[str, Any] = field(default_factory=dict)
    _yaml_path: Optional[str] = field(default=None)

    def __post_init__(self) -> None:
        # Start with defaults
        self._data = dict(_DEFAULTS)
        # Try to load YAML
        self._auto_load_yaml()
        # Apply env var overrides
        self._apply_env_overrides()

    def _auto_load_yaml(self) -> None:
        """Search for and load config YAML file."""
        # Priority 1: explicit path from env
        explicit = os.environ.get("APFLOW_CONFIG")
        if explicit and Path(explicit).is_file():
            self._load_yaml(explicit)
            return

        # Priority 2: project-local
        local_path = Path("apflow.yaml")
        if local_path.is_file():
            self._load_yaml(str(local_path))
            return

        # Priority 3: user-global
        global_path = Path.home() / ".aiperceivable" / "apflow" / "apflow.yaml"
        if global_path.is_file():
            self._load_yaml(str(global_path))
            return

    def _load_yaml(self, path: str) -> None:
        """Load YAML config file and merge into _data."""
        try:
            import yaml

            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            flat = _flatten_dict(raw)
            self._data.update(flat)
            self._yaml_path = path
            logger.info(f"Loaded config from {path}")
        except Exception as e:
            logger.warning(f"Failed to load config from {path}: {e}")

    def _apply_env_overrides(self) -> None:
        """Override config values from APFLOW_* environment variables."""
        for key, current_value in list(self._data.items()):
            env_name = _key_to_env(key)
            env_value = os.environ.get(env_name)
            if env_value is not None:
                self._data[key] = self._coerce(env_value, current_value, key)

    def _coerce(self, env_value: str, current_value: Any, key: str = "") -> Any:
        """Coerce env var string to the type of the current value."""
        if current_value is None:
            return env_value
        if isinstance(current_value, bool):
            return env_value.lower() in ("true", "1", "yes")
        if isinstance(current_value, int):
            try:
                return int(env_value)
            except ValueError:
                logger.warning(f"Cannot parse '{env_value}' as int for {key}, using default")
                return current_value
        if isinstance(current_value, float):
            try:
                return float(env_value)
            except ValueError:
                logger.warning(f"Cannot parse '{env_value}' as float for {key}, using default")
                return current_value
        if isinstance(current_value, list):
            return [s.strip() for s in env_value.split(",") if s.strip()]
        return env_value

    # --- Core get/set interface (future apcore Config compatible) ---

    def get(self, key: str, default: Any = None) -> Any:
        """Get config value by dot-separated key."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set config value by dot-separated key."""
        self._data[key] = value

    def reload(self) -> None:
        """Reload configuration from file + env vars."""
        self._data = dict(_DEFAULTS)
        if self._yaml_path:
            self._load_yaml(self._yaml_path)
        else:
            self._auto_load_yaml()
        self._apply_env_overrides()

    # --- Convenience properties (typed access for common config) ---

    @property
    def api_server_url(self) -> Optional[str]:
        return self.get("api.server_url")

    @property
    def api_timeout(self) -> float:
        return self.get("api.timeout", 30.0)

    @property
    def api_retry_attempts(self) -> int:
        return self.get("api.retry_attempts", 3)

    @property
    def api_retry_backoff(self) -> float:
        return self.get("api.retry_backoff", 1.0)

    @property
    def jwt_secret(self) -> Optional[str]:
        return self.get("api.jwt_secret")

    def is_api_configured(self) -> bool:
        return self.api_server_url is not None

    # --- Env file loading ---

    def load_env_files(self, paths: Iterable[Path], override: bool = False) -> None:
        """Load .env file, then re-apply env overrides."""
        try:
            from dotenv import load_dotenv
        except ImportError:
            logger.debug("python-dotenv not installed; skipping .env load")
            return

        for env_path in paths:
            try:
                if env_path.exists():
                    load_dotenv(env_path, override=override)
                    logger.info(f"Loaded .env file from {env_path}")
                    self._apply_env_overrides()  # Re-apply after loading .env
                    return
            except Exception as exc:
                logger.debug("Failed to load .env from %s: %s", env_path, exc)

    # --- Registry delegation (hooks, task model, flags) ---

    def set_task_model_class(self, cls: Optional["TaskModelType"]) -> None:
        self._registry.set_task_model_class(cls)

    def get_task_model_class(self) -> "TaskModelType":
        return self._registry.get_task_model_class()

    def register_pre_hook(self, hook: TaskPreHook) -> None:
        self._registry.register_pre_hook(hook)

    def register_post_hook(self, hook: TaskPostHook) -> None:
        self._registry.register_post_hook(hook)

    def get_pre_hooks(self) -> List[TaskPreHook]:
        return self._registry.get_pre_hooks()

    def get_post_hooks(self) -> List[TaskPostHook]:
        return self._registry.get_post_hooks()

    def set_use_task_creator(self, enabled: bool) -> None:
        self._registry.set_use_task_creator(enabled)

    def get_use_task_creator(self) -> bool:
        return self._registry.get_use_task_creator()

    def set_require_existing_tasks(self, required: bool) -> None:
        self._registry.set_require_existing_tasks(required)

    def get_require_existing_tasks(self) -> bool:
        return self._registry.get_require_existing_tasks()

    def register_task_tree_hook(self, hook_type: str, hook: Callable) -> None:
        self._registry.register_task_tree_hook(hook_type, hook)

    def get_task_tree_hooks(self, hook_type: str) -> List[Callable]:
        return self._registry.get_task_tree_hooks(hook_type)

    def set_demo_sleep_scale(self, scale: float) -> None:
        self._registry.set_demo_sleep_scale(scale)

    def get_demo_sleep_scale(self) -> float:
        return self._registry.get_demo_sleep_scale()

    # --- Reset ---

    def clear(self) -> None:
        self._registry.clear()
        self._data = dict(_DEFAULTS)
        self._yaml_path = None


_config_manager = ConfigManager()


def get_config_manager() -> ConfigManager:
    return _config_manager


__all__ = ["ConfigManager", "get_config_manager"]
