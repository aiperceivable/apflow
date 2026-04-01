"""
Integration test for aggregate_results_executor in real-world scenarios

This test demonstrates how to use aggregate_results_executor to aggregate
dependency task results in a real execution environment.
"""

import pytest
import json
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.config import get_task_model_class


class TestAggregateResultsIntegration:
    """Integration tests for aggregate_results_executor in real scenarios"""

    @pytest.mark.asyncio
    async def test_aggregate_system_resources_via_cli_style(self, use_test_db_session):
        """
        Real-world scenario: Aggregate system resource monitoring results

        This test demonstrates a typical use case:
        1. Create multiple child tasks that collect system information (CPU, memory, disk)
        2. Create a parent task that depends on all child tasks
        3. Use aggregate_results_executor to aggregate all child results
        4. Verify the aggregated result contains all dependency results

        This is similar to how you would use it via CLI or API.
        """
        task_executor = TaskExecutor()
        db_session = use_test_db_session

        # Define task tree structure
        # This is the same format you would use in CLI: apflow run flow --tasks '[...]'
        tasks = [
            # Root task: Aggregate all system resource information
            {
                "id": "aggregate-system-info",
                "user_id": "test-user",
                "name": "Aggregate System Information",
                "status": "pending",
                "priority": 3,
                "has_children": True,
                "dependencies": [
                    {"id": "cpu-info", "required": True},
                    {"id": "memory-info", "required": True},
                    {"id": "disk-info", "required": True},
                ],
                "params": {"executor_id": "aggregate_results_executor"},
                "inputs": {},  # Will be populated with dependency results by TaskManager
            },
            # Child task 1: Get CPU information
            {
                "id": "cpu-info",
                "parent_id": "aggregate-system-info",
                "user_id": "test-user",
                "name": "Get CPU Information",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "cpu"},
            },
            # Child task 2: Get Memory information
            {
                "id": "memory-info",
                "parent_id": "aggregate-system-info",
                "user_id": "test-user",
                "name": "Get Memory Information",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "memory"},
            },
            # Child task 3: Get Disk information
            {
                "id": "disk-info",
                "parent_id": "aggregate-system-info",
                "user_id": "test-user",
                "name": "Get Disk Information",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "disk"},
            },
        ]

        # Execute tasks using TaskExecutor (same as CLI would do)
        execution_result = await task_executor.execute_tasks(
            tasks=tasks,
            root_task_id=None,
            use_streaming=False,
            require_existing_tasks=False,
            db_session=db_session,
        )

        # Verify execution completed successfully
        assert execution_result is not None
        assert execution_result["status"] == "completed"
        assert "root_task_id" in execution_result
        root_task_id = execution_result["root_task_id"]

        # Verify root task result
        task_repository = TaskRepository(db_session, task_model_class=get_task_model_class())
        root_task = await task_repository.get_task_by_id(root_task_id)

        assert root_task is not None
        assert root_task.status == "completed"
        assert root_task.result is not None

        # Verify aggregated result structure
        aggregated_result = root_task.result
        assert isinstance(aggregated_result, dict)
        assert "summary" in aggregated_result
        assert "timestamp" in aggregated_result
        assert "results" in aggregated_result
        assert "result_count" in aggregated_result
        assert aggregated_result["result_count"] == 3  # cpu, memory, disk

        # Verify all dependency results are in aggregated result
        results = aggregated_result["results"]
        assert "cpu-info" in results
        assert "memory-info" in results
        assert "disk-info" in results

        # Verify child task results contain actual system data
        cpu_result = results["cpu-info"]
        assert isinstance(cpu_result, dict)
        assert "system" in cpu_result or "cores" in cpu_result or "brand" in cpu_result

        memory_result = results["memory-info"]
        assert isinstance(memory_result, dict)
        assert "total_gb" in memory_result or "system" in memory_result

        disk_result = results["disk-info"]
        assert isinstance(disk_result, dict)
        assert "total" in disk_result or "available" in disk_result or "system" in disk_result

        # Verify child tasks were also completed
        cpu_task = await task_repository.get_task_by_id("cpu-info")
        assert cpu_task.status == "completed"
        assert cpu_task.result is not None

        memory_task = await task_repository.get_task_by_id("memory-info")
        assert memory_task.status == "completed"
        assert memory_task.result is not None

        disk_task = await task_repository.get_task_by_id("disk-info")
        assert disk_task.status == "completed"
        assert disk_task.result is not None

        print("\n✅ Successfully aggregated system resources:")
        print(f"   Root task: {root_task_id}")
        print(f"   Aggregated {aggregated_result['result_count']} dependency results")
        print(f"   Results keys: {list(results.keys())}")

    @pytest.mark.asyncio
    async def test_aggregate_results_with_mixed_resources(self, use_test_db_session):
        """
        Real-world scenario: Aggregate results from multiple different resource types

        This test demonstrates aggregating results from tasks that collect
        different types of system information (CPU, memory, disk).
        All tasks are in the same task tree for proper dependency resolution.
        """
        task_executor = TaskExecutor()
        db_session = use_test_db_session

        # Create task tree with multiple resource collection tasks
        tasks = [
            # Root task: Aggregate all resource information
            {
                "id": "aggregate-mixed-resources",
                "user_id": "test-user",
                "name": "Aggregate Mixed Resources",
                "status": "pending",
                "priority": 3,
                "has_children": True,
                "dependencies": [
                    {"id": "resource-cpu", "required": True},
                    {"id": "resource-memory", "required": True},
                    {"id": "resource-disk", "required": True},
                ],
                "params": {"executor_id": "aggregate_results_executor"},
                "inputs": {},
            },
            # Child tasks: Collect different resource types
            {
                "id": "resource-cpu",
                "parent_id": "aggregate-mixed-resources",
                "user_id": "test-user",
                "name": "Collect CPU Info",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "cpu"},
            },
            {
                "id": "resource-memory",
                "parent_id": "aggregate-mixed-resources",
                "user_id": "test-user",
                "name": "Collect Memory Info",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "memory"},
            },
            {
                "id": "resource-disk",
                "parent_id": "aggregate-mixed-resources",
                "user_id": "test-user",
                "name": "Collect Disk Info",
                "status": "pending",
                "priority": 1,
                "has_children": False,
                "dependencies": [],
                "schemas": {"method": "rest_executor"},
                "inputs": {"resource": "disk"},
            },
        ]

        # Execute the complete task tree
        execution_result = await task_executor.execute_tasks(
            tasks=tasks,
            root_task_id=None,
            use_streaming=False,
            require_existing_tasks=False,
            db_session=db_session,
        )

        print(f"==execution_result==\n {json.dumps(execution_result, indent=2)}")

        # Verify execution completed
        assert execution_result["status"] == "completed"

        # Verify aggregated result
        task_repository = TaskRepository(db_session, task_model_class=get_task_model_class())
        aggregate_task = await task_repository.get_task_by_id("aggregate-mixed-resources")
        assert aggregate_task.status == "completed"
        assert aggregate_task.result is not None

        aggregated_result = aggregate_task.result
        assert aggregated_result["result_count"] == 3  # cpu, memory, disk

        results = aggregated_result["results"]
        assert "resource-cpu" in results
        assert "resource-memory" in results
        assert "resource-disk" in results

        # Verify each resource type has appropriate data
        cpu_data = results["resource-cpu"]
        assert isinstance(cpu_data, dict)
        assert "system" in cpu_data or "cores" in cpu_data or "brand" in cpu_data

        memory_data = results["resource-memory"]
        assert isinstance(memory_data, dict)
        assert "total_gb" in memory_data or "system" in memory_data

        disk_data = results["resource-disk"]
        assert isinstance(disk_data, dict)
        assert "total" in disk_data or "available" in disk_data or "system" in disk_data

        print("\n✅ Successfully aggregated mixed resources:")
        print("   Root task: aggregate-mixed-resources")
        print(f"   Aggregated {aggregated_result['result_count']} dependency results")
        print(f"   Results keys: {list(results.keys())}")
        print(f"   CPU data keys: {list(cpu_data.keys())}")
        print(f"   Memory data keys: {list(memory_data.keys())}")
        print(f"   Disk data keys: {list(disk_data.keys())}")
