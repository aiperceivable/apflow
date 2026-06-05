"""
Execution module for task management and distribution

Uses lazy imports to avoid loading heavy TaskExecutor at package import time.
"""

__all__ = [
    "TaskManager",
    "TaskCreator",
    "StreamingCallbacks",
    "TaskTracker",
    "TaskExecutor",
]


def __getattr__(name):
    """Lazy import to avoid loading TaskExecutor (which triggers extensions) at package import"""
    if name == "TaskManager":
        from apflow.core.execution.task_manager import TaskManager

        return TaskManager
    elif name == "TaskCreator":
        from apflow.core.execution.task_creator import TaskCreator

        return TaskCreator
    elif name == "StreamingCallbacks":
        from apflow.core.execution.streaming_callbacks import StreamingCallbacks

        return StreamingCallbacks
    elif name == "TaskTracker":
        from apflow.core.execution.task_tracker import TaskTracker

        return TaskTracker
    elif name == "TaskExecutor":
        from apflow.core.execution.task_executor import TaskExecutor

        return TaskExecutor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
