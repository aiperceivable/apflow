"""
Test flag_modified for JSON field updates in TaskRepository

These tests verify that:
1. JSON fields are properly marked as modified using flag_modified
2. In-place modifications to JSON fields are correctly persisted
3. db.refresh correctly retrieves updated data after commit
"""

import pytest
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


class TestFlagModifiedJsonFields:
    """Test flag_modified behavior for JSON field updates"""

    @pytest.mark.asyncio
    async def test_update_task_result_persists(self, sync_db_session):
        """
        Test that updating result JSON field persists correctly
        """
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )

        # Update status with result
        result_data = {"output": "test_value", "nested": {"key": "value"}}
        success = await repo.update_task(
            task_id=task.id,
            status="completed",
            result=result_data,
        )

        assert success is not None

        # Retrieve task in a fresh query to verify persistence
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.result == result_data
        assert updated_task.result["output"] == "test_value"
        assert updated_task.result["nested"]["key"] == "value"

    @pytest.mark.asyncio
    async def test_update_task_inputs_persists(self, sync_db_session):
        """
        Test that updating inputs JSON field persists correctly
        """
        repo = TaskRepository(sync_db_session)

        # Create a task with initial inputs
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
            inputs={"initial": "value"},
        )

        # Update inputs
        new_inputs = {"url": "https://example.com", "params": {"key": "value"}}
        success = await repo.update_task(task.id, inputs=new_inputs)

        assert success is not None

        # Retrieve task in a fresh query
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.inputs == new_inputs
        assert updated_task.inputs["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_update_task_dependencies_persists(self, sync_db_session):
        """
        Test that updating dependencies JSON field persists correctly
        """
        repo = TaskRepository(sync_db_session)

        # Create tasks
        dep_task = await repo.create_task(
            name="Dependency Task",
            user_id="test-user",
        )
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )

        # Update dependencies
        new_deps = [{"id": dep_task.id, "required": True, "type": "data"}]
        success = await repo.update_task(task.id, dependencies=new_deps)

        assert success is not None

        # Retrieve task in a fresh query
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.dependencies == new_deps
        assert updated_task.dependencies[0]["id"] == dep_task.id

    @pytest.mark.asyncio
    async def test_update_task_params_persists(self, sync_db_session):
        """
        Test that updating params JSON field persists correctly
        """
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )

        # Update params
        new_params = {"executor_id": "test_executor", "config": {"timeout": 60}}
        success = await repo.update_task(task.id, params=new_params)

        assert success is not None

        # Retrieve task in a fresh query
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.params == new_params
        assert updated_task.params["config"]["timeout"] == 60

    @pytest.mark.asyncio
    async def test_update_task_schemas_persists(self, sync_db_session):
        """
        Test that updating schemas JSON field persists correctly
        """
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )

        # Update schemas
        new_schemas = {
            "method": "custom_executor",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
        }
        success = await repo.update_task(task.id, schemas=new_schemas)

        assert success is not None

        # Retrieve task in a fresh query
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.schemas == new_schemas
        assert updated_task.schemas["method"] == "custom_executor"

    @pytest.mark.asyncio
    async def test_multiple_json_field_updates(self, sync_db_session):
        """
        Test multiple JSON field updates in sequence persist correctly
        """
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )

        # Update inputs
        await repo.update_task(task.id, inputs={"input_key": "input_value"})

        # Update params
        await repo.update_task(task.id, params={"param_key": "param_value"})

        # Update schemas
        await repo.update_task(task.id, schemas={"method": "test_method"})

        # Update status with result
        await repo.update_task(
            task_id=task.id,
            status="completed",
            result={"result_key": "result_value"},
        )

        # Verify all fields persisted
        final_task = await repo.get_task_by_id(task.id)
        assert final_task is not None
        assert final_task.inputs == {"input_key": "input_value"}
        assert final_task.params == {"param_key": "param_value"}
        assert final_task.schemas == {"method": "test_method"}
        assert final_task.result == {"result_key": "result_value"}
        assert final_task.status == "completed"

    @pytest.mark.asyncio
    async def test_db_refresh_after_status_update(self, sync_db_session):
        """
        Test that db.refresh correctly retrieves updated data after commit
        """
        repo = TaskRepository(sync_db_session)

        # Create a task
        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )
        original_id = task.id

        # Update status multiple times
        await repo.update_task(
            task_id=task.id,
            status="in_progress",
            progress=0.5,
        )

        # Get task and verify
        task_v1 = await repo.get_task_by_id(original_id)
        assert task_v1 is not None
        assert task_v1.status == "in_progress"
        assert float(task_v1.progress) == 0.5

        # Update again
        await repo.update_task(
            task_id=task.id,
            status="completed",
            progress=1.0,
            result={"final": "result"},
        )

        # Get task again and verify latest state
        task_v2 = await repo.get_task_by_id(original_id)
        assert task_v2 is not None
        assert task_v2.status == "completed"
        assert float(task_v2.progress) == 1.0
        assert task_v2.result == {"final": "result"}

    @pytest.mark.asyncio
    async def test_complex_nested_json_persists(self, sync_db_session):
        """
        Test that complex nested JSON structures persist correctly
        """
        repo = TaskRepository(sync_db_session)

        task = await repo.create_task(
            name="Test Task",
            user_id="test-user",
        )

        # Complex nested result
        complex_result = {
            "level1": {
                "level2": {
                    "level3": {
                        "data": [1, 2, 3],
                        "nested_list": [{"a": 1}, {"b": 2}],
                    }
                }
            },
            "array": [{"key": "value"}, [1, 2, 3]],
            "mixed": {"int": 42, "float": 3.14, "bool": True, "null": None},
        }

        await repo.update_task(
            task_id=task.id,
            status="completed",
            result=complex_result,
        )

        # Retrieve and verify deep nested structure
        updated_task = await repo.get_task_by_id(task.id)
        assert updated_task is not None
        assert updated_task.result == complex_result
        assert updated_task.result["level1"]["level2"]["level3"]["data"] == [1, 2, 3]
        assert updated_task.result["mixed"]["null"] is None
