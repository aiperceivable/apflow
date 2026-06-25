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
        assert adapter.metadata["executor_id"] == "mock_executor"
        assert adapter.tags == ["http", "api"]
        assert adapter.metadata["dependencies"] == ["httpx"]
        assert adapter.metadata["always_available"] is True
        # apcore ModuleAnnotations should be present
        assert hasattr(adapter.annotations, "readonly")


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


class TestAdapterPreview:
    @pytest.mark.asyncio
    async def test_preview_returns_previewresult(self):
        adapter = _make_adapter()
        result = await adapter.preview({"query": "test"})
        assert len(result.changes) == 1
        assert result.changes[0].action == "execute"
        assert "mock_executor" in result.changes[0].target

    @pytest.mark.asyncio
    async def test_preview_includes_executor_name(self):
        adapter = _make_adapter(executor_name="My Executor", executor_id="my_exec")
        result = await adapter.preview({})
        assert "My Executor" in result.changes[0].summary

    @pytest.mark.asyncio
    async def test_preview_with_context_ignored(self):
        adapter = _make_adapter()
        result_no_ctx = await adapter.preview({"x": 1})
        result_with_ctx = await adapter.preview({"x": 1}, context=object())
        assert result_no_ctx.changes[0].action == result_with_ctx.changes[0].action
