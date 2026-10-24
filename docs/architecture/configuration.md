---
description: Configuration architecture for apflow defaults, YAML files, environment overrides, and runtime settings.
---

# Table Name Configuration

## Service Deployment Configuration

`create_app(connection_string=...)` and CLI `--db` take precedence over
`storage.connection_string`. If neither is set, the existing environment
resolution applies (`DATABASE_URL`, then `APFLOW_DATABASE_URL`, then the SQLite
default). HTTP operations, schedulers created by the server, and dispatchers
share the application's selected engine. CLI help and version discovery do not
initialize storage.

The following YAML keys are consumed by `ConfigManager`:

```yaml
storage:
  connection_string: "postgresql+psycopg://user:password@database/apflow"
security:
  require_identity: true
  admin_roles: [apflow.admin]
  allowed_modules: ["apflow.*"]
  module_roles:
    "apflow.task.reconcile": [apflow.admin]
    "apflow.budget.reconcile": [apflow.admin]
  # Optional path to an apcore ACL YAML policy.
  acl_path: null
  credentials:
    - id: training-service
      env_var: TRAINING_SERVICE_TOKEN
      header: Authorization
      prefix: "Bearer "
      allowed_profiles: [training-api]
network:
  profiles:
    - id: training-api
      base_url: "https://training.internal.example/v1/"
      allow_private: true
      allowed_methods: [POST]
      allowed_path_prefixes: [/v1/training/]
      allowed_subjects: [training-worker]
      credential_ref: training-service
      replay_safe: false
service:
  required_modules: [apflow.rest_executor]
  required_schema_versions: {}
```

Use module IDs returned by the deployment's registry for `required_modules`.
An absent module, failed schema discovery, or mismatched configured schema
version prevents startup/readiness and new task admission. Optional executor
discovery remains optional. Configure `required_schema_versions` only for
plugins publishing that metadata.

Run network-facing services with `--auth` and configured JWT verification.
`require_identity` also denies anonymous module execution at the bridge. Trusted
local library use remains compatible when this setting is false. Connection
profile `allowed_subjects` matches the verified identity ID; role policy is a
separate capability restriction.

Task inputs select a profile using `connection_profile: training-api` and a URL
within its configured origin/path/method policy. Private destinations remain
blocked by default; enabling one profile does not authorize arbitrary internal
addresses or task-supplied credentials. Keep deployment egress restrictions to
enforce network boundaries at connection time, including DNS changes. Never put
real database passwords or credential values into tracked configuration.

For reproducible integration testing, install from `uv.lock` (`uv sync --frozen`)
or use equivalent lock-derived constraints. CI installs constrained dependencies
and runs `pip check` plus the dependency closure test before the full suite.

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

