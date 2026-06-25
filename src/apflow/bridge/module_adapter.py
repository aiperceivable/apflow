"""
Adapter that wraps apflow ExecutableTask classes as apcore Modules.

apcore uses duck-typed modules: any object with input_schema, output_schema,
description, and execute() is a valid module.
"""

from typing import Any, Dict

from apcore import Change, ModuleAnnotations, PreviewResult

from apflow.logger import get_logger

logger = get_logger(__name__)


class ExecutableTaskModuleAdapter:
    """Wraps an apflow ExecutableTask class as a duck-typed apcore Module.

    apcore expects modules with:
    - input_schema: dict (JSON Schema)
    - output_schema: dict (JSON Schema)
    - description: str
    - execute(inputs, context=None) -> dict
    """

    def __init__(
        self,
        executor_class: type,
        executor_id: str,
        executor_name: str,
        executor_description: str,
        input_schema: Dict[str, Any],
        output_schema: Dict[str, Any],
        tags: list[str] | None = None,
        dependencies: list[str] | None = None,
        always_available: bool = True,
    ) -> None:
        if not isinstance(executor_class, type):
            raise TypeError(f"executor_class must be a class, got {type(executor_class)}")
        if not executor_id:
            raise ValueError("executor_id must be non-empty")

        self._executor_class = executor_class
        self._executor_id = executor_id
        self._executor_name = executor_name
        self.executor_id = executor_id
        # apcore expects plain attributes (not properties) — it may set them during registration
        self.input_schema = input_schema or {"type": "object", "properties": {}}
        self.output_schema = output_schema or {"type": "object", "properties": {}}
        self.description = executor_description or executor_name
        self.annotations = ModuleAnnotations()
        self.tags = tags or []
        self.metadata = {
            "executor_id": executor_id,
            "dependencies": dependencies or [],
            "always_available": always_available,
        }

    async def preview(self, inputs: Dict[str, Any], context: Any = None) -> PreviewResult:
        """Return a prediction of what this executor would change if called."""
        return PreviewResult(
            changes=[
                Change(
                    action="execute",
                    target=f"executor:{self._executor_id}",
                    summary=f"Run executor '{self._executor_name}' with the provided inputs",
                )
            ]
        )

    async def execute(self, inputs: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
        """Instantiate the executor and delegate to its execute() method."""
        if not isinstance(inputs, dict):
            raise TypeError(f"inputs must be a dict, got {type(inputs)}")

        try:
            executor = self._executor_class()
        except TypeError:
            # Some executors may need kwargs — try with empty inputs
            try:
                executor = self._executor_class(inputs={})
            except Exception as e:
                raise RuntimeError(f"Cannot instantiate executor {self._executor_id}: {e}") from e

        result = await executor.execute(inputs)
        return result
