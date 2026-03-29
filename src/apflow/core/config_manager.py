"""
Typed configuration manager wrapping the legacy ConfigRegistry.

Provides a single entrypoint for managing hooks, execution flags,
and API server configuration. Configuration is loaded from environment
variables.

Note: This module needs refactoring to align with apcore Config.
See docs/features/ for the design spec (TODO).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, List, Optional

from apflow.core.config.registry import ConfigRegistry, get_config
from apflow.core.types import TaskPostHook, TaskPreHook
from apflow.logger import get_logger

if TYPE_CHECKING:
    from apflow.core.storage.sqlalchemy.models import TaskModelType

logger = get_logger(__name__)

# Default API configuration values
DEFAULT_API_TIMEOUT = 30.0
DEFAULT_API_RETRY_ATTEMPTS = 3
DEFAULT_API_RETRY_BACKOFF = 1.0


@dataclass
class ConfigManager:
    """Typed configuration manager.

    Core configuration:
    - Hooks (pre/post execution)
    - Task model class
    - Task creator flags
    - Demo mode

    API server configuration (for scheduler and distributed scenarios):
    - api_server_url: URL of the API server
    - api_timeout: Request timeout in seconds
    - api_retry_attempts: Number of retry attempts
    - api_retry_backoff: Initial backoff for exponential retry
    - jwt_secret: Secret for JWT token generation
    """

    _registry: ConfigRegistry = field(default_factory=get_config)

    # API server configuration (loaded from env vars)
    _api_server_url: Optional[str] = field(default=None)
    _api_timeout: float = field(default=DEFAULT_API_TIMEOUT)
    _api_retry_attempts: int = field(default=DEFAULT_API_RETRY_ATTEMPTS)
    _api_retry_backoff: float = field(default=DEFAULT_API_RETRY_BACKOFF)
    _jwt_secret: Optional[str] = field(default=None)

    def __post_init__(self) -> None:
        """Load configuration from environment variables."""
        self._load_from_env()

    def _load_from_env(self) -> None:
        """Load API and runtime configuration from environment variables."""
        self._api_server_url = os.environ.get("APFLOW_API_SERVER_URL")
        self._jwt_secret = os.environ.get("APFLOW_JWT_SECRET")

        timeout = os.environ.get("APFLOW_API_TIMEOUT")
        if timeout:
            try:
                self._api_timeout = float(timeout)
            except ValueError:
                logger.warning(f"Invalid APFLOW_API_TIMEOUT: {timeout}, using default")

        retry = os.environ.get("APFLOW_API_RETRY_ATTEMPTS")
        if retry:
            try:
                self._api_retry_attempts = int(retry)
            except ValueError:
                logger.warning(f"Invalid APFLOW_API_RETRY_ATTEMPTS: {retry}, using default")

        backoff = os.environ.get("APFLOW_API_RETRY_BACKOFF")
        if backoff:
            try:
                self._api_retry_backoff = float(backoff)
            except ValueError:
                logger.warning(f"Invalid APFLOW_API_RETRY_BACKOFF: {backoff}, using default")

        if self._api_server_url:
            logger.info(f"API server configured: {self._api_server_url}")

    # --- Env file loading ---

    def load_env_files(self, paths: Iterable[Path], override: bool = False) -> None:
        """Load the first existing .env file, then refresh config from env."""
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
                    self._load_from_env()  # Refresh after loading .env
                    return
            except Exception as exc:
                logger.debug("Failed to load .env from %s: %s", env_path, exc)
                continue

    # --- Task model ---

    def set_task_model_class(self, task_model_class: Optional["TaskModelType"]) -> None:
        self._registry.set_task_model_class(task_model_class)

    def get_task_model_class(self) -> "TaskModelType":
        return self._registry.get_task_model_class()

    # --- Hooks ---

    def register_pre_hook(self, hook: TaskPreHook) -> None:
        self._registry.register_pre_hook(hook)

    def register_post_hook(self, hook: TaskPostHook) -> None:
        self._registry.register_post_hook(hook)

    def get_pre_hooks(self) -> List[TaskPreHook]:
        return self._registry.get_pre_hooks()

    def get_post_hooks(self) -> List[TaskPostHook]:
        return self._registry.get_post_hooks()

    # --- Task creator flags ---

    def set_use_task_creator(self, enabled: bool) -> None:
        self._registry.set_use_task_creator(enabled)

    def get_use_task_creator(self) -> bool:
        return self._registry.get_use_task_creator()

    def set_require_existing_tasks(self, required: bool) -> None:
        self._registry.set_require_existing_tasks(required)

    def get_require_existing_tasks(self) -> bool:
        return self._registry.get_require_existing_tasks()

    # --- Task tree hooks ---

    def register_task_tree_hook(self, hook_type: str, hook: Callable) -> None:
        self._registry.register_task_tree_hook(hook_type, hook)

    def get_task_tree_hooks(self, hook_type: str) -> List[Callable]:
        return self._registry.get_task_tree_hooks(hook_type)

    # --- Demo mode ---

    def set_demo_sleep_scale(self, scale: float) -> None:
        self._registry.set_demo_sleep_scale(scale)

    def get_demo_sleep_scale(self) -> float:
        return self._registry.get_demo_sleep_scale()

    # --- API server configuration ---

    @property
    def api_server_url(self) -> Optional[str]:
        return self._api_server_url

    @property
    def api_timeout(self) -> float:
        return self._api_timeout

    @property
    def api_retry_attempts(self) -> int:
        return self._api_retry_attempts

    @property
    def api_retry_backoff(self) -> float:
        return self._api_retry_backoff

    @property
    def jwt_secret(self) -> Optional[str]:
        return self._jwt_secret

    def is_api_configured(self) -> bool:
        """Check if API server is configured."""
        return self._api_server_url is not None

    # --- Reset ---

    def clear(self) -> None:
        self._registry.clear()
        self._api_server_url = None
        self._api_timeout = DEFAULT_API_TIMEOUT
        self._api_retry_attempts = DEFAULT_API_RETRY_ATTEMPTS
        self._api_retry_backoff = DEFAULT_API_RETRY_BACKOFF
        self._jwt_secret = None


_config_manager = ConfigManager()


def get_config_manager() -> ConfigManager:
    return _config_manager


__all__ = ["ConfigManager", "get_config_manager"]
