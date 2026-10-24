"""Required plugin failures must prevent service readiness."""

from unittest.mock import MagicMock
from pathlib import Path

import pytest


def test_required_module_missing_refuses_readiness() -> None:
    from apflow.bridge.registry_setup import validate_required_modules

    registry = MagicMock()
    registry.has.return_value = False
    with pytest.raises(RuntimeError, match="apflow.training"):
        validate_required_modules(registry, ["apflow.training"])


def test_required_module_schema_fallback_refuses_readiness() -> None:
    from apflow.bridge.registry_setup import validate_required_modules

    registry = MagicMock()
    registry.has.return_value = True
    registry.get_definition.return_value.metadata = {"schema_fallback": True}
    with pytest.raises(RuntimeError, match="schema"):
        validate_required_modules(registry, ["apflow.training"])


def test_required_module_version_checked() -> None:
    from apflow.bridge.registry_setup import validate_required_modules

    registry = MagicMock()
    registry.has.return_value = True
    registry.get_definition.return_value.metadata = {"schema_version": "1"}
    validate_required_modules(registry, ["apflow.training"], {"apflow.training": "1"})
    with pytest.raises(RuntimeError, match="version"):
        validate_required_modules(registry, [], {"apflow.training": "2"})


def test_scanner_records_fallback_schema_provenance() -> None:
    from apflow.bridge.scanner_bridge import _create_adapter_from_class

    class BrokenSchemaExecutor:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("Missing deployment configuration")

    adapter = _create_adapter_from_class("broken-schema", BrokenSchemaExecutor)
    assert adapter is not None
    assert adapter.metadata["schema_fallback"] is True


def test_real_app_rejects_missing_configured_required_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reset_storage_singleton: None
) -> None:
    from apflow.app import create_app
    from apflow.core.config_manager import get_config_manager

    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set("service.required_modules", ["apflow.missing_training_plugin"])
    with pytest.raises(RuntimeError, match="apflow.missing_training_plugin"):
        create_app(connection_string=f"sqlite:///{tmp_path / 'required.db'}")


def test_real_app_readiness_rechecks_required_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reset_storage_singleton: None
) -> None:
    from starlette.testclient import TestClient
    from apflow.app import create_app
    from apflow.api.rest import build_rest_app
    from apflow.core.config_manager import get_config_manager

    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set("service.required_modules", [])
    application = create_app(connection_string=f"sqlite:///{tmp_path / 'readiness.db'}")
    try:
        with TestClient(build_rest_app(application.registry)) as client:
            assert client.get("/readyz").status_code == 200
            configuration.set("service.required_modules", ["apflow.missing_training_plugin"])
            assert client.get("/readyz").status_code == 503
            assert client.get("/healthz").status_code == 200
    finally:
        application.session.close()


def test_missing_required_capability_refuses_new_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reset_storage_singleton: None
) -> None:
    from starlette.testclient import TestClient
    from apflow.app import create_app
    from apflow.api.rest import build_rest_app
    from apflow.core.config_manager import get_config_manager

    configuration = get_config_manager()
    monkeypatch.setattr(configuration, "_data", dict(configuration._data))
    configuration.set("service.required_modules", [])
    application = create_app(connection_string=f"sqlite:///{tmp_path / 'admission.db'}")
    try:
        with TestClient(build_rest_app(application.registry)) as client:
            configuration.set("service.required_modules", ["apflow.missing_training_plugin"])
            response = client.post("/modules/apflow.task.create", json={"name": "blocked"})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    finally:
        application.session.close()
