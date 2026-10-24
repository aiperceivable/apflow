"""Local runtime handles share the same storage boundary as task execution."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apflow.core.base import BaseTask
from apflow.core.execution.task_executor import TaskExecutor
from apflow.core.extensions import get_registry
from apflow.core.storage.sqlalchemy.models import Base, TaskModel
from apflow.core.storage.sqlalchemy.task_repository import TaskRepository


@dataclass
class ExecutionControl:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancellations: int = 0
    saw_cancellation: bool = False


@pytest.fixture
def controls() -> Iterator[dict[str, ExecutionControl]]:
    controls = {label: ExecutionControl() for label in ("first", "second")}

    class ScopedProbe(BaseTask):
        id = "storage_scope_execution_probe"
        name = "Storage scope execution probe"
        description = "Wait until released and record cancellation routing."
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            self.control = controls[inputs["label"]]
            self.control.started.set()
            await self.control.release.wait()
            assert self.cancellation_checker is not None
            self.control.saw_cancellation = self.cancellation_checker()
            return {"result": inputs["label"]}

        async def cancel(self) -> dict[str, Any]:
            self.control.cancellations += 1
            self.control.release.set()
            return {"status": "cancelled"}

    registry = get_registry()
    registry.register(ScopedProbe(), executor_class=ScopedProbe, override=True)
    try:
        yield controls
    finally:
        for control in controls.values():
            control.release.set()
        registry.unregister(ScopedProbe.id)


async def _create_task(session: Session, label: str) -> None:
    await TaskRepository(session).create_task(
        id="shared-custom-id",
        name=label,
        inputs={"label": label},
        params={"executor_id": "storage_scope_execution_probe"},
    )


@pytest.mark.asyncio
async def test_cancel_pending_task_does_not_notify_another_database_executor(
    controls: dict[str, ExecutionControl],
) -> None:
    first_engine = create_engine("sqlite://")
    second_engine = create_engine("sqlite://")
    for engine in (first_engine, second_engine):
        Base.metadata.create_all(engine)
    try:
        with Session(first_engine) as first, Session(second_engine) as second:
            await _create_task(first, "first")
            await _create_task(second, "second")
            executor = TaskExecutor()
            running = asyncio.create_task(
                executor.execute_task_by_id("shared-custom-id", db_session=first)
            )
            try:
                await asyncio.wait_for(controls["first"].started.wait(), 3)
                result = await executor.cancel_task_tree("shared-custom-id", db_session=second)
                assert result["stop_confirmed"] is True
                assert controls["first"].cancellations == 0
                assert not running.done()
            finally:
                controls["first"].release.set()
                await asyncio.wait_for(running, 3)
            assert not controls["first"].saw_cancellation
            first_task = first.get(TaskModel, "shared-custom-id")
            second_task = second.get(TaskModel, "shared-custom-id")
            assert first_task is not None and first_task.status == "completed"
            assert second_task is not None and second_task.status == "cancelled"
    finally:
        first_engine.dispose()
        second_engine.dispose()


@pytest.mark.asyncio
async def test_same_task_id_executes_independently_and_cancels_within_its_database(
    controls: dict[str, ExecutionControl],
) -> None:
    first_engine = create_engine("sqlite://")
    second_engine = create_engine("sqlite://")
    for engine in (first_engine, second_engine):
        Base.metadata.create_all(engine)
    try:
        with Session(first_engine) as first, Session(second_engine) as second:
            await _create_task(first, "first")
            await _create_task(second, "second")
            executor = TaskExecutor()
            running = []
            try:
                running.append(
                    asyncio.create_task(
                        executor.execute_task_by_id("shared-custom-id", db_session=first)
                    )
                )
                await asyncio.wait_for(controls["first"].started.wait(), 3)
                running.append(
                    asyncio.create_task(
                        executor.execute_task_by_id("shared-custom-id", db_session=second)
                    )
                )
                await asyncio.wait_for(controls["second"].started.wait(), 3)
                with Session(first_engine) as duplicate_session:
                    duplicate = await executor.execute_task_by_id(
                        "shared-custom-id", db_session=duplicate_session
                    )
                assert duplicate["status"] == "already_running"
                assert executor.get_running_tasks_count() == 2
                with Session(second_engine) as cancellation_session:
                    result = await executor.cancel_task_tree(
                        "shared-custom-id", db_session=cancellation_session
                    )
                assert result["stop_confirmed"] is True
                await asyncio.wait_for(running[1], 3)
                assert controls["second"].cancellations == 1
                assert controls["second"].saw_cancellation
                assert controls["first"].cancellations == 0
                assert not running[0].done()
            finally:
                for control in controls.values():
                    control.release.set()
                await asyncio.wait_for(asyncio.gather(*running), 3)
            first.expire_all()
            second.expire_all()
            first_task = first.get(TaskModel, "shared-custom-id")
            second_task = second.get(TaskModel, "shared-custom-id")
            assert first_task is not None and first_task.status == "completed"
            assert second_task is not None and second_task.status == "cancelled"
            assert not controls["first"].saw_cancellation
            assert not executor.is_task_running("shared-custom-id")
    finally:
        first_engine.dispose()
        second_engine.dispose()
