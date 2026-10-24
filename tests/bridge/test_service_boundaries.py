"""Ownership and identity regressions for the public service boundary."""

from typing import Any
from pathlib import Path
from sqlalchemy.orm import Session

import jwt
import pytest
from apcore import Context, Identity, Registry
from apcore.errors import ModuleError
from starlette.testclient import TestClient

from apflow.api.auth import apply_rest_auth, build_mcp_authenticator
from apflow.api.rest import build_rest_app
from apflow.bridge.task_modules import TaskCreateModule, TaskGetModule, TaskListModule
from apflow.core.config_manager import ConfigManager
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.mark.asyncio
async def test_task_get_rejects_other_owner(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="private", user_id="bob")
    context = Context.create(identity=Identity(id="alice"))
    with pytest.raises(ModuleError) as raised:
        await TaskGetModule(repository).execute({"task_id": task.id}, context)
    assert raised.value.code == "ACL_DENIED"


@pytest.mark.asyncio
async def test_task_create_binds_authenticated_owner(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    creator = TaskCreator(sync_db_session)
    context = Context.create(identity=Identity(id="alice"))
    created = await TaskCreateModule(creator, repository).execute({"name": "owned"}, context)
    task = await repository.get_task_by_id(created["id"])
    assert task is not None
    assert task.user_id == "alice"


@pytest.mark.asyncio
async def test_task_create_rejects_forged_owner(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    creator = TaskCreator(sync_db_session)
    context = Context.create(identity=Identity(id="alice"))
    with pytest.raises(ModuleError):
        await TaskCreateModule(creator, repository).execute(
            {"name": "owned", "user_id": "bob"}, context
        )


@pytest.mark.asyncio
async def test_task_list_cannot_select_other_owner(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    await repository.create_task(name="private", user_id="bob")
    context = Context.create(identity=Identity(id="alice"))
    with pytest.raises(ModuleError):
        await TaskListModule(repository).execute({"user_id": "bob"}, context)


def test_rest_forwards_verified_identity_to_module() -> None:
    from apcore import ModuleAnnotations

    class IdentityModule:
        description = "Inspect verified identity"
        input_schema = {"type": "object"}
        output_schema = {"type": "object"}
        annotations = ModuleAnnotations()

        async def execute(self, inputs: dict, context: Any = None) -> dict:
            return {"owner": context.identity.id if context and context.identity else None}

    registry = Registry()
    registry.register("identity", IdentityModule())
    configuration = ConfigManager()
    secret = "synthetic-identity-test-key-32-bytes"
    configuration.set("api.jwt_secret", secret)
    authenticator = build_mcp_authenticator(configuration)
    client = TestClient(apply_rest_auth(build_rest_app(registry), authenticator))
    token = jwt.encode({"sub": "alice"}, secret, algorithm="HS256")
    response = client.post(
        "/modules/identity", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    assert response.json()["owner"] == "alice"


@pytest.mark.asyncio
async def test_cancel_module_notifies_runtime_executor(sync_db_session: Session) -> None:
    from apflow.bridge.task_modules import TaskCancelModule
    from apflow.core.execution.task_executor import TaskExecutor
    from apflow.core.execution.task_manager import TaskManager

    class RunningExecutor:
        called = False

        async def cancel(self) -> dict:
            self.called = True
            return {"status": "cancelled"}

    repository = TaskRepository(sync_db_session)
    task = await repository.create_task(name="running", status="in_progress")
    executor = RunningExecutor()
    runtime = TaskExecutor()
    runtime._executor_instances[task.id] = executor
    try:
        output = await TaskCancelModule(TaskManager(sync_db_session)).execute(
            {"task_ids": [task.id]}
        )
        assert executor.called
        assert output["results"][0]["stop_confirmed"]
    finally:
        runtime._executor_instances.pop(task.id, None)


def test_yaml_preserves_security_policy_maps(tmp_path: Path) -> None:
    path = tmp_path / "apflow.yaml"
    path.write_text(
        "security:\n  module_roles:\n    apflow.rest_executor: [trainer]\n"
        "service:\n  required_schema_versions:\n    apflow.training: '1'\n"
    )
    configuration = ConfigManager()
    configuration._load_yaml(str(path))
    assert configuration.get("security.module_roles") == {"apflow.rest_executor": ["trainer"]}
    assert configuration.get("service.required_schema_versions") == {"apflow.training": "1"}


@pytest.mark.asyncio
async def test_authenticated_cancel_preserves_invalid_array_error(sync_db_session: Session) -> None:
    from apflow.bridge.task_modules import TaskCancelModule
    from apflow.core.execution.task_manager import TaskManager

    context = Context.create(identity=Identity(id="alice"))
    with pytest.raises(ModuleError) as invalid:
        await TaskCancelModule(TaskManager(sync_db_session)).execute({"task_ids": None}, context)
    assert invalid.value.code == "INVALID_INPUT"
