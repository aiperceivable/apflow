# Feature Spec: Function Executor

**Feature ID:** F-011
**Priority:** P0
**Phase:** 0.21.0

---

## Purpose

The current executor registration pattern requires writing a full `BaseTask` subclass with class attributes (`id`, `name`, `description`, `inputs_schema`, `outputs_schema`) and an `async execute()` method. This is unnecessarily heavy for simple, stateless use cases like "call an API and return the result."

The `@function_executor` decorator eliminates this boilerplate by wrapping any async (or sync) callable as a fully registered executor. The generated executor is discoverable by the bridge, exposable via MCP/A2A/CLI, and referenceable by `id` in task trees -- identical in behavior to a hand-written `BaseTask` subclass.

## File Changes

### New Files

#### `src/apflow/adapters/__init__.py`

```python
"""Adapter utilities for simplified executor registration."""

from apflow.adapters.function_executor import function_executor

__all__ = ["function_executor"]
```

#### `src/apflow/adapters/function_executor.py`

```python
"""
Decorator that wraps a plain function as a registered ExecutableTask.

Generates a BaseTask subclass dynamically, registers it via executor_register,
and returns the original function unchanged.
"""

import asyncio
import inspect
from typing import Any, Callable, Dict, Optional, Type, Union

from pydantic import BaseModel

from apflow.core.base import BaseTask
from apflow.core.extensions.decorators import executor_register
from apflow.logger import get_logger

logger = get_logger(__name__)

# Module-level registry so the bridge can discover function executors
_function_executor_classes: Dict[str, type] = {}


def function_executor(
    id: str,
    description: str,
    name: Optional[str] = None,
    input_schema: Optional[Dict[str, Any]] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    input_model: Optional[Type[BaseModel]] = None,
    output_model: Optional[Type[BaseModel]] = None,
    tags: Optional[list[str]] = None,
    override: bool = False,
) -> Callable:
    ...


def _resolve_schema(
    explicit_schema: Optional[Dict[str, Any]],
    pydantic_model: Optional[Type[BaseModel]],
) -> Optional[Union[Type[BaseModel], Dict[str, Any]]]:
    ...


def _build_executor_class(
    executor_id: str,
    executor_name: str,
    executor_description: str,
    fn: Callable,
    inputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]],
    outputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]],
    tags: list[str],
) -> type:
    ...


def get_function_executor_classes() -> Dict[str, type]:
    ...
```

### Modified Files

#### `src/apflow/core/decorators.py`

Add re-export of `function_executor`:

```python
from apflow.adapters.function_executor import function_executor

__all__ = [
    ...
    "function_executor",
]
```

#### `src/apflow/__init__.py`

Add `function_executor` to `__all__` and the lazy-import block so users can write `from apflow import function_executor`.

#### `src/apflow/bridge/scanner_bridge.py` -- `discover_executor_modules()`

After the AST-scanned executors loop, append function executors from the runtime registry:

```python
from apflow.adapters.function_executor import get_function_executor_classes

# ... existing AST scan code ...

# Append runtime-registered function executors
for executor_id, executor_class in get_function_executor_classes().items():
    if executor_id not in seen_ids:
        adapter = _create_adapter_from_class(executor_id, executor_class)
        if adapter is not None:
            adapters.append(adapter)
```

A new helper `_create_adapter_from_class` instantiates `ExecutableTaskModuleAdapter` directly from the class (no AST metadata needed) by reading `id`, `name`, `description`, `inputs_schema`, `outputs_schema` from the class attributes.

## Method Signatures

### `function_executor(...)` (decorator factory)

```python
def function_executor(
    id: str,
    description: str,
    name: Optional[str] = None,
    input_schema: Optional[Dict[str, Any]] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    input_model: Optional[Type[BaseModel]] = None,
    output_model: Optional[Type[BaseModel]] = None,
    tags: Optional[list[str]] = None,
    override: bool = False,
) -> Callable:
```

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `id` | `str` | Yes | Unique executor identifier (used in task trees) |
| `description` | `str` | Yes | Human-readable description (exposed to AI agents) |
| `name` | `str` | No | Display name; defaults to `id` title-cased |
| `input_schema` | `dict` | No | JSON Schema dict for inputs |
| `output_schema` | `dict` | No | JSON Schema dict for outputs |
| `input_model` | `Type[BaseModel]` | No | Pydantic model for inputs (mutually exclusive with `input_schema`) |
| `output_model` | `Type[BaseModel]` | No | Pydantic model for outputs (mutually exclusive with `output_schema`) |
| `tags` | `list[str]` | No | Tags for categorization |
| `override` | `bool` | No | Force re-registration if id already exists |

**Behavior:**

1. Validates that `id` and `description` are non-empty strings.
2. Validates mutual exclusivity: `input_schema` and `input_model` cannot both be provided (same for output).
3. Calls `_resolve_schema()` to normalize schema arguments.
4. Calls `_build_executor_class()` to create a `BaseTask` subclass via `type()`.
5. Applies `@executor_register(override=override)` to the generated class.
6. Stores the class in `_function_executor_classes[id]` for bridge discovery.
7. Returns the original function unchanged.

### `_resolve_schema(...)`

```python
def _resolve_schema(
    explicit_schema: Optional[Dict[str, Any]],
    pydantic_model: Optional[Type[BaseModel]],
) -> Optional[Union[Type[BaseModel], Dict[str, Any]]]:
```

Returns `pydantic_model` if provided, `explicit_schema` if provided, or `None` if neither. Raises `ValueError` if both are provided.

### `_build_executor_class(...)`

```python
def _build_executor_class(
    executor_id: str,
    executor_name: str,
    executor_description: str,
    fn: Callable,
    inputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]],
    outputs_schema: Optional[Union[Type[BaseModel], Dict[str, Any]]],
    tags: list[str],
) -> type:
```

Uses `type(class_name, (BaseTask,), attrs)` to create a dynamic subclass. The `execute` method on the generated class:

- If `fn` is a coroutine function (`inspect.iscoroutinefunction`), calls `await fn(inputs)`.
- If `fn` is a sync function, calls `await asyncio.to_thread(fn, inputs)`.

The generated class name is derived from `executor_id` (e.g., `"analyze_pr"` becomes `"AnalyzePrFunctionExecutor"`).

### `get_function_executor_classes()`

```python
def get_function_executor_classes() -> Dict[str, type]:
```

Returns a copy of `_function_executor_classes`. Used by the bridge to discover runtime-registered function executors that are invisible to the AST scanner.

### `_create_adapter_from_class(...)` (new helper in `scanner_bridge.py`)

```python
def _create_adapter_from_class(
    executor_id: str, executor_class: type
) -> ExecutableTaskModuleAdapter | None:
```

Extracts `name`, `description`, `tags` from class attributes. Calls `_extract_schema()` for input/output schemas. Returns an `ExecutableTaskModuleAdapter` instance.

## Data Models

None. This feature reuses existing `BaseTask`, `ExecutableTaskModuleAdapter`, and the extension registry. No new data models are introduced.

## Test Requirements

Test file: `tests/adapters/test_function_executor.py`

### Unit Tests

```python
import pytest
from apflow.adapters.function_executor import (
    function_executor,
    get_function_executor_classes,
    _resolve_schema,
    _build_executor_class,
)


class TestFunctionExecutorDecorator:
    """Tests for the @function_executor decorator."""

    def test_decorator_returns_original_function(self):
        """Decorated function should remain callable as a plain function."""

        @function_executor(id="test_returns_fn", description="test")
        async def my_func(inputs: dict) -> dict:
            return {"ok": True}

        assert callable(my_func)
        assert not isinstance(my_func, type)

    def test_decorator_registers_in_extension_registry(self):
        """Generated class should be discoverable in the extension registry."""

        @function_executor(id="test_registry", description="test")
        async def my_func(inputs: dict) -> dict:
            return {}

        from apflow.core.extensions.registry import get_registry
        entry = get_registry().get_by_id("test_registry")
        assert entry is not None

    def test_decorator_stores_in_function_executor_classes(self):
        """Generated class should appear in get_function_executor_classes()."""

        @function_executor(id="test_stored", description="test")
        async def my_func(inputs: dict) -> dict:
            return {}

        classes = get_function_executor_classes()
        assert "test_stored" in classes

    @pytest.mark.asyncio
    async def test_async_function_execution(self):
        """Async function should execute correctly via the generated class."""

        @function_executor(id="test_async_exec", description="test")
        async def my_func(inputs: dict) -> dict:
            return {"value": inputs.get("x", 0) + 1}

        cls = get_function_executor_classes()["test_async_exec"]
        instance = cls()
        result = await instance.execute({"x": 5})
        assert result == {"value": 6}

    @pytest.mark.asyncio
    async def test_sync_function_wrapped_in_thread(self):
        """Sync function should be wrapped in asyncio.to_thread."""

        @function_executor(id="test_sync_exec", description="test")
        def my_sync_func(inputs: dict) -> dict:
            return {"sync": True}

        cls = get_function_executor_classes()["test_sync_exec"]
        instance = cls()
        result = await instance.execute({})
        assert result == {"sync": True}

    def test_explicit_json_schema(self):
        """Explicit JSON Schema dicts should be set on the generated class."""
        in_schema = {"type": "object", "properties": {"url": {"type": "string"}}}
        out_schema = {"type": "object", "properties": {"data": {"type": "string"}}}

        @function_executor(
            id="test_json_schema",
            description="test",
            input_schema=in_schema,
            output_schema=out_schema,
        )
        async def my_func(inputs: dict) -> dict:
            return {}

        cls = get_function_executor_classes()["test_json_schema"]
        assert cls.inputs_schema == in_schema
        assert cls.outputs_schema == out_schema

    def test_pydantic_model_schema(self):
        """Pydantic models should be set as inputs_schema / outputs_schema."""
        from pydantic import BaseModel

        class In(BaseModel):
            url: str

        class Out(BaseModel):
            data: str

        @function_executor(
            id="test_pydantic_schema",
            description="test",
            input_model=In,
            output_model=Out,
        )
        async def my_func(inputs: dict) -> dict:
            return {}

        cls = get_function_executor_classes()["test_pydantic_schema"]
        assert cls.inputs_schema is In
        assert cls.outputs_schema is Out

    def test_mutual_exclusivity_raises(self):
        """Providing both input_schema and input_model should raise ValueError."""
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        with pytest.raises(ValueError, match="mutually exclusive"):

            @function_executor(
                id="test_exclusive",
                description="test",
                input_schema={"type": "object"},
                input_model=M,
            )
            async def my_func(inputs: dict) -> dict:
                return {}

    def test_empty_id_raises(self):
        """Empty id should raise ValueError."""
        with pytest.raises(ValueError):

            @function_executor(id="", description="test")
            async def my_func(inputs: dict) -> dict:
                return {}

    def test_name_defaults_to_title_cased_id(self):
        """Name should default to title-cased id when not provided."""

        @function_executor(id="analyze_pr", description="test")
        async def my_func(inputs: dict) -> dict:
            return {}

        cls = get_function_executor_classes()["analyze_pr"]
        assert cls.name == "Analyze Pr"

    def test_custom_name(self):
        """Explicit name should override the default."""

        @function_executor(id="test_custom_name", description="test", name="My Custom Name")
        async def my_func(inputs: dict) -> dict:
            return {}

        cls = get_function_executor_classes()["test_custom_name"]
        assert cls.name == "My Custom Name"


class TestResolveSchema:
    """Tests for _resolve_schema helper."""

    def test_returns_none_when_both_none(self):
        assert _resolve_schema(None, None) is None

    def test_returns_dict_schema(self):
        schema = {"type": "object"}
        assert _resolve_schema(schema, None) == schema

    def test_returns_pydantic_model(self):
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        assert _resolve_schema(None, M) is M

    def test_raises_when_both_provided(self):
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        with pytest.raises(ValueError):
            _resolve_schema({"type": "object"}, M)


class TestBridgeIntegration:
    """Tests that function executors are discoverable by the bridge."""

    def test_function_executor_appears_in_discover_executor_modules(self):
        """Registered function executor should appear in bridge discovery."""

        @function_executor(id="test_bridge_discovery", description="bridge test")
        async def my_func(inputs: dict) -> dict:
            return {}

        from apflow.bridge.scanner_bridge import discover_executor_modules

        modules = discover_executor_modules()
        ids = [m.executor_id for m in modules]
        assert "test_bridge_discovery" in ids
```

### Coverage Target

- 90%+ line coverage on `src/apflow/adapters/function_executor.py`.
- All error paths (validation failures, schema conflicts) tested.
- Both sync and async function execution tested.

## Acceptance Criteria

1. `@function_executor(id="x", description="y")` on an async function creates a registered `BaseTask` subclass and returns the original function unchanged.
2. `@function_executor` on a sync function wraps execution in `asyncio.to_thread`.
3. Explicit `input_schema`/`output_schema` (JSON Schema dict) are set on the generated class.
4. Explicit `input_model`/`output_model` (Pydantic `BaseModel`) are set on the generated class as `inputs_schema`/`outputs_schema`.
5. Providing both `input_schema` and `input_model` (or both output variants) raises `ValueError`.
6. Empty `id` or `description` raises `ValueError`.
7. The generated class is registered in `ExtensionRegistry` and discoverable via `get_registry().get_by_id(id)`.
8. `discover_executor_modules()` in the bridge includes function executors alongside AST-scanned executors.
9. Function executors can be referenced by `id` in task trees and executed by `TaskManager`.
10. No changes to `ExecutableTask`, `BaseTask`, or `ExecutableTaskModuleAdapter` are required.
11. All tests pass; `ruff check`, `black`, and `pyright` report zero errors.
