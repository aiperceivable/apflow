"""
Auto-generate Executor Documentation

This script generates markdown documentation for all registered executors
using their runtime schemas (not static docs).
"""

import sys
from pathlib import Path
from typing import Dict, Any
import json

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from apflow.core.extensions.registry import get_registry
from apflow.logger import get_logger

logger = get_logger(__name__)


def generate_executor_docs(output_dir: Path) -> None:
    """
    Generate documentation for all executors

    Args:
        output_dir: Directory to write documentation files
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = get_registry()
    executors = registry.list_executors()

    logger.info(f"Found {len(executors)} executors to document")

    index_lines = [
        "# Executor Reference\n",
        "\nAuto-generated documentation for all available executors.\n\n",
    ]

    for executor in executors:
        try:
            executor_id = executor.id
            doc_file = output_dir / f"{executor_id}.md"

            logger.info(f"Generating documentation for: {executor_id}")
            doc_content = generate_single_executor_doc(executor)

            doc_file.write_text(doc_content, encoding="utf-8")

            index_lines.append(
                f"- [{executor.name}]({executor_id}.md) - {getattr(executor, 'description', '')[:100]}\n"
            )

        except Exception as e:
            logger.error(f"Failed to generate docs for {getattr(executor, 'id', 'unknown')}: {e}")
            continue

    index_file = output_dir / "index.md"
    index_file.write_text("".join(index_lines), encoding="utf-8")

    logger.info(f"Documentation generated in: {output_dir}")


def generate_single_executor_doc(executor: Any) -> str:
    """
    Generate markdown documentation for a single executor

    Args:
        executor: Executor extension instance

    Returns:
        Markdown documentation string
    """
    lines = []

    lines.append(f"# {executor.name}\n\n")
    lines.append(f"**Executor ID:** `{executor.id}`\n\n")

    if getattr(executor, "description", ""):
        lines.append(f"## Description\n\n{getattr(executor, 'description', '')}\n\n")

    if hasattr(executor, "tags") and executor.tags:
        lines.append(f"**Tags:** {', '.join(executor.tags)}\n\n")

    try:
        registry = get_registry()
        instance = registry.create_executor_instance(executor.id, inputs={})

        if instance and hasattr(instance, "get_input_schema"):
            schema = instance.get_input_schema()
            if schema and isinstance(schema, dict):
                lines.append("## Input Schema\n\n")
                lines.append(format_schema(schema))
                lines.append("\n")

        if instance and hasattr(instance, "get_output_schema"):
            output_schema = instance.get_output_schema()
            if output_schema and isinstance(output_schema, dict):
                lines.append("## Output Schema\n\n")
                lines.append(format_schema(output_schema))
                lines.append("\n")

    except Exception as e:
        logger.debug(f"Could not get schemas for {executor.id}: {e}")

    example = generate_example(executor.id)
    if example:
        lines.append("## Example\n\n")
        lines.append("```json\n")
        lines.append(example)
        lines.append("\n```\n\n")

    return "".join(lines)


def format_schema(schema: Dict[str, Any]) -> str:
    """
    Format JSON schema as markdown table

    Args:
        schema: JSON schema dictionary

    Returns:
        Markdown formatted schema
    """
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    if not properties:
        return "*No properties defined*\n"

    lines = [
        "| Field | Type | Required | Description |\n",
        "|-------|------|----------|-------------|\n",
    ]

    for prop_name, prop_info in properties.items():
        prop_type = prop_info.get("type", "any")
        is_required = "✓" if prop_name in required else ""
        description = prop_info.get("description", "")

        lines.append(f"| `{prop_name}` | {prop_type} | {is_required} | {description} |\n")

    return "".join(lines)


def generate_example(executor_id: str) -> str:
    """
    Generate example task JSON for executor

    Args:
        executor_id: Executor ID

    Returns:
        Example JSON string
    """
    examples = {
        "rest_executor": {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "name": "Fetch User Data",
            "schemas": {"method": "rest_executor"},
            "inputs": {
                "url": "https://api.example.com/users/123",
                "method": "GET",
                "headers": {"Accept": "application/json"},
            },
        },
        "send_email_executor": {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "name": "Notify On Completion",
            "schemas": {"method": "send_email_executor"},
            "inputs": {
                "provider": "smtp",
                "to": ["ops@example.com"],
                "subject": "Task done",
                "body": "Pipeline finished",
            },
        },
        "aggregate_results_executor": {
            "id": "550e8400-e29b-41d4-a716-446655440002",
            "name": "Aggregate Upstream Results",
            "schemas": {"method": "aggregate_results_executor"},
            "inputs": {},
        },
        "apflow_api_executor": {
            "id": "550e8400-e29b-41d4-a716-446655440003",
            "name": "Call apflow API",
            "schemas": {"method": "apflow_api_executor"},
            "inputs": {"endpoint": "/tasks", "payload": {"limit": 10}},
        },
    }

    if executor_id in examples:
        return json.dumps(examples[executor_id], indent=2)

    return json.dumps(
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "name": f"Example {executor_id.replace('_', ' ').title()}",
            "schemas": {"method": executor_id},
            "inputs": {},
        },
        indent=2,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate executor documentation")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "executors",
        help="Output directory for documentation",
    )
    parser.add_argument("--executor-id", type=str, help="Generate docs for specific executor only")

    args = parser.parse_args()

    if args.executor_id:
        registry = get_registry()
        executors = [e for e in registry.list_executors() if e.id == args.executor_id]
        if not executors:
            print(f"Executor not found: {args.executor_id}")
            sys.exit(1)

        doc = generate_single_executor_doc(executors[0])
        output_file = args.output_dir / f"{args.executor_id}.md"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(doc, encoding="utf-8")
        print(f"Generated: {output_file}")
    else:
        generate_executor_docs(args.output_dir)
        print(f"Generated documentation in: {args.output_dir}")
