# Feature Spec: apcore Module Bridge (F-002)

**Feature ID:** F-002
**Priority:** P0
**Phase:** Phase 1 (0.20.0-alpha.1)
**Tech Design Reference:** Section 4.3

---

## Purpose

Register apflow's capabilities as apcore Modules so that apcore-mcp, apcore-a2a, and apcore-cli automatically expose them without any protocol-specific code in apflow. Uses the existing AST extension scanner for zero-configuration discovery of executors.

---

## File Changes

### New Files

**`src/apflow/bridge/__init__.py`**

```python
"""apcore Module Bridge -- registers apflow capabilities as apcore Modules."""

from apflow.bridge.registry_setup import create_apflow_registry

__all__ = ["create_apflow_registry"]
```

**`src/apflow/bridge/module_adapter.py`**

Contains `ExecutableTaskModuleAdapter` class and `ModuleAnnotations` dataclass.

**`src/apflow/bridge/scanner_bridge.py`**

Contains `discover_executor_modules()` function and `_create_adapter_from_metadata()` helper.

**`src/apflow/bridge/task_modules.py`**

Contains `TaskCreateModule`, `TaskExecuteModule`, `TaskListModule`, `TaskGetModule`, `TaskDeleteModule` classes.

**`src/apflow/bridge/registry_setup.py`**

Contains `create_apflow_registry()` function.

### Test Files

```
tests/bridge/__init__.py
tests/bridge/test_module_adapter.py
tests/bridge/test_scanner_bridge.py
tests/bridge/test_task_modules.py
tests/bridge/test_registry_setup.py
```

---

## Method Signatures

### `ExecutableTaskModuleAdapter`

```python
class ExecutableTaskModuleAdapter:
    def __init__(
        self,
        executor_class: type,
        executor_id: str,           # Non-empty string
        executor_name: str,
        executor_description: str,
        input_schema: Dict[str, Any],   # Valid JSON Schema dict
        output_schema: Dict[str, Any],  # Valid JSON Schema dict
        tags: list[str],
        dependencies: list[str],
        always_available: bool,
    ) -> None: ...

    # Plain attributes (not properties) — apcore may set these during registration
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    description: str
    annotations: ModuleAnnotations

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        """Execute the wrapped executor.
        Logic:
        1. Validate inputs is a dict.
        2. Try executor_class() (no args).
        3. If TypeError, try executor_class(inputs={}).
        4. Call await executor.execute(inputs).
        5. Return result dict.
        Raises: TypeError if inputs is not dict. RuntimeError on executor failure.
        """
        ...
```

### `discover_executor_modules()`

```python
def discover_executor_modules() -> list[ExecutableTaskModuleAdapter]:
    """Discover all registered executors and create Module adapters.
    Logic:
    1. Call ExtensionScanner.scan_builtin_executors() to get metadata map.
    2. For each executor metadata:
       a. Call _create_adapter_from_metadata(metadata).
       b. If successful, append to adapters list.
       c. If failed, log error and continue.
    3. Return adapters list.
    """
    ...
```

### `_create_adapter_from_metadata()`

```python
def _create_adapter_from_metadata(
    metadata: ExecutorMetadata,
) -> ExecutableTaskModuleAdapter | None:
    """Create adapter from AST-scanned metadata.
    Logic:
    1. importlib.import_module(metadata.module_path).
    2. getattr(module, metadata.class_name).
    3. Try to create template instance for schema extraction:
       a. Try executor_class(inputs={}).
       b. If fails, create minimal subclass with no-op __init__.
    4. Call template.get_input_schema() (fallback: {"type": "object", "properties": {}}).
    5. Call template.get_output_schema() (fallback: {"type": "object", "properties": {}}).
    6. Return ExecutableTaskModuleAdapter with all extracted data.
    Returns None if import fails.
    """
    ...
```

### `TaskCreateModule`

```python
class TaskCreateModule:
    description: str = "Create a new task in the apflow task engine."
    input_schema: Dict[str, Any]   # JSON Schema with: name (required), inputs, params,
                                    # parent_id, priority (0-3), dependencies, token_budget,
                                    # cost_policy, max_attempts (1-100), backoff_strategy
    output_schema: Dict[str, Any]  # JSON Schema with: id (required), name, status, created_at

    def __init__(self, task_creator: Any, task_repository: Any) -> None: ...

    async def execute(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """Create a task.
        Logic:
        1. Extract 'name' from input. Validate non-empty.
        2. Build task_data dict from input fields, remove None values.
        3. Call task_creator.create_task_trees_from_array([task_data]).
        4. Return {id, name, status, created_at} from first tree's root task.
        Raises: ValueError if name is missing or empty.
        """
        ...
```

### `TaskExecuteModule`

```python
class TaskExecuteModule:
    description: str = "Execute an existing task in the apflow task engine."
    input_schema: Dict[str, Any]   # {task_id: required, minLength: 1}
    output_schema: Dict[str, Any]  # {task_id, status, result, token_usage}

    def __init__(self, task_manager: Any) -> None: ...

    async def execute(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task by ID.
        Logic:
        1. Extract task_id. Validate non-empty.
        2. Call task_manager.execute_task(task_id).
        3. Return result.
        Raises: ValueError if task_id empty.
        """
        ...
```

### `TaskListModule`

```python
class TaskListModule:
    description: str = "List tasks from the apflow task engine with optional filtering."
    input_schema: Dict[str, Any]   # {status: enum, user_id, limit: 1-1000 default 50, offset: >=0}
    output_schema: Dict[str, Any]  # {tasks: array, total: int}

    def __init__(self, task_repository: Any) -> None: ...

    async def execute(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """List tasks.
        Logic:
        1. Clamp limit to [1, 1000], default 50.
        2. Clamp offset to >= 0, default 0.
        3. Call task_repository.list_tasks() with filters.
        4. Call task_repository.count_tasks() for total.
        5. Return {tasks: [{id, name, status, created_at}], total}.
        """
        ...
```

### `TaskGetModule`

```python
class TaskGetModule:
    description: str = "Get detailed information about a specific task."
    input_schema: Dict[str, Any]   # {task_id: required, minLength: 1}
    output_schema: Dict[str, Any]  # Full task dict

    def __init__(self, task_repository: Any) -> None: ...

    async def execute(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """Get task by ID.
        Logic:
        1. Extract task_id. Validate non-empty.
        2. Call task_repository.get_task_by_id(task_id).
        3. If None, raise KeyError.
        4. Return task.to_dict().
        Raises: ValueError if empty, KeyError if not found.
        """
        ...
```

### `TaskDeleteModule`

```python
class TaskDeleteModule:
    description: str = "Delete a task from the apflow task engine."
    input_schema: Dict[str, Any]   # {task_id: required, minLength: 1}
    output_schema: Dict[str, Any]  # {task_id, deleted: bool}

    def __init__(self, task_repository: Any) -> None: ...

    async def execute(self, input: Dict[str, Any]) -> Dict[str, Any]:
        """Delete task.
        Logic:
        1. Extract task_id. Validate non-empty.
        2. Verify task exists via get_task_by_id. If not, raise KeyError.
        3. Call task_repository.delete_task(task_id).
        4. Return {task_id, deleted: True}.
        Raises: ValueError if empty, KeyError if not found.
        """
        ...
```

### `create_apflow_registry()`

```python
def create_apflow_registry(
    task_manager: Any,
    task_creator: Any,
    task_repository: Any,
    namespace: str = "apflow",  # Non-empty, default "apflow"
) -> Registry:
    """Create and populate an apcore Registry with all apflow modules.
    Logic:
    1. Create APCore() client.
    2. Access client.registry (APCore provides registry directly, no create_registry() needed).
    3. Call discover_executor_modules() to get executor adapters.
    4. For each adapter, call client.register(f"{namespace}.{adapter._executor_id}", adapter).
    5. Create TaskCreateModule, TaskExecuteModule, TaskListModule, TaskGetModule, TaskDeleteModule.
    6. Register each as f"{namespace}.task.{action}".
    7. Log total module count.
    8. Return registry.
    Raises: RuntimeError if APCore initialization fails.
    """
    ...
```

---

## Data Models

No data model changes. This feature adds a bridge layer without modifying existing data structures.

---

## Test Requirements

### Unit Tests: `tests/bridge/test_module_adapter.py`

```python
def test_adapter_creation_valid():
    """Adapter initializes with valid parameters."""
    adapter = ExecutableTaskModuleAdapter(
        executor_class=MockExecutor,
        executor_id="mock_executor",
        executor_name="Mock Executor",
        executor_description="A mock executor for testing",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        tags=["test"],
        dependencies=[],
        always_available=True,
    )
    assert adapter.description == "A mock executor for testing"
    assert adapter.input_schema["properties"]["query"]["type"] == "string"

def test_adapter_creation_empty_id_raises():
    """Empty executor_id raises ValueError."""
    with pytest.raises(ValueError, match="non-empty"):
        ExecutableTaskModuleAdapter(executor_class=MockExecutor, executor_id="", ...)

def test_adapter_creation_non_class_raises():
    """Non-class executor_class raises TypeError."""
    with pytest.raises(TypeError, match="must be a class"):
        ExecutableTaskModuleAdapter(executor_class="not_a_class", ...)

async def test_adapter_execute_delegates():
    """execute() creates instance and delegates to executor.execute()."""
    adapter = ExecutableTaskModuleAdapter(executor_class=MockExecutor, ...)
    result = await adapter.execute({"query": "test"})
    assert "result" in result

async def test_adapter_execute_non_dict_raises():
    """execute() raises TypeError for non-dict input."""
    adapter = ExecutableTaskModuleAdapter(executor_class=MockExecutor, ...)
    with pytest.raises(TypeError, match="must be a dict"):
        await adapter.execute("not a dict")

def test_adapter_annotations():
    """annotations property returns correct metadata."""
    adapter = ExecutableTaskModuleAdapter(
        executor_class=MockExecutor,
        executor_id="mock",
        ...,
        tags=["http", "api"],
        dependencies=["httpx"],
        always_available=False,
    )
    assert adapter.annotations.executor_id == "mock"
    assert adapter.annotations.tags == ["http", "api"]
    assert adapter.annotations.dependencies == ["httpx"]
```

### Unit Tests: `tests/bridge/test_scanner_bridge.py`

```python
def test_discover_executor_modules_returns_list():
    """discover_executor_modules returns a list of adapters."""
    adapters = discover_executor_modules()
    assert isinstance(adapters, list)
    assert len(adapters) > 0  # At least REST, SSH, Docker, etc.

def test_discover_executor_modules_has_rest():
    """REST executor is discovered."""
    adapters = discover_executor_modules()
    ids = [a._executor_id for a in adapters]
    assert "rest_executor" in ids

def test_discover_handles_import_failure(monkeypatch):
    """Import failure for one executor does not block others."""
    # Monkeypatch importlib.import_module to fail for one specific module
    # Verify other executors still discovered
```

### Unit Tests: `tests/bridge/test_task_modules.py`

```python
async def test_task_create_module_valid():
    """TaskCreateModule creates a task with valid input."""
    module = TaskCreateModule(mock_creator, mock_repo)
    result = await module.execute({"name": "test_task"})
    assert "id" in result
    assert result["name"] == "test_task"
    assert result["status"] == "pending"

async def test_task_create_module_empty_name_raises():
    """TaskCreateModule raises ValueError for empty name."""
    module = TaskCreateModule(mock_creator, mock_repo)
    with pytest.raises(ValueError, match="non-empty"):
        await module.execute({"name": ""})

async def test_task_create_module_missing_name_raises():
    """TaskCreateModule raises ValueError when name is missing."""
    module = TaskCreateModule(mock_creator, mock_repo)
    with pytest.raises(ValueError, match="non-empty"):
        await module.execute({})

async def test_task_execute_module_valid():
    """TaskExecuteModule executes a task by ID."""
    module = TaskExecuteModule(mock_manager)
    result = await module.execute({"task_id": "abc-123"})
    assert "task_id" in result

async def test_task_execute_module_empty_id_raises():
    """TaskExecuteModule raises ValueError for empty task_id."""
    module = TaskExecuteModule(mock_manager)
    with pytest.raises(ValueError):
        await module.execute({"task_id": ""})

async def test_task_list_module_defaults():
    """TaskListModule uses default limit=50, offset=0."""
    module = TaskListModule(mock_repo)
    result = await module.execute({})
    assert "tasks" in result
    assert "total" in result

async def test_task_list_module_clamps_limit():
    """TaskListModule clamps limit to [1, 1000]."""
    module = TaskListModule(mock_repo)
    # limit=0 should become 1
    await module.execute({"limit": 0})
    # limit=5000 should become 1000
    await module.execute({"limit": 5000})

async def test_task_get_module_not_found():
    """TaskGetModule raises KeyError for non-existent task."""
    module = TaskGetModule(mock_repo_returning_none)
    with pytest.raises(KeyError, match="not found"):
        await module.execute({"task_id": "nonexistent"})

async def test_task_delete_module_not_found():
    """TaskDeleteModule raises KeyError for non-existent task."""
    module = TaskDeleteModule(mock_repo_returning_none)
    with pytest.raises(KeyError, match="not found"):
        await module.execute({"task_id": "nonexistent"})
```

### Integration Tests: `tests/bridge/test_registry_setup.py`

```python
async def test_create_apflow_registry():
    """create_apflow_registry returns populated registry."""
    registry = create_apflow_registry(mock_manager, mock_creator, mock_repo)
    # Verify executor modules registered
    # Verify task modules registered
    # Verify total module count > 5 (at least 5 task modules)

async def test_registry_custom_namespace():
    """Custom namespace prefix is applied."""
    registry = create_apflow_registry(
        mock_manager, mock_creator, mock_repo, namespace="myapp"
    )
    # Verify modules named "myapp.rest_executor", "myapp.task.create", etc.
```

---

## Acceptance Criteria

1. `apcore-mcp` can start and expose all registered modules as MCP tools (verified by calling `serve_mcp(registry)` and listing tools).
2. `apcore-a2a` can start and expose registered modules as A2A skills (verified by calling `serve_a2a(registry)` and checking agent card).
3. `apcore-cli` can generate CLI commands for registered modules (verified by calling `create_cli(registry)` and running `--help`).
4. Each module has valid JSON Schema for inputs and outputs (input_schema and output_schema are non-empty dicts with "type" key).
5. Module registration is automatic: adding a new `@executor_register` decorated class causes it to appear in the registry without any code changes to the bridge.
6. All executor modules from preserved extensions are registered: rest, ssh, docker, email, scrape, websocket, mcp, command (8 executors minimum).
7. All 5 task management modules are registered: task.create, task.execute, task.list, task.get, task.delete.
