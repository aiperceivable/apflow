"""
Test TaskModel customization functionality

This file contains all tests related to custom TaskModel fields (e.g., project_id, priority_level, department).
These tests use extend_existing=True which modifies Base.metadata, so they are excluded from default test runs
to avoid Base.metadata pollution.

Run these tests explicitly with: pytest -m no_auto_run
"""

import pytest
from sqlalchemy import Column, String, Integer
from apflow import (
    set_task_model_class,
    get_task_model_class,
    task_model_register,
    clear_config,
)
from apflow.core.storage.sqlalchemy.models import TaskModel


class TestTaskModelCustomization:
    """Test TaskModel customization features"""

    def setup_method(self):
        """Clear config before each test"""
        clear_config()

    def test_task_model_register_decorator(self):
        """Test @task_model_register() decorator"""

        @task_model_register()
        class CustomTaskModel(TaskModel):
            __tablename__ = "apflow_tasks"
            __table_args__ = {"extend_existing": True}
            project_id = Column(String(255), nullable=True)  # Removed index=True to avoid conflicts
            department = Column(String(100), nullable=True)

        # Verify class was registered
        retrieved_class = get_task_model_class()
        assert retrieved_class == CustomTaskModel
        assert retrieved_class.__name__ == "CustomTaskModel"

    def test_task_model_register_validation(self):
        """Test that task_model_register validates inheritance"""
        with pytest.raises(TypeError, match="must be a subclass of TaskModel"):

            @task_model_register()
            class NotTaskModel:
                pass

    def test_set_task_model_class_validation(self):
        """Test that set_task_model_class validates inheritance"""

        class NotTaskModel:
            pass

        with pytest.raises(TypeError, match="must be a subclass of TaskModel"):
            set_task_model_class(NotTaskModel)

    def test_set_task_model_class_improved_error_message(self):
        """Test that set_task_model_class provides helpful error message"""

        class NotTaskModel:
            pass

        try:
            set_task_model_class(NotTaskModel)
            assert False, "Should have raised TypeError"
        except TypeError as e:
            error_msg = str(e)
            assert "must be a subclass of TaskModel" in error_msg
            assert "Please ensure your custom class inherits from TaskModel" in error_msg
            assert "class MyTaskModel(TaskModel):" in error_msg

    def test_task_model_register_error_message(self):
        """Test that task_model_register provides helpful error message"""
        try:

            @task_model_register()
            class NotTaskModel:
                pass

            assert False, "Should have raised TypeError"
        except TypeError as e:
            error_msg = str(e)
            assert "must be a subclass of TaskModel" in error_msg
            assert "Please ensure your class inherits from TaskModel" in error_msg

    def test_custom_task_model_with_fields(self):
        """Test creating and using custom TaskModel with additional fields"""

        @task_model_register()
        class ProjectTaskModel(TaskModel):
            __tablename__ = "apflow_tasks"
            __table_args__ = {"extend_existing": True}
            project_id = Column(String(255), nullable=True)  # Removed index=True to avoid conflicts
            department = Column(String(100), nullable=True)
            priority_level = Column(Integer, default=2)

        # Verify model class
        model_class = get_task_model_class()
        assert model_class == ProjectTaskModel

        # Verify custom fields exist
        assert hasattr(model_class, "project_id")
        assert hasattr(model_class, "department")
        assert hasattr(model_class, "priority_level")

        # Verify it still has base TaskModel fields
        assert hasattr(model_class, "id")
        assert hasattr(model_class, "name")
        assert hasattr(model_class, "status")
        assert hasattr(model_class, "inputs")
        assert hasattr(model_class, "result")

    def test_set_task_model_class_none(self):
        """Test that set_task_model_class(None) resets to default"""

        # Set custom model
        @task_model_register()
        class CustomTaskModel(TaskModel):
            __tablename__ = "apflow_tasks"
            __table_args__ = {"extend_existing": True}
            pass

        assert get_task_model_class() == CustomTaskModel

        # Reset to None (should use default)
        set_task_model_class(None)
        assert get_task_model_class() == TaskModel

    def test_get_task_model_class_default(self):
        """Test that get_task_model_class returns default when not set"""
        clear_config()
        model_class = get_task_model_class()
        assert model_class == TaskModel


class TestCustomTaskModelWithRepository:
    """Test custom TaskModel with TaskRepository"""

    @pytest.mark.asyncio
    async def test_custom_task_model(self, sync_db_session):
        """
        Test creating tasks with custom TaskModel that has additional fields

        Note: This test requires dropping and recreating tables to add custom columns.
        In production, this would be done via Alembic migrations.

        Since SQLAlchemy's metadata.tables is immutable, we use a new Base instance
        for the custom model to avoid conflicts.
        """
        from apflow.core.storage.sqlalchemy.models import TASK_TABLE_NAME
        from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
        from sqlalchemy import Column, String
        from sqlalchemy import text

        # Drop existing table using raw SQL first
        try:
            sync_db_session.execute(text(f"DROP TABLE IF EXISTS {TASK_TABLE_NAME}"))
            sync_db_session.commit()
        except Exception:
            sync_db_session.rollback()

        # Define custom TaskModel with project_id field using the new Base
        class CustomTaskModel(TaskModel):
            __tablename__ = TASK_TABLE_NAME
            __table_args__ = {"extend_existing": True}
            project_id = Column(String(255), nullable=True)

        # Create table with custom model
        CustomTaskModel.metadata.create_all(sync_db_session.bind)

        repo = TaskRepository(sync_db_session, task_model_class=CustomTaskModel)

        # Create task with custom field
        task = await repo.create_task(
            name="Project Task", user_id="test-user", project_id="proj-123"  # Custom field
        )

        assert task.project_id == "proj-123"
        assert isinstance(task, CustomTaskModel)


class TestCustomTaskModelWithAgentExecutor:
    """Test custom TaskModel with AgentExecutor and hooks"""

    @pytest.fixture
    def mock_event_queue(self):
        """Create mock event queue"""
        from unittest.mock import AsyncMock
        from a2a.server.events import EventQueue

        event_queue = AsyncMock(spec=EventQueue)
        event_queue.enqueue_event = AsyncMock()
        return event_queue

    def _create_request_context(self, tasks: list, metadata: dict = None):
        """Helper to create RequestContext with tasks array"""
        from unittest.mock import Mock
        from a2a.server.agent_execution import RequestContext
        from a2a.types import Message, DataPart
        import uuid

        if metadata is None:
            metadata = {}

        # Create message with DataPart containing tasks
        message = Mock(spec=Message)
        message.parts = []

        # Option 1: Wrapped format (tasks array in first part)
        if len(tasks) == 1:
            data_part = Mock()
            data_part.root = DataPart(data={"tasks": tasks})
            message.parts.append(data_part)
        else:
            # Option 2: Direct format (each part is a task)
            for task in tasks:
                data_part = Mock()
                data_part.root = DataPart(data=task)
                message.parts.append(data_part)

        context = Mock(spec=RequestContext)
        context.task_id = str(uuid.uuid4())
        context.context_id = str(uuid.uuid4())
        context.metadata = metadata
        context.message = message
        context.configuration = {}

        return context
