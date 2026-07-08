"""
Aggregate Results Executor - Aggregates dependency task results

This executor aggregates results from dependency tasks into a single result.
It's a built-in executor that provides a common aggregation pattern.

Users can create custom aggregator executors for more complex aggregation logic.
"""

from typing import ClassVar, Dict, Any, Optional

from pydantic import BaseModel, Field

from apflow.core.base import BaseTask
from apflow.core.extensions.decorators import executor_register
from apflow.logger import get_logger

logger = get_logger(__name__)


class AggregateInputSchema(BaseModel):
    """Input schema for aggregate results executor"""

    dependencies: Optional[list[str]] = Field(
        default=None,
        description="List of dependency task IDs (optional, auto-populated by TaskManager)",
    )

    model_config = {"extra": "allow"}


class AggregateOutputSchema(BaseModel):
    summary: str = Field(description="Summary of the aggregation operation")
    timestamp: str = Field(description="ISO timestamp when aggregation was performed")
    results: Dict[str, Any] = Field(description="Dictionary of dependency results keyed by task ID")
    result_count: int = Field(description="Number of aggregated results")


@executor_register()
class AggregateResultsExecutor(BaseTask):
    """
    Executor for aggregating dependency task results

       **How aggregation works:**

       1. **Dependency Resolution (by TaskManager):**
          Before this executor runs, TaskManager's resolve_task_dependencies() merges
          dependency task results into the task's inputs dictionary:

          - For each dependency in task.dependencies, it adds: inputs[dep_id] = dep_task.result
          - Example: If task depends on ["cpu-info", "memory-info"], and those tasks completed
            with results {"cores": 8} and {"total_gb": 64.0}, then inputs becomes:
            {
                "cpu-info": {"cores": 8, "system": "Darwin"},
                "memory-info": {"total_gb": 64.0, "system": "Darwin"}
            }

       2. **Result Extraction (by this executor):**
          This executor receives the inputs dictionary and:
          - Treats all keys in inputs as dependency task IDs
          - Extracts their values as dependency results
          - No filtering is applied - all keys are included
          - If you need to filter certain keys, implement a custom executor

     3. **Aggregation (by this executor):**
        Returns a dictionary containing all dependency results keyed by task ID:
        {
            "cpu-info": {"cores": 8, "system": "Darwin"},
            "memory-info": {"total_gb": 64.0, "system": "Darwin"}
        }

    **Example usage:**
    ```python
    {
        "schemas": {
            "input_schema": {}  # Optional
        },
        "params": {
            "executor_id": "aggregate_results_executor"
        },
        "dependencies": [
            {"id": "cpu-info", "required": True},
            {"id": "memory-info", "required": True}
        ],
        "inputs": {}  # Will be populated by TaskManager with dependency results
    }
    ```

     **Result structure:**
     The executor returns:
     ```python
     {
         "cpu-info": {...},      # Result from cpu-info task
         "memory-info": {...}     # Result from memory-info task
     }
     ```
    """

    id = "aggregate_results_executor"
    name = "Aggregate Results Executor"
    description = "Aggregates dependency task results into a single result"
    tags = ["aggregation", "core", "built-in"]
    examples = [
        "Aggregate system resource monitoring results",
        "Merge multiple task outputs",
        "Combine dependency results",
    ]

    # Cancellation support: No-op (aggregation is instant)
    cancelable: bool = False

    inputs_schema: ClassVar[type[BaseModel]] = AggregateInputSchema
    outputs_schema: ClassVar[type[BaseModel]] = AggregateOutputSchema

    @property
    def type(self) -> str:
        """Extension type identifier for categorization"""
        return "core"

    def __init__(
        self, name: Optional[str] = None, inputs: Optional[Dict[str, Any]] = None, **kwargs: Any
    ):
        """
        Initialize AggregateResultsExecutor

        Args:
            name: Optional executor name
            inputs: Input parameters (will contain dependency results)
            **kwargs: Additional configuration
        """
        super().__init__(inputs=inputs, **kwargs)
        if name:
            self.name = name

    async def execute(self, inputs: Dict[str, Any] = {}) -> Dict[str, Any]:
        """
        Aggregate dependency results from inputs

        Returns:
            Dictionary containing all dependency results keyed by task ID.
        """
        logger.info(f"Aggregating dependency results for {self.name}")

        # Validate inputs if input_schema is defined (from BaseTask)
        try:
            self.check_input_schema(inputs)
            logger.debug(f"Input validation passed for {self.name}")
        except ValueError as e:
            error_msg = f"Input validation failed for {self.name}: {str(e)}"
            logger.error(error_msg)
            raise ValueError(error_msg)

        # Extract dependency results from inputs. No filtering beyond the
        # internal pre-hook markers — a dependency task ID may legitimately
        # contain a "." (see class docstring: "No filtering is applied - all
        # keys are included").
        pre_hook_markers = {"_pre_hook_executed", "_pre_hook_timestamp"}
        dependency_results = {
            key: value for key, value in inputs.items() if key not in pre_hook_markers
        }
        logger.debug(
            f"Extracted {len(dependency_results)} dependency results: {list(dependency_results.keys())}"
        )

        # Create summary information
        import datetime

        result_count = len(dependency_results)

        # Generate summary
        summary_parts = []
        if result_count > 0:
            summary_parts.append(
                f"Aggregated {result_count} task result{'s' if result_count != 1 else ''}"
            )
            if len(dependency_results) <= 3:
                task_names = list(dependency_results.keys())
                summary_parts.append(f"from: {', '.join(task_names)}")
        else:
            summary_parts.append("No task results to aggregate")

        summary = ". ".join(summary_parts)

        logger.info(
            f"Aggregated {len(dependency_results)} dependency results: "
            f"{list(dependency_results.keys())}"
        )

        return {
            "summary": summary,
            "timestamp": datetime.datetime.now().isoformat(),
            "results": dependency_results,
            "result_count": result_count,
        }

    def get_demo_result(self, task: Any, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Provide demo aggregated results"""
        # Filter out internal markers
        pre_hook_markers = {"_pre_hook_executed", "_pre_hook_timestamp", "use_demo"}
        dependency_results = {
            key: value for key, value in inputs.items() if key not in pre_hook_markers
        }

        # If no dependency results, create some demo ones
        if not dependency_results:
            dependency_results = {
                "demo-task-1": {"status": "completed", "result": "Demo result 1"},
                "demo-task-2": {"status": "completed", "result": "Demo result 2"},
            }

        # Create summary information
        import datetime

        result_count = len(dependency_results)
        summary = (
            f"Aggregated {result_count} task results from: {', '.join(dependency_results.keys())}"
        )

        return {
            "summary": summary,
            "timestamp": datetime.datetime.now().isoformat(),
            "results": dependency_results,
            "result_count": result_count,
        }
