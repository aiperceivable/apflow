from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from apflow import clear_config, executor_register
from apflow.core.base import BaseTask
from apflow.core.config_manager import get_config_manager
from apflow.core.execution.task_manager import TaskManager
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.models import TaskModel
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository
from apflow.core.types import TaskTreeNode


@pytest.mark.asyncio
async def test_config_manager_env_and_hooks_integration(tmp_path: Path, sync_db_session) -> None:
    clear_config()
    registry = get_registry()
    registry._executor_classes.clear()
    registry._factory_functions.clear()

    config_manager = get_config_manager()
    config_manager.clear()

    env_keys = ["APFLOW_DEMO_SLEEP_SCALE", "INTEGRATION_FLAG"]
    previous_env: Dict[str, Optional[str]] = {key: os.getenv(key) for key in env_keys}

    env_file = tmp_path / ".env"
    env_file.write_text("APFLOW_DEMO_SLEEP_SCALE=0.25\nINTEGRATION_FLAG=from_env\n")
    config_manager.load_env_files([env_file], override=True)

    try:
        assert os.getenv("INTEGRATION_FLAG") == "from_env"
        assert os.getenv("APFLOW_DEMO_SLEEP_SCALE") == "0.25"

        hook_calls: List[str] = []

        async def mark_from_config(task: TaskModel) -> None:
            if task.inputs is None:
                task.inputs = {}
            task.inputs["integration_flag"] = os.getenv("INTEGRATION_FLAG")
            hook_calls.append(str(task.id))

        config_manager.register_pre_hook(mark_from_config)
        assert config_manager.get_pre_hooks() == [mark_from_config]

        @executor_register()
        class ConfigManagerExecutor(BaseTask):
            id = "config_manager_executor"
            name = "ConfigManager Executor"
            description = "Executor for ConfigManager integration testing"

            def __init__(self, inputs: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
                super().__init__(inputs=inputs or {}, **kwargs)

            async def execute(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
                return {"result": "ok", "inputs": inputs}

            def get_input_schema(self) -> Dict[str, Any]:
                return {"type": "object"}

        registry.register(
            ConfigManagerExecutor(), executor_class=ConfigManagerExecutor, override=True
        )

        task_manager = TaskManager(sync_db_session)
        task_repository = TaskRepository(sync_db_session)

        task = await task_repository.create_task(
            name=ConfigManagerExecutor.id,
            user_id="integration_user",
            inputs={},
            schemas={"method": ConfigManagerExecutor.id},
        )

        task_tree = TaskTreeNode(task)
        await task_manager.distribute_task_tree(task_tree)
        updated_task = await task_repository.get_task_by_id(task.id)

        assert hook_calls == [str(task.id)]
        assert updated_task is not None
        assert updated_task.inputs is not None
        assert updated_task.result["result"] == "ok"
        assert updated_task.result["inputs"]["integration_flag"] == "from_env"
        assert updated_task.inputs["integration_flag"] == "from_env"
    finally:
        config_manager.clear()
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
