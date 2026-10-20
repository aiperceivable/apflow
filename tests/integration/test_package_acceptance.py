"""Offline package, source-tree, and installed CLI acceptance checks."""

import ast
import json
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import tomllib
from importlib import metadata
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REMOVED_DEPENDENCIES = {
    "duckdb-engine",
    "crewai",
    "litellm",
    "typer",
    "strawberry-graphql",
    "grpclib",
    "beautifulsoup4",
    "trafilatura",
}
REMOVED_EXTENSIONS = {"crewai", "llm", "generate", "grpc", "tools"}


def project_configuration() -> dict[str, Any]:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())


def test_removed_directories_contain_no_source_artifacts() -> None:
    removed_paths = [
        *(f"src/apflow/api/{name}" for name in ("a2a", "mcp", "graphql", "docs")),
        "src/apflow/cli",
        *(f"src/apflow/extensions/{name}" for name in REMOVED_EXTENSIONS),
        *(f"tests/api/{name}" for name in ("a2a", "mcp", "graphql")),
        "tests/cli/legacy",
        *(f"tests/extensions/{name}" for name in REMOVED_EXTENSIONS),
    ]
    remaining = [
        str(artifact.relative_to(PROJECT_ROOT))
        for path in removed_paths
        for artifact in (PROJECT_ROOT / path).rglob("*")
        if artifact.is_file() and "__pycache__" not in artifact.parts
    ]
    assert remaining == []


def test_declared_dependencies_exclude_removed_integrations() -> None:
    project = project_configuration()["project"]
    extras = project["optional-dependencies"]
    assert not REMOVED_EXTENSIONS.intersection(extras)
    assert not {"a2a", "cli", "graphql", "llm-key-config", "mcp", "standard"}.intersection(extras)
    requirements = [
        *project["dependencies"],
        *(value for extra in extras.values() for value in extra),
    ]
    names = {canonicalize_name(Requirement(requirement).name) for requirement in requirements}
    assert not REMOVED_DEPENDENCIES.intersection(names)
    assert {"apcore", "apcore-mcp", "apcore-a2a", "apcore-cli"}.issubset(names)


def test_core_dependency_closure_excludes_removed_optional_integrations() -> None:
    pending = [
        (Requirement(value), frozenset())
        for value in project_configuration()["project"]["dependencies"]
    ]
    visited: set[tuple[str, frozenset[str]]] = set()
    while pending:
        requirement, parent_extras = pending.pop()
        if requirement.marker and not any(
            requirement.marker.evaluate({"extra": extra}) for extra in parent_extras | {""}
        ):
            continue
        name = canonicalize_name(requirement.name)
        extras = frozenset(requirement.extras)
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        distribution = metadata.distribution(name)
        assert requirement.specifier.contains(distribution.version, prereleases=True), requirement
        pending.extend((Requirement(value), extras) for value in distribution.requires or [])
    names = {name for name, _ in visited}
    assert not REMOVED_DEPENDENCIES.intersection(names)
    assert "duckdb-engine" not in names


def test_preserved_source_has_no_removed_extension_imports() -> None:
    violations: list[str] = []
    for source in (PROJECT_ROOT / "src/apflow").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            imports = []
            if isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
            elif isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            for imported in imports:
                if any(
                    imported == f"apflow.extensions.{name}"
                    or imported.startswith(f"apflow.extensions.{name}.")
                    for name in REMOVED_EXTENSIONS
                ):
                    violations.append(
                        f"{source.relative_to(PROJECT_ROOT)}:{node.lineno}: {imported}"
                    )
    assert violations == []


def test_current_console_script_targets_supported_entrypoint() -> None:
    assert project_configuration()["project"]["scripts"] == {"apflow": "apflow.cli:main"}


def run_checked(command: list[str], directory: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=directory,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


def build_local_wheel(directory: Path, environment: dict[str, str]) -> Path:
    source = directory / "source"
    source.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(PROJECT_ROOT / filename, source / filename)
    shutil.copytree(
        PROJECT_ROOT / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )
    wheels = directory / "wheels"
    run_checked(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheels),
            str(source),
        ],
        directory,
        environment,
    )
    (wheel,) = wheels.glob("apflow-*.whl")
    return wheel


def install_local_wheel(wheel: Path, directory: Path, environment: dict[str, str]) -> Path:
    installation = directory / "installed"
    run_checked(
        [sys.executable, "-m", "venv", "--system-site-packages", str(installation)],
        directory,
        environment,
    )
    destination = (
        installation
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    (destination / "acceptance-dependencies.pth").write_text(
        "\n".join(site.getsitepackages()) + "\n"
    )
    run_checked(
        [
            str(installation / "bin/python"),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--ignore-installed",
            str(wheel),
        ],
        directory,
        environment,
    )
    return installation


@pytest.mark.timeout(120)
def test_offline_wheel_installs_with_current_version_and_working_console_script(
    tmp_path: Path,
) -> None:
    for build_dependency in ("build", "setuptools", "setuptools_scm", "wheel"):
        pytest.importorskip(build_dependency)
    configuration = tmp_path / "apflow.yaml"
    connection_string = f"sqlite:///{tmp_path / 'installed.db'}"
    configuration.write_text(json.dumps({"storage": {"connection_string": connection_string}}))
    environment = {
        **os.environ,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "APFLOW_CONFIG": str(configuration),
        "APFLOW_STORAGE_CONNECTION_STRING": connection_string,
    }
    environment.pop("PYTHONPATH", None)
    wheel = build_local_wheel(tmp_path, environment)
    installation = install_local_wheel(wheel, tmp_path, environment)
    output = run_checked(
        [
            str(installation / "bin/python"),
            "-c",
            "import apflow,json; from importlib.metadata import version; "
            "print(json.dumps({'version':apflow.__version__,'metadata':version('apflow'),"
            "'file':apflow.__file__}))",
        ],
        tmp_path,
        environment,
    )
    installed = json.loads(output)
    expected = project_configuration()["project"]["version"]
    assert installed["version"] == installed["metadata"] == expected
    assert Path(installed["file"]).is_relative_to(installation)
    assert expected in run_checked(
        [str(installation / "bin/apflow"), "--version"], tmp_path, environment
    )
    assert "AI-Perceivable Distributed Orchestration" in run_checked(
        [str(installation / "bin/apflow"), "--help"], tmp_path, environment
    )
