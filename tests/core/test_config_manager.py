import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from apflow.core.config_manager import get_config_manager


@pytest.fixture(autouse=True)
def _reset_config_manager():
    cm = get_config_manager()
    cm.clear()
    yield
    cm.clear()


def test_register_pre_post_hooks_via_config_manager():
    cm = get_config_manager()

    recorded: List[str] = []

    def pre_hook(task: Any) -> None:  # type: ignore[unused-argument]
        recorded.append("pre")

    def post_hook(task: Any, inputs: Dict[str, Any], result: Any) -> None:  # type: ignore[unused-argument]
        recorded.append("post")

    cm.register_pre_hook(pre_hook)
    cm.register_post_hook(post_hook)

    assert pre_hook in cm.get_pre_hooks()
    assert post_hook in cm.get_post_hooks()


def test_register_task_tree_hook_via_config_manager():
    cm = get_config_manager()

    def on_completed(root_task: Any, *_: Any) -> None:  # type: ignore[unused-argument]
        return

    cm.register_task_tree_hook("on_tree_completed", on_completed)

    hooks = cm.get_task_tree_hooks("on_tree_completed")
    assert on_completed in hooks


def test_demo_sleep_scale_roundtrip():
    cm = get_config_manager()
    cm.set_demo_sleep_scale(0.5)
    assert cm.get_demo_sleep_scale() == 0.5


def test_clear_resets_demo_sleep_scale(monkeypatch: pytest.MonkeyPatch):
    """Regression: clear() never reset _demo_sleep_scale, breaking its own
    'clear all configuration' contract. (Review CRITICAL #18)"""
    monkeypatch.setenv("APFLOW_DEMO_SLEEP_SCALE", "1.0")
    cm = get_config_manager()
    cm.set_demo_sleep_scale(5.0)
    assert cm.get_demo_sleep_scale() == 5.0

    cm.clear()

    assert cm.get_demo_sleep_scale() == 1.0


def test_get_use_task_creator_defaults_to_task_creator_class():
    """Default (never configured) resolves to the built-in TaskCreator."""
    from apflow.core.execution.task_creator import TaskCreator

    cm = get_config_manager()
    assert cm.get_use_task_creator() is TaskCreator


def test_set_use_task_creator_true_resolves_to_default():
    """Regression: set_use_task_creator(True) — the documented 'recommended'
    usage — previously stored the literal bool True, so get_use_task_creator()
    returned True instead of the TaskCreator class, crashing every caller that
    tries to instantiate it (TypeError: 'bool' object is not callable).
    (Review BLOCKER #1)"""
    from apflow.core.execution.task_creator import TaskCreator

    cm = get_config_manager()
    cm.set_use_task_creator(True)
    assert cm.get_use_task_creator() is TaskCreator


def test_set_use_task_creator_false_raises():
    """Regression: set_use_task_creator(False) — the documented 'quick create
    mode' usage — previously stored the literal bool False, crashing later
    with a confusing 'bool object is not callable' at the only real caller
    (TaskExecutor.execute_tasks). No such mode has ever been implemented, so
    it must now fail immediately and clearly instead. (Review BLOCKER #1)"""
    cm = get_config_manager()
    with pytest.raises(ValueError, match="quick create"):
        cm.set_use_task_creator(False)


def test_set_use_task_creator_custom_class():
    """A custom TaskCreator-compatible class can still be injected directly."""

    class CustomCreator:
        pass

    cm = get_config_manager()
    cm.set_use_task_creator(CustomCreator)
    assert cm.get_use_task_creator() is CustomCreator


def test_load_env_files_uses_dotenv_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cm = get_config_manager()

    loaded: Dict[str, Any] = {}

    def fake_load_dotenv(path: Path, override: bool = False) -> None:
        loaded["path"] = path
        loaded["override"] = override

    fake_dotenv = SimpleNamespace(load_dotenv=fake_load_dotenv)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    env_file = tmp_path / ".env"
    env_file.write_text("KEY=value", encoding="utf-8")

    cm.load_env_files([env_file], override=False)

    assert loaded.get("path") == env_file
    assert loaded.get("override") is False
