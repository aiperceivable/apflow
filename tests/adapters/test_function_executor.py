"""Tests for @function_executor decorator."""

import threading
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.orm import Session

from apflow.adapters.function_executor import (
    _resolve_schema,
    function_executor,
    get_function_executor_classes,
)
from apflow.core.base import BaseTask
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.execution.task_manager import TaskManager


class TestFunctionExecutorDecorator:
    def test_returns_original_function(self) -> None:
        async def my_func(inputs: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True}

        decorated = function_executor(id="test_returns_fn", description="test")(my_func)
        assert decorated is my_func
        generated = get_function_executor_classes()["test_returns_fn"]
        assert issubclass(generated, BaseTask)

    def test_registers_in_extension_registry(self):
        @function_executor(id="test_ext_registry", description="test")
        async def my_func(inputs: dict) -> dict:
            return {}

        from apflow.core.extensions.registry import get_registry

        entry = get_registry().get_by_id("test_ext_registry")
        assert entry is not None

    def test_stores_in_function_executor_classes(self):
        @function_executor(id="test_stored", description="test")
        async def my_func(inputs: dict) -> dict:
            return {}

        classes = get_function_executor_classes()
        assert "test_stored" in classes

    @pytest.mark.asyncio
    async def test_async_function_execution(self):
        @function_executor(id="test_async_exec", description="test")
        async def my_func(inputs: dict) -> dict:
            return {"value": inputs.get("x", 0) + 1}

        cls = get_function_executor_classes()["test_async_exec"]
        instance = cls()
        result = await instance.execute({"x": 5})
        assert result == {"result": {"value": 6}}

    @pytest.mark.asyncio
    async def test_sync_function_wrapped_in_thread(self) -> None:
        event_loop_thread = threading.get_ident()
        called_threads: list[int] = []

        @function_executor(id="test_sync_exec", description="test")
        def my_sync_func(inputs: dict[str, Any]) -> dict[str, Any]:
            called_threads.append(threading.get_ident())
            return {"sync": True}

        cls = get_function_executor_classes()["test_sync_exec"]
        instance = cls()
        result = await instance.execute({})
        assert result == {"result": {"sync": True}}
        assert len(called_threads) == 1
        assert called_threads[0] != event_loop_thread

    def test_output_schema_and_model_conflict_rejected(self) -> None:
        class Output(BaseModel):
            value: int

        with pytest.raises(ValueError, match="mutually exclusive"):
            function_executor(
                id="conflicting_output",
                description="Conflicting output schema and model",
                output_schema={"type": "object"},
                output_model=Output,
            )

    @pytest.mark.asyncio
    async def test_registered_function_runs_through_task_manager_tree(
        self, sync_db_session: Session
    ) -> None:
        calls: list[dict[str, Any]] = []

        @function_executor(
            id="function_tree_acceptance", description="Execute a function in a tree"
        )
        async def calculate(inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(inputs)
            return {"answer": inputs["value"] + 1}

        tree = await TaskCreator(sync_db_session).create_task_tree_from_array(
            [
                {
                    "name": "Function task",
                    "params": {"executor_id": "function_tree_acceptance"},
                    "inputs": {"value": 41},
                }
            ]
        )
        manager = TaskManager(sync_db_session, pre_hooks=[], post_hooks=[])
        completed = await manager.distribute_task_tree(tree, use_callback=False)
        sync_db_session.expire_all()
        stored = await manager.task_repository.get_task_by_id(str(tree.task.id))
        assert completed is tree
        assert calls == [{"value": 41}]
        assert stored is not None
        assert stored.status == "completed"
        assert stored.result == {"answer": 42}

    def test_explicit_json_schema(self):
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
        assert cls().get_output_schema()["$defs"]["business_result"]["anyOf"][0] == out_schema

    def test_pydantic_model_schema(self):
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
        assert (
            cls().get_output_schema()["$defs"]["business_result"]["anyOf"][0]
            == Out.model_json_schema()
        )

    def test_mutual_exclusivity_raises(self):
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
        with pytest.raises(ValueError):

            @function_executor(id="", description="test")
            async def my_func(inputs: dict) -> dict:
                return {}

    def test_empty_description_raises(self):
        with pytest.raises(ValueError):

            @function_executor(id="test_empty_desc", description="")
            async def my_func(inputs: dict) -> dict:
                return {}

    def test_name_defaults_to_title_cased_id(self):
        @function_executor(id="analyze_pr_v2", description="test")
        async def my_func(inputs: dict) -> dict:
            return {}

        cls = get_function_executor_classes()["analyze_pr_v2"]
        assert cls.name == "Analyze Pr V2"

    def test_custom_name(self):
        @function_executor(id="test_custom_name", description="test", name="My Custom Name")
        async def my_func(inputs: dict) -> dict:
            return {}

        cls = get_function_executor_classes()["test_custom_name"]
        assert cls.name == "My Custom Name"

    def test_bridge_registry_matches_execution_registry_on_id_reuse(self):
        """Regression: registering the same id twice without override= must
        not let the bridge-discovery dict (_function_executor_classes)
        diverge from the class the extension registry actually uses at
        execution time — otherwise MCP/CLI advertises one implementation
        while execution runs another. (Review CRITICAL #6)
        """

        @function_executor(id="test_diverge", description="v1")
        async def my_func_v1(inputs: dict) -> dict:
            return {"version": 1}

        @function_executor(id="test_diverge", description="v2")
        async def my_func_v2(inputs: dict) -> dict:
            return {"version": 2}

        from apflow.core.extensions.registry import get_registry

        registry = get_registry()
        execution_class = registry.get_executor_class("test_diverge")
        bridge_class = get_function_executor_classes()["test_diverge"]

        assert bridge_class is execution_class


class TestResolveSchema:
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
    def test_function_executor_in_discover_modules(self):
        @function_executor(id="test_bridge_disc", description="bridge test")
        async def my_func(inputs: dict) -> dict:
            return {}

        from apflow.bridge.scanner_bridge import discover_executor_modules

        modules = discover_executor_modules()
        ids = [m.executor_id for m in modules]
        assert "test_bridge_disc" in ids
