"""Verified invocation context carried through orchestration and background runs."""

from contextvars import ContextVar
from typing import Any

execution_context: ContextVar[Any] = ContextVar("apflow_execution_context", default=None)
current_invocation_id: ContextVar[str | None] = ContextVar("apflow_invocation_id", default=None)
