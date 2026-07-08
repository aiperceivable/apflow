"""
Unit tests for AggregateResultsExecutor

Tests the aggregate_results_executor functionality in isolation and with real executors.
"""

import pytest
from apflow.adapters.function_executor import function_executor
from apflow.extensions.core.aggregate_results_executor import AggregateResultsExecutor


class TestAggregateResultsExecutor:
    """Test cases for AggregateResultsExecutor"""

    @pytest.fixture
    def executor(self):
        """Create AggregateResultsExecutor instance"""
        return AggregateResultsExecutor()

    @pytest.mark.asyncio
    async def test_execute_with_dependency_results(self, executor):
        """Test aggregating dependency results from inputs"""
        # Simulate inputs with dependency results (as merged by TaskManager)
        inputs = {
            "task-1": {"system": "Darwin", "cores": 8, "threads": 8},
            "task-2": {"total_gb": 64.0, "system": "Darwin"},
            "task-3": {"total": "926Gi", "used": "15Gi", "available": "319Gi"},
            # Pre-hook markers are filtered out (internal implementation details)
            "_pre_hook_executed": True,
            "_pre_hook_timestamp": "test-timestamp",
        }

        result = await executor.execute(inputs)

        # Verify result structure
        assert result is not None
        assert "summary" in result
        assert "timestamp" in result
        assert "results" in result
        assert "result_count" in result

        # Verify aggregated results (pre-hook markers are filtered out)
        assert result["result_count"] == 3  # Only 3 task results (pre-hook markers filtered)
        assert "task-1" in result["results"]
        assert "task-2" in result["results"]
        assert "task-3" in result["results"]
        # Pre-hook markers are filtered out (internal implementation details)
        assert "_pre_hook_executed" not in result["results"]
        assert "_pre_hook_timestamp" not in result["results"]

        # Verify result values
        assert result["results"]["task-1"]["cores"] == 8
        assert result["results"]["task-2"]["total_gb"] == 64.0
        assert result["results"]["task-3"]["total"] == "926Gi"

    @pytest.mark.asyncio
    async def test_execute_includes_dependency_ids_containing_a_dot(self, executor):
        """Regression: a "." not in key filter silently dropped any dependency
        whose task ID contains a literal dot, contradicting the class
        docstring's documented contract ("No filtering is applied - all keys
        are included"). (Review CRITICAL #59)
        """
        inputs = {
            "task-1": {"cores": 8},
            "service.worker-1": {"status": "ok"},
        }

        result = await executor.execute(inputs)

        assert result["result_count"] == 2
        assert "service.worker-1" in result["results"]
        assert result["results"]["service.worker-1"] == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_execute_with_empty_inputs(self, executor):
        """Test aggregating with empty inputs"""
        inputs = {}

        result = await executor.execute(inputs)

        assert result is not None
        assert result["result_count"] == 0
        assert result["results"] == {}

    @pytest.mark.asyncio
    async def test_execute_with_only_internal_keys(self, executor):
        """Test that pre-hook markers are filtered out, but other keys are included

        Note: Pre-hook markers (_pre_hook_executed, _pre_hook_timestamp) are filtered
        as they are internal implementation details. Other keys are included.
        """
        inputs = {
            "_pre_hook_executed": True,
            "_pre_hook_timestamp": "test-timestamp",
            "_hook_timestamp": "2024-01-01T00:00:00Z",
            "_project_id": "project-123",
        }

        result = await executor.execute(inputs)

        assert result is not None
        # Pre-hook markers are filtered, but other keys are included
        assert result["result_count"] == 2
        assert len(result["results"]) == 2
        # Pre-hook markers are filtered out
        assert "_pre_hook_executed" not in result["results"]
        assert "_pre_hook_timestamp" not in result["results"]
        # Other keys are included
        assert "_hook_timestamp" in result["results"]
        assert "_project_id" in result["results"]

    @pytest.mark.asyncio
    async def test_execute_with_mixed_types(self, executor):
        """Test aggregating results with different types"""
        inputs = {
            "task-1": {"status": "completed", "value": 100},
            "task-2": "simple string result",
            "task-3": 42,  # numeric result
            "task-4": ["list", "result"],
            "_pre_hook_executed": True,  # Pre-hook marker (filtered out)
        }

        result = await executor.execute(inputs)

        assert result is not None
        assert result["result_count"] == 4  # Only 4 task results (pre-hook marker filtered)
        assert "task-1" in result["results"]
        assert "task-2" in result["results"]
        assert "task-3" in result["results"]
        assert "task-4" in result["results"]
        # Pre-hook marker is filtered out
        assert "_pre_hook_executed" not in result["results"]

        # Verify values are preserved
        assert result["results"]["task-1"]["value"] == 100
        assert result["results"]["task-2"] == "simple string result"
        assert result["results"]["task-3"] == 42
        assert result["results"]["task-4"] == ["list", "result"]

    @pytest.mark.asyncio
    async def test_execute_with_nested_results(self, executor):
        """Test aggregating nested result structures"""
        inputs = {
            "task-1": {"result": {"nested": {"data": "value"}}},
            "task-2": {"metadata": {"timestamp": "2024-01-01", "version": "1.0"}},
        }

        result = await executor.execute(inputs)

        assert result is not None
        assert result["result_count"] == 2
        assert "task-1" in result["results"]
        assert "task-2" in result["results"]

        # Verify nested structures are preserved
        assert result["results"]["task-1"]["result"]["nested"]["data"] == "value"
        assert result["results"]["task-2"]["metadata"]["version"] == "1.0"

    def test_get_input_schema(self, executor):
        """Test input schema definition"""
        schema = executor.get_input_schema()

        assert schema is not None
        assert "type" in schema
        assert schema["type"] == "object"
        assert "description" in schema

    def test_executor_properties(self, executor):
        """Test executor metadata properties"""
        assert executor.id == "aggregate_results_executor"
        assert executor.name == "Aggregate Results Executor"
        assert executor.type == "core"
        assert executor.cancelable is False
        assert "aggregation" in executor.tags
        assert "core" in executor.tags

    @pytest.mark.asyncio
    async def test_execute_with_real_function_executor(self, executor):
        """
        Integration test: run three real function-based executors, then aggregate their
        outputs via AggregateResultsExecutor. Verifies the dependency-merge contract
        (executor output -> merged into inputs keyed by task id -> aggregator filters
        pre-hook markers and collates the real results).
        """

        @function_executor(
            id="fe_cpu_probe",
            description="Return a fake CPU snapshot for aggregation tests",
            override=True,
        )
        async def cpu_probe(_inputs: dict) -> dict:
            return {"system": "Darwin", "cores": 8}

        @function_executor(
            id="fe_memory_probe",
            description="Return a fake memory snapshot for aggregation tests",
            override=True,
        )
        async def memory_probe(_inputs: dict) -> dict:
            return {"total_gb": 64.0}

        @function_executor(
            id="fe_disk_probe",
            description="Return a fake disk snapshot for aggregation tests",
            override=True,
        )
        async def disk_probe(_inputs: dict) -> dict:
            return {"total": "926Gi", "available": "319Gi"}

        cpu_result = await cpu_probe({})
        memory_result = await memory_probe({})
        disk_result = await disk_probe({})

        # TaskManager would merge dependency results into the aggregator's inputs
        # keyed by upstream task id, alongside internal pre-hook markers.
        inputs = {
            "cpu-info": cpu_result,
            "memory-info": memory_result,
            "disk-info": disk_result,
            "_pre_hook_executed": True,
            "_pre_hook_timestamp": "2026-04-16T00:00:00Z",
        }

        aggregated = await executor.execute(inputs)

        assert aggregated["result_count"] == 3
        assert aggregated["results"]["cpu-info"] == cpu_result
        assert aggregated["results"]["memory-info"] == memory_result
        assert aggregated["results"]["disk-info"] == disk_result
        assert "_pre_hook_executed" not in aggregated["results"]
        assert "_pre_hook_timestamp" not in aggregated["results"]
