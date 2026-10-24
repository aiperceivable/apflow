"""Submission keys identify one logical task or tree within a caller scope."""

from typing import Any
from pathlib import Path
from sqlalchemy.orm import Session
from apflow.core.types import TaskTreeNode

import pytest
from apcore import Context, Identity
from apcore.errors import ModuleError

from apflow.bridge.task_modules import TaskCreateModule, TaskCreateTreeModule
from apflow.core.execution.task_creator import TaskCreator
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@pytest.mark.asyncio
async def test_create_key_replays_receipt_and_rejects_changed_content(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    module = TaskCreateModule(TaskCreator(sync_db_session), repository)
    request = {"name": "submitted", "idempotency_key": "operation-1"}
    first = await module.execute(request)
    assert await module.execute(request) == first
    assert len(await repository.query_tasks()) == 1
    with pytest.raises(ModuleError) as raised:
        await module.execute({**request, "name": "different"})
    assert raised.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_create_keys_are_scoped_by_verified_owner(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    module = TaskCreateModule(TaskCreator(sync_db_session), repository)
    request = {"name": "submitted", "idempotency_key": "operation-1"}
    first = await module.execute(request, Context.create(identity=Identity(id="alice")))
    second = await module.execute(request, Context.create(identity=Identity(id="bob")))
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_same_request_receipt_survives_identity_role_refresh(
    sync_db_session: Session,
) -> None:
    repository = TaskRepository(sync_db_session)
    module = TaskCreateModule(TaskCreator(sync_db_session), repository)
    request = {"name": "submitted", "idempotency_key": "operation-1"}
    first = await module.execute(
        request, Context.create(identity=Identity(id="alice", roles=("trainer",)))
    )
    repeated = await module.execute(
        request, Context.create(identity=Identity(id="alice", roles=("trainer", "reviewer")))
    )
    assert repeated == first


@pytest.mark.asyncio
async def test_tree_submission_replays_entire_tree(sync_db_session: Session) -> None:
    repository = TaskRepository(sync_db_session)
    module = TaskCreateTreeModule(TaskCreator(sync_db_session), repository)
    request = {
        "tasks": [{"name": "root"}, {"name": "child", "parent_id": "root"}],
        "idempotency_key": "tree-1",
    }
    first = await module.execute(request)
    assert await module.execute(request) == first
    assert len(await repository.query_tasks()) == 2


def test_concurrent_create_commits_one_tree_and_one_receipt(tmp_path: Path) -> None:
    import asyncio
    import copy
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import sessionmaker
    from apflow.core.execution.submission import create_with_receipt
    from apflow.core.storage.sqlalchemy.models import Base, SubmissionReceipt, TaskModel

    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-create.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = Barrier(2)

    class RacingCreator(TaskCreator):
        async def create_task_trees_from_array(
            self, tasks: list[dict[str, Any]], *, commit: bool = True
        ) -> list[TaskTreeNode]:
            barrier.wait(timeout=10)
            return await super().create_task_trees_from_array(tasks, commit=commit)

    definition = [{"name": "root"}, {"name": "child", "parent_id": "root"}]

    def submit() -> dict[str, Any]:
        with factory() as session:
            return asyncio.run(
                create_with_receipt(
                    RacingCreator(session),
                    TaskRepository(session),
                    copy.deepcopy(definition),
                    "racing-operation",
                    "alice",
                    "tree",
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            receipts = [future.result(timeout=15) for future in futures]
        assert receipts[0] == receipts[1]
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(TaskModel)) == 2
            assert session.scalar(select(func.count()).select_from(SubmissionReceipt)) == 1
    finally:
        engine.dispose()
