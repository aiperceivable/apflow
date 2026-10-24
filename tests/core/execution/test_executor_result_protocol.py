"""Exercise the result protocol across execution, storage, and module boundaries."""

from collections.abc import Iterator
from typing import Any, cast
import asyncio
from unittest.mock import AsyncMock

import pytest
from jsonschema import validate
from sqlalchemy.orm import Session

from apflow.adapters.function_executor import function_executor, get_function_executor_classes
from apflow.bridge.scanner_bridge import _create_adapter_from_class
from apflow.bridge.task_modules import TaskExecuteModule
from apflow.core.base import BaseTask
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.execution.task_manager import TaskManager
from apflow.core.execution.errors import ValidationError
from apflow.core.execution.result_protocol import normalize_executor_result
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.extensions.http.rest_executor import RestExecutor


class EnvelopeProbe(BaseTask):
    id = "protocol_envelope_probe"
    name = "Envelope probe"
    description = "Return a declared execution envelope."
    result_format = "envelope"

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        return inputs["response"]


class RawProbe(EnvelopeProbe):
    id = "protocol_raw_probe"
    result_format = "raw"


@pytest.fixture(autouse=True)
def protocol_executors() -> Iterator[None]:
    registry = get_registry()
    for cls in (EnvelopeProbe, RawProbe):
        registry.register(cls(), executor_class=cls, override=True)
    yield
    for cls in (EnvelopeProbe, RawProbe):
        registry.unregister(cls.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [None, False, 0, "", [], {}, {"result": 42, "error": "business"}]
)
async def test_envelope_is_extracted_once_in_storage_and_root_response(
    sync_db_session: Session, payload: Any
) -> None:
    tree = await TaskCreator(sync_db_session).create_task_tree_from_array(
        [
            {
                "name": "Protocol root",
                "params": {"executor_id": EnvelopeProbe.id},
                "inputs": {"response": {"result": payload, "token_usage": {"total": 3}}},
            }
        ]
    )
    response = await TaskExecutor().execute_task_by_id(
        str(tree.task.id), db_session=sync_db_session
    )
    task = await TaskManager(sync_db_session).task_repository.get_task_by_id(str(tree.task.id))
    assert task is not None
    assert task.status == "completed"
    assert task.result == payload
    assert task.token_usage == {"input": 0, "output": 0, "total": 3}
    assert "result" in response and response["result"] == payload
    validate(response, TaskExecuteModule(TaskManager(sync_db_session)).output_schema)


@pytest.mark.asyncio
async def test_raw_business_error_and_token_fields_are_not_framework_metadata(
    sync_db_session: Session,
) -> None:
    payload = {"result": 42, "error": "business description", "token_usage": {"total": 100}}
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="Raw business", params={"executor_id": RawProbe.id}, inputs={"response": payload}
    )
    await manager._execute_single_task(task, use_callback=False)
    sync_db_session.refresh(task)
    assert task.status == "completed"
    assert task.result == payload
    assert task.token_usage is None


@pytest.mark.asyncio
async def test_failure_preserves_partial_business_result_and_usage(
    sync_db_session: Session,
) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="Partial failure",
        params={"executor_id": EnvelopeProbe.id},
        inputs={
            "response": {
                "result": {"partial": 1},
                "error": "Provider failed",
                "token_usage": {"total": 2},
            }
        },
    )
    await manager._execute_single_task(task, use_callback=False)
    sync_db_session.refresh(task)
    assert task.status == "failed"
    assert task.error == "Provider failed"
    assert task.result == {"partial": 1}
    assert task.token_usage["total"] == 2


@pytest.mark.asyncio
async def test_declared_envelope_rejects_unplaced_business_fields(sync_db_session: Session) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="Invalid envelope",
        params={"executor_id": EnvelopeProbe.id},
        inputs={"response": {"result": 42, "evidence": ["document"]}},
    )
    await manager._execute_single_task(task, use_callback=False)
    sync_db_session.refresh(task)
    assert task.status == "failed"
    assert task.error is not None
    assert "evidence" in task.error


@pytest.mark.asyncio
async def test_completed_null_dependency_is_injected_and_refreshed(
    sync_db_session: Session,
) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    root = TaskModel(
        id="protocol-root",
        name="Root",
        inputs={"url": "https://b.example"},
        params={"executor_id": "rest_executor"},
        dependencies=[{"id": "protocol-source"}],
    )
    source = TaskModel(
        id="protocol-source",
        name="Source",
        parent_id="protocol-root",
        status="completed",
        result=None,
        params={"executor_id": "rest_executor"},
    )
    sync_db_session.add_all([root, source])
    sync_db_session.commit()
    assert await manager._resolve_and_update_task_inputs(root, str(root.id)) == {
        "url": "https://b.example",
        "protocol-source": None,
    }
    await manager.task_repository.update_task(str(source.id), result={"url": "https://a.example"})
    assert await manager.resolve_task_dependencies(root) == {
        "url": "https://b.example",
        "protocol-source": {"url": "https://a.example"},
    }


@pytest.mark.asyncio
async def test_copy_drops_previously_injected_dependency_keys(sync_db_session: Session) -> None:
    creator = TaskCreator(sync_db_session)
    tree = await creator.create_task_tree_from_array(
        [
            {
                "id": "copy-root",
                "name": "Root",
                "dependencies": [{"id": "copy-source"}],
                "inputs": {"copy-source": {"old": True}, "explicit": 1},
            },
            {"id": "copy-source", "name": "Source", "parent_id": "copy-root"},
        ]
    )
    copied = await creator.from_copy(tree.task)
    assert cast(dict[str, Any], copied.task.inputs) == {"explicit": 1}


@pytest.mark.asyncio
async def test_function_bridge_schema_matches_envelope_and_nested_result() -> None:
    executor_id = "protocol_function_business"

    @function_executor(
        id=executor_id,
        description="Return ordinary business fields.",
        output_schema={
            "type": "object",
            "properties": {"result": {"type": "integer"}},
            "required": ["result"],
            "additionalProperties": False,
        },
    )
    async def business(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"result": 42}

    try:
        cls = get_function_executor_classes()[executor_id]
        adapter = _create_adapter_from_class(executor_id, cls)
        assert adapter is not None
        response = await adapter.execute({})
        validate(response, adapter.output_schema)
        assert response == {"result": {"result": 42}}
        assert await business({}) == {"result": 42}
    finally:
        get_registry().unregister(executor_id)
        from apflow.adapters.function_executor import _function_executor_classes

        _function_executor_classes.pop(executor_id, None)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": 1, "evidence": []},
        {"result": 1, "error": False},
        {"result": 1, "token_usage": {"total": -1}},
        {"result": 1, "token_usage": {"total": True}},
        {"result": 1, "token_usage": {"unexpected": 2}},
    ],
)
def test_envelope_validation_rejects_ambiguous_or_invalid_control_fields(
    response: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        normalize_executor_result(response, "envelope")


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [None, False, 0, "", {}, []])
async def test_cancel_persists_partial_payload_and_usage_separately(
    sync_db_session: Session, partial: Any
) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(name="Cancelled", status="in_progress")
    executor = EnvelopeProbe()
    executor.cancel = AsyncMock(
        return_value={"status": "cancelled", "partial_result": partial, "token_usage": {"total": 4}}
    )
    manager._executor_instances[str(task.id)] = executor
    response = await manager.cancel_task(str(task.id))
    sync_db_session.refresh(task)
    assert response["status"] == task.status == "cancelled"
    assert response["result"] == task.result == partial
    assert task.token_usage["total"] == 4
    await manager._handle_task_execution_result(
        task, str(task.id), {"result": "late", "token_usage": {"total": 4}}
    )
    sync_db_session.refresh(task)
    assert task.status == "cancelled"
    assert task.result == partial
    assert task.token_usage["total"] == 4


@pytest.mark.asyncio
async def test_executor_hook_shortcut_uses_envelope_and_task_hook_sees_business_payload(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"result": "nested business"}
    envelope = {"result": payload, "token_usage": {"total": 2}}
    observed: list[Any] = []

    async def pre_hook(executor: Any, task: Any, inputs: Any) -> dict[str, Any]:
        return envelope

    async def executor_post(executor: Any, task: Any, inputs: Any, output: Any) -> None:
        observed.append(output)

    async def task_post(task: Any, inputs: Any, output: Any) -> None:
        observed.append(output)

    monkeypatch.setattr(
        EnvelopeProbe,
        "_executor_hooks",
        {"pre_hook": pre_hook, "post_hook": executor_post},
        raising=False,
    )
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[task_post])
    task = await manager.task_repository.create_task(
        name="Hook shortcut", params={"executor_id": EnvelopeProbe.id}
    )
    await manager._execute_single_task(task, use_callback=False)
    sync_db_session.refresh(task)
    assert task.status == "completed"
    assert task.result == payload
    assert observed == [envelope, payload]


@pytest.mark.asyncio
async def test_demo_provider_returns_business_example_without_execution(
    sync_db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        EnvelopeProbe,
        "get_demo_result",
        lambda self, task, inputs: {"result": "business", "_demo_sleep": 0},
    )
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[], use_demo=True)
    task = await manager.task_repository.create_task(
        name="Demo envelope", params={"executor_id": EnvelopeProbe.id}
    )
    await manager._execute_single_task(task, use_callback=False)
    sync_db_session.refresh(task)
    assert task.status == "completed"
    assert task.result == {"result": "business", "demo_mode": True}


@pytest.mark.asyncio
async def test_rest_chain_preserves_request_configuration_and_reruns_with_new_output(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sequence = 0

    async def request(self: Any, inputs: dict[str, Any]) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        calls.append(inputs)
        return {
            "success": True,
            "url": inputs["url"],
            "method": inputs["method"],
            "json": {"sequence": sequence},
        }

    monkeypatch.setattr(RestExecutor, "_execute_request", request)
    tree = await TaskCreator(sync_db_session).create_task_tree_from_array(
        [
            {
                "id": "rest-b",
                "name": "Second request",
                "params": {"executor_id": "rest_executor"},
                "inputs": {"url": "https://b.example", "method": "POST"},
                "dependencies": [{"id": "rest-a"}],
            },
            {
                "id": "rest-a",
                "name": "First request",
                "parent_id": "rest-b",
                "params": {"executor_id": "rest_executor"},
                "inputs": {"url": "https://a.example", "method": "GET"},
            },
        ]
    )
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    await manager.distribute_task_tree(tree)
    root = await manager.task_repository.get_task_by_id("rest-b")
    assert root is not None and str(root.status) == "completed"
    assert calls[1]["url"] == "https://b.example" and calls[1]["method"] == "POST"
    assert calls[1]["rest-a"]["json"] == {"sequence": 1}
    await manager.task_repository.update_task("rest-a", status="pending")
    await manager.task_repository.update_task("rest-b", status="pending")
    await manager.distribute_task_tree(tree)
    assert calls[-1]["rest-a"]["json"]["sequence"] > 1
    assert calls[-1]["url"] == "https://b.example"


@pytest.mark.asyncio
async def test_function_bridge_resolves_nested_business_schema_references() -> None:
    from pydantic import BaseModel

    class Evidence(BaseModel):
        source: str

    class Report(BaseModel):
        evidence: Evidence

    executor_id = "protocol_nested_schema"

    @function_executor(id=executor_id, description="Nested schema.", output_model=Report)
    async def report(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"evidence": {"source": "document"}}

    try:
        adapter = _create_adapter_from_class(
            executor_id, get_function_executor_classes()[executor_id]
        )
        assert adapter is not None
        validate(await adapter.execute({}), adapter.output_schema)
    finally:
        get_registry().unregister(executor_id)
        from apflow.adapters.function_executor import _function_executor_classes

        _function_executor_classes.pop(executor_id, None)


@pytest.mark.asyncio
async def test_streaming_final_carries_root_business_result(sync_db_session: Session) -> None:
    payload = {"answer": 42}
    tree = await TaskCreator(sync_db_session).create_task_tree_from_array(
        [
            {
                "name": "Streaming protocol",
                "params": {"executor_id": EnvelopeProbe.id},
                "inputs": {"response": {"result": payload}},
            }
        ]
    )
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    manager.streaming_callbacks.set_streaming_context(events, None)
    await manager.distribute_task_tree_with_streaming(tree)
    await asyncio.gather(*manager.streaming_callbacks._pending_tasks)
    updates = [events.get_nowait() for _ in range(events.qsize())]
    final = next(event for event in updates if event["type"] == "final")
    assert final["result"] == payload
    assert final["progress"] == 1.0


@pytest.mark.asyncio
async def test_builtin_failure_is_a_failed_envelope_in_bridge_and_task_storage(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"success": False, "status_code": 503, "url": "https://api.example", "method": "GET"}
    monkeypatch.setattr(RestExecutor, "_execute_request", AsyncMock(return_value=payload))
    adapter = _create_adapter_from_class(RestExecutor.id, RestExecutor)
    assert adapter is not None
    response = await adapter.execute({"url": "https://api.example"})
    validate(response, adapter.output_schema)
    assert response == {"result": payload, "error": "HTTP request failed"}
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="HTTP failure",
        params={"executor_id": RestExecutor.id},
        inputs={"url": "https://api.example"},
    )
    await manager._execute_single_task(task, use_callback=False)
    sync_db_session.refresh(task)
    assert task.status == "failed"
    assert task.result == payload
    assert task.error == response["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,expected", [({"input": 3, "output": 2}, 5), ({"total": -1}, None)])
async def test_cancellation_normalizes_usage_without_blocking_cancellation(
    sync_db_session: Session,
    usage: dict[str, int],
    expected: int | None,
) -> None:
    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="Cancellation metadata", status="in_progress"
    )
    executor = EnvelopeProbe()
    executor.cancel = AsyncMock(return_value={"status": "cancelled", "token_usage": usage})
    manager._executor_instances[str(task.id)] = executor
    result = await manager.cancel_task(str(task.id))
    sync_db_session.refresh(task)
    assert result["status"] == "cancelled"
    assert task.status == "cancelled"
    saved_usage = cast(dict[str, int] | None, task.token_usage)
    assert (saved_usage or {}).get("total") == expected


@pytest.mark.asyncio
async def test_bridge_preserves_declared_failure_schema_without_adding_fields() -> None:
    class ErrorOnlyExecutor(EnvelopeProbe):
        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            return {"error": "Provider failed", "token_usage": {"input": 3, "output": 2}}

        def get_output_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "error": {"type": "string"},
                    "token_usage": {
                        "type": "object",
                        "properties": {
                            "input": {"type": "integer"},
                            "output": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            }

    adapter = _create_adapter_from_class("protocol_error_only", ErrorOnlyExecutor)
    assert adapter is not None
    response = await adapter.execute({})
    validate(response, adapter.output_schema)
    assert "result" not in response
    assert "total" not in response["token_usage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [42, False, None])
async def test_raw_bridge_without_schema_accepts_primitive_business_output(payload: Any) -> None:
    from apcore import Executor, Registry

    adapter = _create_adapter_from_class(RawProbe.id, RawProbe)
    assert adapter is not None
    response = await adapter.execute({"response": payload})
    validate(response, adapter.output_schema)
    assert response == {"result": payload}
    registry = Registry()
    registry.register("protocol.raw", adapter)
    assert await Executor.from_registry(registry).call_async(
        "protocol.raw", {"response": payload}
    ) == {"result": payload}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, False])
async def test_apcore_task_execution_accepts_null_and_scalar_business_output(
    sync_db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
) -> None:
    from apcore import Executor, Registry

    manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
    task = await manager.task_repository.create_task(
        name="SDK result protocol",
        params={"executor_id": EnvelopeProbe.id},
        inputs={"response": {"result": payload}},
    )

    class SessionExecutor:
        async def execute_task_by_id(
            self, task_id: str, *, db_session: Any = None
        ) -> dict[str, Any]:
            return await TaskExecutor().execute_task_by_id(task_id, db_session=sync_db_session)

    module = TaskExecuteModule(manager)
    monkeypatch.setattr(module, "_new_executor", lambda: SessionExecutor())
    registry = Registry()
    registry.register("protocol.execute", module)
    response = await Executor.from_registry(registry).call_async(
        "protocol.execute", {"task_id": str(task.id)}
    )
    assert response["status"] == "completed"
    assert response["result"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_id_at", [None, "root", "property"])
async def test_raw_bridge_keeps_local_business_schema_pointers_valid(
    empty_id_at: str | None,
) -> None:
    from apcore import Executor, Registry

    class ReferencingExecutor(RawProbe):
        def get_output_schema(self) -> dict[str, Any]:
            schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "value": {"type": "integer"},
                    "copy": {"$ref": "#/properties/value"},
                },
                "required": ["value", "copy"],
            }
            if empty_id_at is not None:
                target = schema if empty_id_at == "root" else schema["properties"]["copy"]
                target["$id"] = ""
            return schema

    adapter = _create_adapter_from_class("protocol_local_pointer", ReferencingExecutor)
    assert adapter is not None
    payload = {"value": 42, "copy": 42}
    response = await adapter.execute({"response": payload})
    validate(response, adapter.output_schema)
    registry = Registry()
    registry.register("protocol.reference", adapter)
    assert await Executor.from_registry(registry).call_async(
        "protocol.reference", {"response": payload}
    ) == {"result": payload}


@pytest.mark.asyncio
async def test_business_schema_sensitive_fields_survive_envelope_wrapping() -> None:
    import json
    from apcore import Executor, Registry
    from apcore_mcp import ExecutionRouter

    class SensitiveRawExecutor(RawProbe):
        def get_output_schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "secret": {"type": "string", "x-sensitive": True},
                },
            }

    adapter = _create_adapter_from_class("protocol_sensitive", SensitiveRawExecutor)
    assert adapter is not None
    registry = Registry()
    registry.register("protocol.sensitive", adapter)
    definition = registry.get_definition("protocol.sensitive")
    assert definition is not None
    router = ExecutionRouter(
        Executor.from_registry(registry),
        output_schema_map={"protocol.sensitive": definition.output_schema},
    )
    content, is_error, _ = await router.handle_call(
        "protocol.sensitive", {"response": {"secret": "private-value"}}
    )
    assert not is_error
    response = json.loads(content[0]["text"])
    assert response["result"]["secret"] != "private-value"


@pytest.mark.asyncio
async def test_envelope_function_preserves_its_declared_failure_schema(
    sync_db_session: Session,
) -> None:
    executor_id = "protocol_function_failure"
    schema = {
        "type": "object",
        "properties": {
            "error": {"type": "string"},
            "token_usage": {
                "type": "object",
                "properties": {
                    "input": {"type": "integer"},
                    "output": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }

    @function_executor(
        id=executor_id,
        description="Explicit failure envelope.",
        result_format="envelope",
        output_schema=schema,
    )
    async def failure(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"error": "Provider failed", "token_usage": {"input": 3, "output": 2}}

    try:
        adapter = _create_adapter_from_class(
            executor_id, get_function_executor_classes()[executor_id]
        )
        assert adapter is not None
        response = await adapter.execute({})
        validate(response, adapter.output_schema)
        assert response == await failure({})
        manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
        task = await manager.task_repository.create_task(
            name="Function failure", params={"executor_id": executor_id}
        )
        await manager._execute_single_task(task, use_callback=False)
        sync_db_session.refresh(task)
        assert task.status == "failed"
        assert task.result is None
        assert task.token_usage["total"] == 5
    finally:
        get_registry().unregister(executor_id)
        from apflow.adapters.function_executor import _function_executor_classes

        _function_executor_classes.pop(executor_id, None)


@pytest.mark.asyncio
async def test_envelope_function_without_schema_accepts_error_only_through_apcore(
    sync_db_session: Session,
) -> None:
    from apcore import Executor, Registry
    from apflow.adapters.function_executor import _function_executor_classes

    executor_id = "protocol_function_default_failure"

    @function_executor(
        id=executor_id,
        description="Failure without a custom output schema.",
        result_format="envelope",
    )
    async def failure(inputs: dict[str, Any]) -> dict[str, Any]:
        return {"error": "Provider failed", "token_usage": {"input": 3, "output": 2}}

    try:
        adapter = _create_adapter_from_class(
            executor_id, get_function_executor_classes()[executor_id]
        )
        assert adapter is not None
        registry = Registry()
        registry.register("protocol.default_failure", adapter)
        response = await Executor.from_registry(registry).call_async("protocol.default_failure", {})
        assert response == await failure({})
        manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
        task = await manager.task_repository.create_task(
            name="Default function failure", params={"executor_id": executor_id}
        )
        await manager._execute_single_task(task, use_callback=False)
        sync_db_session.refresh(task)
        assert task.status == "failed"
        assert task.error == "Provider failed"
        assert task.result is None
        assert task.token_usage["total"] == 5
    finally:
        get_registry().unregister(executor_id)
        _function_executor_classes.pop(executor_id, None)


@pytest.mark.asyncio
async def test_function_business_schema_rejects_successful_null_through_apcore() -> None:
    from apcore import Executor, Registry
    from apcore.errors import SchemaValidationError
    from apflow.adapters.function_executor import _function_executor_classes

    executor_id = "protocol_function_nonnullable"

    @function_executor(
        id=executor_id, description="Require an integer output.", output_schema={"type": "integer"}
    )
    async def invalid_output(inputs: dict[str, Any]) -> None:
        return None

    try:
        adapter = _create_adapter_from_class(
            executor_id, get_function_executor_classes()[executor_id]
        )
        assert adapter is not None
        registry = Registry()
        registry.register("protocol.nonnullable", adapter)
        with pytest.raises(SchemaValidationError):
            await Executor.from_registry(registry).call_async("protocol.nonnullable", {})
    finally:
        get_registry().unregister(executor_id)
        _function_executor_classes.pop(executor_id, None)


@pytest.mark.asyncio
async def test_legacy_function_keeps_provider_usage_in_business_output(
    sync_db_session: Session,
) -> None:
    from apcore import Executor, Registry
    from apflow.adapters.function_executor import _function_executor_classes

    executor_id = "protocol_function_legacy_usage"
    payload = {"result": 42, "token_usage": {"input": 3, "output": 2, "provider_request": 1}}

    @function_executor(
        id=executor_id, description="Legacy provider output.", result_format="legacy"
    )
    async def legacy(inputs: dict[str, Any]) -> dict[str, Any]:
        return payload

    try:
        adapter = _create_adapter_from_class(
            executor_id, get_function_executor_classes()[executor_id]
        )
        assert adapter is not None
        registry = Registry()
        registry.register("protocol.legacy_usage", adapter)
        response = await Executor.from_registry(registry).call_async("protocol.legacy_usage", {})
        assert response == {"result": payload, "token_usage": {"input": 3, "output": 2, "total": 5}}
        manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
        task = await manager.task_repository.create_task(
            name="Legacy function usage", params={"executor_id": executor_id}
        )
        await manager._execute_single_task(task, use_callback=False)
        sync_db_session.refresh(task)
        assert task.status == "completed"
        assert task.result == payload
        assert task.token_usage == {"input": 3, "output": 2, "total": 5}
        assert await legacy({}) == payload
    finally:
        get_registry().unregister(executor_id)
        _function_executor_classes.pop(executor_id, None)
