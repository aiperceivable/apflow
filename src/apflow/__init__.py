"""
apflow - AI Agent Production Middleware

Framework-agnostic production middleware that makes AI agents reliable,
cost-governed, and auditable.

Core modules (always included):
- core.interfaces: Core interfaces (ExecutableTask, BaseTask)
- core.execution: Task orchestration (TaskManager, StreamingCallbacks)
- core.extensions: Unified extension system (ExtensionRegistry)
- core.storage: Database session factory (SQLite default, PostgreSQL optional)
- bridge: apcore Module registration (auto-discovery of executors)
- durability: Checkpoint/resume, retry with backoff, circuit breaker
- governance: Token budget, cost policy, model downgrade, usage reporting

Protocol integration via apcore ecosystem:
- apcore-mcp: Model Context Protocol server [mcp-server]
- apcore-a2a: Agent-to-Agent Protocol server [a2a-server]
- apcore-cli: Command-line interface [cli-gen]
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _get_version

try:
    __version__ = _get_version("apflow")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    # Core framework
    "ExecutableTask",
    "BaseTask",
    "TaskManager",
    "StreamingCallbacks",
    "create_session",
    "get_default_session",
    "get_hook_session",
    "get_hook_repository",
    # Unified decorators
    "register_pre_hook",
    "register_post_hook",
    "register_task_tree_hook",
    "get_task_tree_hooks",
    "set_task_model_class",
    "get_task_model_class",
    "task_model_register",
    "clear_config",
    "set_use_task_creator",
    "get_use_task_creator",
    "set_require_existing_tasks",
    "get_require_existing_tasks",
    "register_webhook_verify_hook",
    "get_webhook_verify_hook",
    "WebhookVerifyContext",
    "WebhookVerifyResult",
    "executor_register",
    "storage_register",
    "hook_register",
    # Extension utilities
    "add_executor_hook",
    # Version
    "__version__",
]


def __getattr__(name):
    """Lazy import to avoid loading heavy apflow.core at package import time"""

    # Core interfaces
    if name in (
        "ExecutableTask",
        "BaseTask",
        "TaskManager",
        "StreamingCallbacks",
        "create_session",
        "get_default_session",
        "get_hook_session",
        "get_hook_repository",
    ):
        from apflow.core import (
            ExecutableTask,  # noqa: F401
            BaseTask,  # noqa: F401
            TaskManager,  # noqa: F401
            StreamingCallbacks,  # noqa: F401
            create_session,  # noqa: F401
            get_default_session,  # noqa: F401
            get_hook_session,  # noqa: F401
            get_hook_repository,  # noqa: F401
        )

        return locals()[name]

    # Decorators
    if name in (
        "register_pre_hook",
        "register_post_hook",
        "register_task_tree_hook",
        "get_task_tree_hooks",
        "set_task_model_class",
        "get_task_model_class",
        "task_model_register",
        "clear_config",
        "set_use_task_creator",
        "get_use_task_creator",
        "set_require_existing_tasks",
        "get_require_existing_tasks",
        "register_webhook_verify_hook",
        "get_webhook_verify_hook",
        "executor_register",
        "storage_register",
        "hook_register",
    ):
        from apflow.core.decorators import (
            register_pre_hook,  # noqa: F401
            register_post_hook,  # noqa: F401
            register_task_tree_hook,  # noqa: F401
            get_task_tree_hooks,  # noqa: F401
            set_task_model_class,  # noqa: F401
            get_task_model_class,  # noqa: F401
            task_model_register,  # noqa: F401
            clear_config,  # noqa: F401
            set_use_task_creator,  # noqa: F401
            get_use_task_creator,  # noqa: F401
            set_require_existing_tasks,  # noqa: F401
            get_require_existing_tasks,  # noqa: F401
            register_webhook_verify_hook,  # noqa: F401
            get_webhook_verify_hook,  # noqa: F401
            executor_register,  # noqa: F401
            storage_register,  # noqa: F401
            hook_register,  # noqa: F401
        )

        return locals()[name]

    # Core types
    if name in ("WebhookVerifyContext", "WebhookVerifyResult"):
        from apflow.core.types import (
            WebhookVerifyContext,  # noqa: F401
            WebhookVerifyResult,  # noqa: F401
        )

        return locals()[name]

    # Extension utilities
    if name == "add_executor_hook":
        from apflow.core.extensions import add_executor_hook

        return add_executor_hook

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
