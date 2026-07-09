---
description: Configuration architecture for apflow defaults, YAML files, environment overrides, and runtime settings.
---

# Table Name Configuration

## Overview

The `TaskModel` table name is configurable via environment variable to avoid conflicts with other frameworks or to align with your naming conventions.

## Default Table Name

**Default**: `apflow_tasks`

The table name uses the prefix `apflow_` (short for "apflow") to distinguish it from:
- A2A Protocol's default `tasks` table (for execution instances)
- Other frameworks that might use `tasks` table

## Configuration

### Environment Variable

Set the `APFLOW_TASK_TABLE_NAME` environment variable to customize the table name:

```bash
export APFLOW_TASK_TABLE_NAME="my_custom_tasks"
```

### Example Usage

```python
import os
os.environ["APFLOW_TASK_TABLE_NAME"] = "my_custom_tasks"

# Import after setting environment variable
from apflow.core.storage.sqlalchemy.models import TaskModel

# TaskModel will use "my_custom_tasks" as table name
print(TaskModel.__tablename__)  # Output: "my_custom_tasks"
```

**Important**: You must set the environment variable **before** importing the model for the first time.

## Table Purpose

The `TaskModel` table stores:
- **Task definitions**: Orchestration metadata (dependencies, priority, schemas)
- **Execution results**: Latest execution status, result, error, progress
- **Task tree structure**: Parent-child relationships

This is different from A2A Protocol's `tasks` table, which stores:
- **Execution instances**: LLM message context, history, artifacts
- **Dynamic execution data**: Per-execution information

## Migration

If you need to rename an existing table, use SQL:

```sql
-- Rename existing table
ALTER TABLE tasks RENAME TO apflow_tasks;

-- Or if using custom name
ALTER TABLE tasks RENAME TO my_custom_tasks;
```

## Best Practices

1. **Use prefix**: Consider using a prefix (e.g., `myapp_tasks`) to avoid conflicts
2. **Set early**: Set the environment variable before any imports
3. **Document**: Document your custom table name in your project documentation
4. **Consistency**: Use the same table name across all environments (dev, staging, prod)

## Examples

### Development
```bash
export APFLOW_TASK_TABLE_NAME="apflow_tasks_dev"
```

### Production
```bash
export APFLOW_TASK_TABLE_NAME="apflow_tasks_prod"
```

### Multi-tenant
```bash
export APFLOW_TASK_TABLE_NAME="apflow_tasks_tenant_1"
```

