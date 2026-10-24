"""Preserve public accounting data and redact secrets on protocol outputs."""

from collections.abc import AsyncIterator
from typing import Any

from apcore import Context, Executor, Registry, redact_sensitive
from apcore.pipeline import ExecutionStrategy, PipelineTrace
from apcore_mcp import APCoreMCP

from apflow.core.utils.credentials import redact_public_data

_USAGE_COUNTERS = frozenset(
    {
        "input",
        "output",
        "total",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    }
)
_ACCOUNTING_COUNTERS = _USAGE_COUNTERS | frozenset(
    {
        "token_budget",
        "limit_tokens",
        "spent_tokens",
        "reserved_tokens",
        "unknown_tokens",
        "remaining_tokens",
        "actual_tokens",
    }
)


def _restore_accounting(source: Any, target: Any) -> Any:
    """Retain SDK name masking except for already sanitized integer accounting data."""
    if isinstance(source, dict) and isinstance(target, dict):
        for key, value in source.items():
            is_counter = key in _ACCOUNTING_COUNTERS and type(value) is int
            is_usage = (
                key in {"token_usage", "reported_usage"}
                and isinstance(value, dict)
                and set(value) <= _USAGE_COUNTERS
                and all(type(item) is int for item in value.values())
            )
            if is_counter or is_usage:
                target[key] = value.copy() if isinstance(value, dict) else value
            else:
                target[key] = _restore_accounting(value, target.get(key))
    elif isinstance(source, list) and isinstance(target, list):
        return [_restore_accounting(before, after) for before, after in zip(source, target)]
    return target


class PublicExecutor:
    """Delegate policy and execution while sanitizing every result and stream chunk."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor

    @property
    def registry(self) -> Registry:
        return self._executor.registry

    def __getattr__(self, name: str) -> Any:
        return getattr(self._executor, name)

    def _redact(self, module_id: str, output: dict[str, Any]) -> dict[str, Any]:
        definition = self.registry.get_definition(module_id)
        schema = definition.output_schema if definition is not None else None
        public = redact_public_data(output, schema)
        masked = redact_sensitive(public, schema or {})
        return _restore_accounting(public, masked)

    async def call_async(
        self,
        module_id: str,
        inputs: dict[str, Any] | None = None,
        context: Context | None = None,
        version_hint: str | None = None,
    ) -> dict[str, Any]:
        result = await self._executor.call_async(module_id, inputs, context, version_hint)
        return self._redact(module_id, result)

    async def stream(
        self,
        module_id: str,
        inputs: dict[str, Any] | None = None,
        context: Context | None = None,
        version_hint: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        source = self._executor.stream(module_id, inputs, context, version_hint)
        try:
            async for chunk in source:
                yield self._redact(module_id, chunk)
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    async def call_async_with_trace(
        self,
        module_id: str,
        inputs: dict[str, Any] | None = None,
        context: Context | None = None,
        *,
        version_hint: str | None = None,
        strategy: ExecutionStrategy | str | None = None,
    ) -> tuple[dict[str, Any], PipelineTrace]:
        output, trace = await self._executor.call_async_with_trace(
            module_id, inputs, context, version_hint=version_hint, strategy=strategy
        )
        return self._redact(module_id, output), trace


class ApflowMCP(APCoreMCP):
    """Use apflow's schema and credential redaction instead of token-name matching."""

    def __init__(self, backend: object, **options: Any) -> None:
        super().__init__(backend, **{**options, "redact_output": False})
        # Wrap after SDK initialization to preserve its ACL, approval, and middleware.
        # The configured executor is exposed read-only by the current SDK.
        self._executor = PublicExecutor(self.executor)
