---
description: Design of the apflow extension registry, executor discovery, metadata, and type-safe extension boundaries.
---

# Extension Registry Design - Protocol-Based Architecture

## Overview

The extension registry uses **Protocol-based design** to avoid circular dependencies while maintaining type safety. This follows the **Dependency Inversion Principle** (SOLID).

## Architecture

### Core Components

1. **`Extension`** (base interface)
   - All extensions must implement this
   - Defines `id`, `category`, `name`, etc.

2. **`ExecutableTask`** (executor interface)
   - Extends `Extension`
   - Defines `execute()`, `get_input_schema()`
   - Located in `core/interfaces/executable_task.py`

3. **`ExecutorLike` Protocol** (structural typing)
   - Defines the "shape" of executors
   - Located in `core/extensions/protocol.py`
   - **No dependency on ExecutableTask**

4. **`ExtensionRegistry`**
   - Uses `ExecutorLike` protocol for type checking
   - **No direct import of ExecutableTask**
   - Located in `core/extensions/registry.py`

### Import Flow (No Circular Dependencies)

```
Extension (base.py)
    ↑
ExecutableTask (interfaces/executable_task.py)
    ↑
ExtensionRegistry (extensions/registry.py)
    ↑ (uses ExecutorLike Protocol, not ExecutableTask)
ExecutorLike Protocol (extensions/protocol.py)
    ↑ (no dependencies)
```

## Key Design Decisions

### 1. Protocol-Based Structural Typing

Instead of importing `ExecutableTask` for type checks, we use `ExecutorLike` protocol:

```python
# ✅ Good: No circular dependency
from apflow.core.extensions.protocol import ExecutorLike

if hasattr(extension, 'execute') and hasattr(extension, 'get_input_schema'):
    # It's an executor-like object
    ...

# ❌ Bad: Would cause circular import
from apflow.core.interfaces.executable_task import ExecutableTask
if isinstance(extension, ExecutableTask):  # Circular!
    ...
```

### 2. Runtime Structural Checks

We use `hasattr()` checks instead of `isinstance()` with Protocol:

```python
# Check if object has required methods (duck typing)
if hasattr(extension, 'execute') and hasattr(extension, 'get_input_schema'):
    # It implements ExecutorLike protocol
    ...
```

This works because:
- Python's duck typing allows any object with the right methods
- Protocol defines the interface without requiring inheritance
- No runtime dependency on ExecutableTask

### 3. Type Hints with Protocol

Type hints use Protocol for better IDE support:

```python
from apflow.core.extensions.protocol import ExecutorFactory

_factory_functions: Dict[str, ExecutorFactory] = {}
```

## Benefits

1. **No Circular Dependencies**
   - `ExtensionRegistry` doesn't import `ExecutableTask`
   - `ExecutableTask` can safely import `Extension`
   - Clean import hierarchy

2. **Type Safety**
   - Protocol provides type hints
   - IDE autocomplete works
   - Static type checkers understand the structure

3. **Flexibility**
   - Any class with `execute()` and `get_input_schema()` can be used
   - Not limited to `ExecutableTask` inheritance
   - Supports duck typing

4. **Maintainability**
   - Clear separation of concerns
   - Easy to understand and modify
   - Follows SOLID principles

## Example Usage

```python
# 1. Define executor (implements ExecutableTask which extends Extension)
from apflow.core.base import BaseTask

class MyExecutor(BaseTask):
    id = "my_executor"
    name = "My Executor"
    
    async def execute(self, inputs):
        return {"result": "done"}
    
    def get_input_schema(self):
        return {}

# 2. Register (automatically via decorator)
from apflow.core.extensions.decorators import extension_register

@extension_register()
class MyExecutor(BaseTask):
    ...

# 3. Use in registry (no ExecutableTask import needed)
from apflow.core.extensions import get_registry

registry = get_registry()
executor = registry.create_executor_instance("my_executor", inputs={})
# executor has execute() and get_input_schema() - that's all we need!
```

## Migration Notes

### Before (Circular Import Problem)

```python
# registry.py
from apflow.core.interfaces.executable_task import ExecutableTask  # ❌

if isinstance(extension, ExecutableTask):  # Circular!
    ...
```

### After (Protocol-Based Solution)

```python
# protocol.py
from typing import Protocol

class ExecutorLike(Protocol):
    async def execute(self, inputs): ...
    def get_input_schema(self): ...

# registry.py
from apflow.core.extensions.protocol import ExecutorLike  # ✅

if hasattr(extension, 'execute') and hasattr(extension, 'get_input_schema'):
    # No circular dependency!
    ...
```

## Testing

All imports work without circular dependencies:

```python
# ✅ All these imports work simultaneously
from apflow.core.extensions import ExtensionRegistry
from apflow.core.interfaces import ExecutableTask
from apflow.core.extensions.protocol import ExecutorLike
from apflow.extensions.http import RestExecutor
```

## Future Extensions

This pattern can be extended for other extension types:

```python
class StorageLike(Protocol):
    def connect(self): ...
    def disconnect(self): ...

class HookLike(Protocol):
    def before_execute(self): ...
    def after_execute(self): ...
```

