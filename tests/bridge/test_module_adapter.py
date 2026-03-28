"""Tests for ExecutableTaskModuleAdapter"""

import pytest
from apflow.bridge.module_adapter import ExecutableTaskModuleAdapter


class MockExecutor:
    """Mock executor for testing."""

    async def execute(self, inputs):
        return {"result": f"processed {inputs.get('query', '')}"}

    def get_input_schema(self):
        return {"type": "object", "properties": {"query": {"type": "string"}}}

    def get_output_schema(self):
        return {"type": "object", "properties": {"result": {"type": "string"}}}


def _make_adapter(**overrides):
    defaults = {
        "executor_class": MockExecutor,
        "executor_id": "mock_executor",
        "executor_name": "Mock Executor",
        "executor_description": "A mock executor for testing",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
        "tags": ["test"],
        "dependencies": [],
        "always_available": True,
    }
    defaults.update(overrides)
    return ExecutableTaskModuleAdapter(**defaults)


class TestAdapterCreation:
    def test_valid_creation(self):
        adapter = _make_adapter()
        assert adapter.description == "A mock executor for testing"
        assert adapter.input_schema["properties"]["query"]["type"] == "string"
        assert adapter.output_schema["properties"]["result"]["type"] == "string"

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _make_adapter(executor_id="")

    def test_non_class_raises(self):
        with pytest.raises(TypeError, match="must be a class"):
            _make_adapter(executor_class="not_a_class")

    def test_annotations(self):
        adapter = _make_adapter(tags=["http", "api"], dependencies=["httpx"])
        assert adapter.annotations.executor_id == "mock_executor"
        assert adapter.annotations.tags == ["http", "api"]
        assert adapter.annotations.dependencies == ["httpx"]
        assert adapter.annotations.always_available is True


class TestAdapterExecution:
    @pytest.mark.asyncio
    async def test_execute_delegates(self):
        adapter = _make_adapter()
        result = await adapter.execute({"query": "test"})
        assert result == {"result": "processed test"}

    @pytest.mark.asyncio
    async def test_execute_non_dict_raises(self):
        adapter = _make_adapter()
        with pytest.raises(TypeError, match="must be a dict"):
            await adapter.execute("not a dict")

    @pytest.mark.asyncio
    async def test_execute_empty_inputs(self):
        adapter = _make_adapter()
        result = await adapter.execute({})
        assert result == {"result": "processed "}
