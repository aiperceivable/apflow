"""
Typed configuration manager wrapping the legacy ConfigRegistry.

Provides a single entrypoint for loading environment variables, managing
hooks, and controlling execution flags.
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


@dataclass
class ConfigManager:
    """Typed configuration manager.

    Usage:
        from apflow.core.config_manager import get_config_manager

        cm = get_config_manager()
        cm.register_pre_hook(lambda task: task.inputs.update({"ctx": "dynamic"}))
        cm.set_demo_sleep_scale(0.5)
        cm.load_env_files([Path.cwd()/".env"], override=False)
    """

    _registry: ConfigRegistry = field(default_factory=get_config)

    def load_env_files(self, paths: Iterable[Path], override: bool = False) -> None:
        """Load the first existing .env file from the provided paths."""
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
                    return
            except Exception as exc:
                logger.debug("Failed to load .env from %s: %s", env_path, exc)
                continue

        logger.debug("No .env file found in any of the checked paths")

    # Task model configuration

    def set_task_model_class(self, task_model_class: Optional["TaskModelType"]) -> None:
        self._registry.set_task_model_class(task_model_class)

    def get_task_model_class(self) -> "TaskModelType":
        return self._registry.get_task_model_class()

    # Hook management

    def register_pre_hook(self, hook: TaskPreHook) -> None:
        self._registry.register_pre_hook(hook)

    def register_post_hook(self, hook: TaskPostHook) -> None:
        self._registry.register_post_hook(hook)

    def get_pre_hooks(self) -> List[TaskPreHook]:
        return self._registry.get_pre_hooks()

    def get_post_hooks(self) -> List[TaskPostHook]:
        return self._registry.get_post_hooks()

    # Task creator flags

    def set_use_task_creator(self, enabled: bool) -> None:
        self._registry.set_use_task_creator(enabled)

    def get_use_task_creator(self) -> bool:
        return self._registry.get_use_task_creator()

    def set_require_existing_tasks(self, required: bool) -> None:
        self._registry.set_require_existing_tasks(required)

    def get_require_existing_tasks(self) -> bool:
        return self._registry.get_require_existing_tasks()

    # Task tree hooks

    def register_task_tree_hook(self, hook_type: str, hook: Callable) -> None:
        self._registry.register_task_tree_hook(hook_type, hook)

    def get_task_tree_hooks(self, hook_type: str) -> List[Callable]:
        return self._registry.get_task_tree_hooks(hook_type)

    # Demo mode

    def set_demo_sleep_scale(self, scale: float) -> None:
        self._registry.set_demo_sleep_scale(scale)

    def get_demo_sleep_scale(self) -> float:
        return self._registry.get_demo_sleep_scale()

    # API server detection (v2: via environment variable)

    def is_api_configured(self) -> bool:
        """Check if API server is configured via APFLOW_API_SERVER_URL."""
        return bool(os.environ.get("APFLOW_API_SERVER_URL"))

    @property
    def api_server_url(self) -> Optional[str]:
        """Get API server URL from environment."""
        return os.environ.get("APFLOW_API_SERVER_URL")

    def clear(self) -> None:
        self._registry.clear()


_config_manager = ConfigManager()


def get_config_manager() -> ConfigManager:
    return _config_manager


__all__ = ["ConfigManager", "get_config_manager"]
