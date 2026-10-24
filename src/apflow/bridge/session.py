"""Bind each public operation to an isolated session on its application's engine."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable
from typing import Any

from apflow.core.storage.session import session_scope


class SessionBoundModule:
    """Preserve module metadata while rebinding its storage for every invocation."""

    def __init__(self, module: Any, factory: Callable[[], Any], registry: Any = None) -> None:
        self._module = module
        self._factory = factory
        self._registry = registry

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def _bind(self, session: Any) -> Any:
        from apflow.core.execution.task_creator import TaskCreator
        from apflow.core.execution.task_manager import TaskManager
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository

        module = copy.copy(self._module)
        if hasattr(module, "_repo"):
            module._repo = TaskRepository(session)
        if hasattr(module, "_creator"):
            module._creator = TaskCreator(session)
        if hasattr(module, "_manager"):
            module._manager = TaskManager(session)
        return module

    def _context(self, context: Any) -> Any:
        """Carry the registered namespace into nested executor authorization."""
        from apcore import Context

        bound = copy.copy(context) if context is not None else Context.create()
        identifier = getattr(self._module, "module_id", "apflow.task.unknown")
        bound.data = {**bound.data, "apflow_namespace": identifier.rsplit(".", 2)[0]}
        return bound

    async def execute(self, inputs: dict[str, Any], context: Any = None) -> dict[str, Any]:
        from apflow.core.utils.credentials import redact_public_data

        self._check_admission()
        async with session_scope(self._factory) as session:
            return redact_public_data(
                await self._bind(session).execute(inputs, self._context(context))
            )

    def _check_admission(self) -> None:
        """Stop new service work if a required capability becomes unavailable."""
        identifier = getattr(self._module, "module_id", "")
        if self._registry is None or not identifier.endswith(
            (".task.create", ".task.create_tree", ".task.submit", ".run.retry")
        ):
            return
        from apcore.errors import ModuleError
        from apflow.bridge.registry_setup import validate_required_modules
        from apflow.core.config_manager import get_config_manager

        configuration = get_config_manager()
        try:
            validate_required_modules(
                self._registry,
                configuration.get("service.required_modules", []),
                configuration.get("service.required_schema_versions", {}),
            )
        except RuntimeError as error:
            raise ModuleError(
                code="SERVICE_UNAVAILABLE", message="Required service capabilities are unavailable"
            ) from error

    async def stream(self, inputs: dict[str, Any], context: Any) -> AsyncIterator[dict[str, Any]]:
        from apflow.core.utils.credentials import redact_public_data

        self._check_admission()
        async with session_scope(self._factory) as session:
            module = self._bind(session)
            context = self._context(context)
            if hasattr(module, "stream"):
                async for event in module.stream(inputs, context):
                    yield redact_public_data(event)
            else:
                yield redact_public_data(await module.execute(inputs, context))
