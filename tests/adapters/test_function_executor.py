"""Tests for @function_executor decorator."""

import pytest

from apflow.adapters.function_executor import (
    _resolve_schema,
    function_executor,
    get_function_executor_classes,
)


class TestFunctionExecutorDecorator:
    def test_returns_original_function(self):
        @function_executor(id="test_returns_fn", description="test")
        async def my_func(inputs: dict) -> dict:
            return {"ok": True}

        assert callable(my_func)
        assert not isinstance(my_func, type)

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
        assert result == {"value": 6}

    @pytest.mark.asyncio
    async def test_sync_function_wrapped_in_thread(self):
        @function_executor(id="test_sync_exec", description="test")
        def my_sync_func(inputs: dict) -> dict:
            return {"sync": True}

        cls = get_function_executor_classes()["test_sync_exec"]
        instance = cls()
        result = await instance.execute({})
        assert result == {"sync": True}

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
        assert cls.outputs_schema == out_schema

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
        assert cls.outputs_schema is Out

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
