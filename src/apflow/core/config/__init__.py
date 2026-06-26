"""
Configuration and registry for global settings

This module provides a centralized way to manage global configuration
like task model class, hooks, and other cross-cutting concerns.

Instead of passing parameters through multiple layers, components can
access configuration through this module.
"""

from apflow.core.config.registry import (
    get_config,
    register_pre_hook,
    register_post_hook,
    set_task_model_class,
    get_task_model_class,
    get_pre_hooks,
    get_post_hooks,
    clear_config,
    set_use_task_creator,
    get_use_task_creator,
    set_require_existing_tasks,
    get_require_existing_tasks,
    register_task_tree_hook,
    get_task_tree_hooks,
    task_model_register,
    set_demo_sleep_scale,
    get_demo_sleep_scale,
    register_webhook_verify_hook,
    get_webhook_verify_hook,
    set_policy_engine,
    get_policy_engine,
    set_circuit_breaker_registry,
    get_circuit_breaker_registry,
    set_governance_enabled,
    is_governance_enabled,
    set_durability_enabled,
    is_durability_enabled,
)

__all__ = [
    "get_config",
    "register_pre_hook",
    "register_post_hook",
    "set_task_model_class",
    "get_task_model_class",
    "get_pre_hooks",
    "get_post_hooks",
    "clear_config",
    "set_use_task_creator",
    "get_use_task_creator",
    "set_require_existing_tasks",
    "get_require_existing_tasks",
    "register_task_tree_hook",
    "get_task_tree_hooks",
    "task_model_register",
    "set_demo_sleep_scale",
    "get_demo_sleep_scale",
    "register_webhook_verify_hook",
    "get_webhook_verify_hook",
    "set_policy_engine",
    "get_policy_engine",
    "set_circuit_breaker_registry",
    "get_circuit_breaker_registry",
    "set_governance_enabled",
    "is_governance_enabled",
    "set_durability_enabled",
    "is_durability_enabled",
]
