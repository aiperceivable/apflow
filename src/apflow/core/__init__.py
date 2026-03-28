"""
Core orchestration framework modules

This module contains all core framework components for task orchestration:
- interfaces/: Core interfaces (ExecutableTask) - abstract contracts
- base/: Base class implementations (BaseTask) - common functionality
- execution/: Task orchestration (TaskManager, StreamingCallbacks)
- storage/: Storage implementation (SQLite default, PostgreSQL optional)
- types.py: Core type definitions (TaskTreeNode, TaskStatus, hooks)
- utils/: Utility functions

All core modules are always included (pip install apflow).
No optional dependencies required.

Note: TaskCreator (core) creates tasks from tasks array.
Note: Protocol specifications are handled by A2A Protocol (Agent-to-Agent Protocol),
which is the standard protocol for agent communication. See api/ module for A2A implementation.

Performance: Uses lazy imports to avoid loading heavy modules at package import time.
"""

__all__ = [
    # Base interfaces
    "ExecutableTask",
    "BaseTask",
    # Core types
    "TaskTreeNode",
    "TaskPreHook",
    "TaskPostHook",
    "TaskStatus",
    # Execution
    "TaskManager",
    "TaskCreator",
    "StreamingCallbacks",
    "TaskBuilder",
    # Extensions
    "Extension",
    "ExtensionCategory",
    "ExtensionRegistry",
    "get_registry",
    "register_extension",
    # Unified Decorators (Flask-style API)
    "register_pre_hook",
    "register_post_hook",
    "set_task_model_class",
    "get_task_model_class",
    "clear_config",
    "set_use_task_creator",
    "get_use_task_creator",
    "set_require_existing_tasks",
    "get_require_existing_tasks",
    "executor_register",
    "storage_register",
    "hook_register",
    # Configuration Registry (internal)
    "get_pre_hooks",
    "get_post_hooks",
    # Storage
    "create_session",
    "get_default_session",
    "get_hook_session",
    "get_hook_repository",
]


def __getattr__(name):
    """Lazy import to avoid loading heavy modules at package import time"""
    # Base interfaces
    if name == "ExecutableTask":
        from apflow.core.interfaces import ExecutableTask

        return ExecutableTask
    elif name == "BaseTask":
        from apflow.core.base import BaseTask

        return BaseTask

    # Core types
    elif name in ("TaskTreeNode", "TaskPreHook", "TaskPostHook", "TaskStatus"):
        from apflow.core.types import (
            TaskTreeNode,  # noqa: F401
            TaskPreHook,  # noqa: F401
            TaskPostHook,  # noqa: F401
            TaskStatus,  # noqa: F401
        )

        return locals()[name]

    # Execution
    elif name in ("TaskManager", "TaskCreator", "StreamingCallbacks"):
        from apflow.core.execution import TaskManager, TaskCreator, StreamingCallbacks  # noqa: F401

        return locals()[name]
    elif name == "TaskBuilder":
        from apflow.core.builders import TaskBuilder

        return TaskBuilder

    # Extensions
    elif name in (
        "Extension",
        "ExtensionCategory",
        "ExtensionRegistry",
        "get_registry",
        "register_extension",
        "executor_register",
        "storage_register",
        "hook_register",
    ):
        from apflow.core.extensions import (
            Extension,  # noqa: F401
            ExtensionCategory,  # noqa: F401
            ExtensionRegistry,  # noqa: F401
            get_registry,  # noqa: F401
            register_extension,  # noqa: F401
            executor_register,  # noqa: F401
            storage_register,  # noqa: F401
            hook_register,  # noqa: F401
        )

        return locals()[name]

    # Decorators
    elif name in (
        "register_pre_hook",
        "register_post_hook",
        "set_task_model_class",
        "get_task_model_class",
        "clear_config",
        "set_use_task_creator",
        "get_use_task_creator",
        "set_require_existing_tasks",
        "get_require_existing_tasks",
    ):
        from apflow.core.decorators import (
            register_pre_hook,  # noqa: F401
            register_post_hook,  # noqa: F401
            set_task_model_class,  # noqa: F401
            get_task_model_class,  # noqa: F401
            clear_config,  # noqa: F401
            set_use_task_creator,  # noqa: F401
            get_use_task_creator,  # noqa: F401
            set_require_existing_tasks,  # noqa: F401
            get_require_existing_tasks,  # noqa: F401
        )

        return locals()[name]

    # Configuration
    elif name in ("get_pre_hooks", "get_post_hooks"):
        from apflow.core.config import get_pre_hooks, get_post_hooks  # noqa: F401

        return locals()[name]

    # Storage
    elif name in (
        "create_session",
        "get_default_session",
        "get_hook_session",
        "get_hook_repository",
    ):
        from apflow.core.storage import (
            create_session,  # noqa: F401
            get_default_session,  # noqa: F401
            get_hook_session,  # noqa: F401
            get_hook_repository,  # noqa: F401
        )

        return locals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
