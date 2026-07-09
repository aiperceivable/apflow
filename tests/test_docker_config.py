from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_starts_apflow_service() -> None:
    dockerfile = PROJECT_ROOT / "Dockerfile"
    lines = dockerfile.read_text(encoding="utf-8").splitlines()

    assert 'CMD ["apflow", "serve", "--host", "0.0.0.0", "--port", "8000", "--all"]' in lines


def test_compose_maps_configurable_host_port_to_container_port_8000() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    ports = compose["services"]["apflow"]["ports"]

    assert ports == ["${APFLOW_API_PORT:-8000}:8000"]


def test_compose_only_exposes_runtime_environment_used_by_standalone_service() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    service = compose["services"]["apflow"]
    environment = service["environment"]

    assert "env_file" not in service
    assert "APFLOW_CLUSTER_ENABLED=${APFLOW_CLUSTER_ENABLED:-false}" not in environment
    assert "APFLOW_NODE_ROLE=${APFLOW_NODE_ROLE:-auto}" not in environment
    assert "APFLOW_NODE_ID=${APFLOW_NODE_ID:-}" not in environment
