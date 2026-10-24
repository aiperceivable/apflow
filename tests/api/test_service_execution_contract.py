"""Exercise persisted execution and cancellation through authenticated REST requests."""

import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import jwt
import pytest
from starlette.testclient import TestClient


def test_public_run_recovery_routes_create_new_run_and_reject_in_place_reset(
    tmp_path: Path,
) -> None:
    from apflow.api.rest import build_rest_app
    from apflow.app import create_app
    from apflow.core.execution.runs import RunService

    application = create_app(connection_string=f"sqlite:///{tmp_path / 'recovery.db'}")
    try:
        task = asyncio.run(
            application.task_repository.create_task(name="Failed REST run", status="failed")
        )
        asyncio.run(RunService(application.session).ensure_run(task.id))
        with TestClient(build_rest_app(application.registry)) as client:
            paths = client.get("/openapi.json").json()["paths"]
            for action in (
                "task.submit",
                "task.receipt",
                "task.reconcile",
                "run.get",
                "run.retry",
                "budget.scope",
                "budget.reconcile",
                "usage.events",
                "usage.ack",
            ):
                assert f"/modules/apflow.{action}" in paths
            before = client.post("/modules/apflow.run.get", json={"run_id": task.id})
            assert before.status_code == 200
            request = {"run_id": task.id, "idempotency_key": "rest-retry-1"}
            first = client.post("/modules/apflow.run.retry", json=request)
            repeated = client.post("/modules/apflow.run.retry", json=request)
            assert first.status_code == repeated.status_code == 200
            assert first.json() == repeated.json()
            assert first.json()["run_id"] != task.id
            assert first.json()["retry_of_run_id"] == task.id
            obsolete = client.post("/modules/apflow.task.reconcile", json={"task_id": task.id})
            assert obsolete.status_code == 422
            assert obsolete.json()["error"]["code"] == "INVALID_INPUT"
            after = client.post("/modules/apflow.run.get", json={"run_id": task.id})
            assert after.status_code == 200 and before.json() == after.json()
    finally:
        engine = application.session.get_bind().engine
        application.session.close()
        engine.dispose()


def test_rest_background_tree_survives_submit_and_receives_independent_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_storage_singleton: None
) -> None:
    from apflow.api.auth import apply_rest_auth, build_mcp_authenticator
    from apflow.api.rest import build_rest_app
    from apflow.app import create_app
    from apflow.core.base import BaseTask
    from apflow.core.config_manager import get_config_manager
    from apflow.core.extensions import get_registry

    started, notified = Event(), Event()
    release = asyncio.Event()
    calls: list[str] = []

    class BlockingExecutor(BaseTask):
        id = "rest_background_cancel_probe"
        name = "REST cancellation probe"
        description = "Record execution and wait for cooperative cancellation."
        result_format = "envelope"

        async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
            calls.append(inputs["step"])
            if inputs["step"] == "first":
                started.set()
                await release.wait()
            return {"result": {"late": True}}

        async def cancel(self) -> dict[str, Any]:
            notified.set()
            release.set()
            return {"status": "cancelled", "partial_result": {"stopped": True}}

    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set("security.require_identity", True)
    secret = "synthetic-rest-service-key-32-bytes"
    configuration.set("api.jwt_secret", secret)
    extensions = get_registry()
    extensions.register(BlockingExecutor(), executor_class=BlockingExecutor, override=True)
    application = create_app(connection_string=f"sqlite:///{tmp_path / 'rest-service.db'}")
    token = jwt.encode({"sub": "alice"}, secret, algorithm="HS256")
    headers = {"Authorization": f"Bearer {token}"}
    server = apply_rest_auth(
        build_rest_app(application.registry), build_mcp_authenticator(configuration)
    )
    try:
        with TestClient(server) as client:
            definitions = [
                {
                    "name": "root",
                    "params": {"executor_id": BlockingExecutor.id},
                    "inputs": {"step": "root"},
                },
                {
                    "name": "first",
                    "parent_id": "root",
                    "params": {"executor_id": BlockingExecutor.id},
                    "inputs": {"step": "first"},
                },
                {
                    "name": "second",
                    "parent_id": "root",
                    "dependencies": [{"name": "first"}],
                    "params": {"executor_id": BlockingExecutor.id},
                    "inputs": {"step": "second"},
                },
            ]
            response = client.post(
                "/modules/apflow.task.create_tree",
                headers=headers,
                json={"tasks": definitions, "idempotency_key": "rest-operation"},
            )
            assert response.status_code == 200, response.text
            root_id = response.json()["root_task_id"]
            submitted = client.post(
                "/modules/apflow.task.submit", headers=headers, json={"task_id": root_id}
            )
            assert submitted.status_code == 200, submitted.text
            assert submitted.json()["state"] == "accepted"
            assert started.wait(timeout=5), "Accepted background execution did not start"
            observed = client.post(
                "/modules/apflow.task.get", headers=headers, json={"task_id": root_id}
            )
            assert observed.status_code == 200
            assert calls == ["first"]
            other = jwt.encode({"sub": "bob"}, secret, algorithm="HS256")
            assert (
                client.post(
                    "/modules/apflow.task.get",
                    json={"task_id": root_id},
                    headers={"Authorization": f"Bearer {other}"},
                ).status_code
                == 403
            )
            cancelled = client.post(
                "/modules/apflow.task.cancel", headers=headers, json={"task_ids": [root_id]}
            )
            assert cancelled.status_code == 200, cancelled.text
            assert notified.wait(timeout=2), "Cancellation did not reach the running executor"
            final = client.post(
                "/modules/apflow.task.get", headers=headers, json={"task_id": root_id}
            )
            assert final.status_code == 200
            assert final.json()["status"] == "cancelled"
            assert calls == ["first"]
    finally:
        extensions.unregister(BlockingExecutor.id)
        application.session.close()
