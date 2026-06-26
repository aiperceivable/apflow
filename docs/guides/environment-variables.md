# Environment Variables

apflow can be configured entirely through environment variables. Each variable maps to a YAML config key with the `APFLOW_` prefix.

## Configuration Priority

```
Defaults → apflow.yaml → Environment Variables (highest priority)
```

## Core Variables

| Variable | Config Key | Default | Description |
|----------|-----------|---------|-------------|
| `APFLOW_CONFIG` | — | — | Explicit path to config YAML file |
| `APFLOW_API_SERVER_URL` | `api.server_url` | None | Remote API server URL (used by API-gateway clients; the scheduler no longer has an API mode — it runs in-process) |
| `APFLOW_API_TIMEOUT` | `api.timeout` | 30.0 | API request timeout (seconds) |
| `APFLOW_API_RETRY_ATTEMPTS` | `api.retry_attempts` | 3 | API retry attempts |
| `APFLOW_API_RETRY_BACKOFF` | `api.retry_backoff` | 1.0 | Initial retry backoff (seconds) |

## Authentication (JWT)

These configure the JWT Bearer auth shared by the REST, A2A, and MCP servers
(enabled per server with `--auth`). The same token validates on every surface.
See [REST / HTTP API → Authentication](../features/rest-api.md#authentication-jwt).

| Variable | Config Key | Default | Description |
|----------|-----------|---------|-------------|
| `APFLOW_API_JWT_SECRET` | `api.jwt_secret` | None | Shared secret for **symmetric** algorithms (HS256/384/512). Also used to auto-generate the scheduler's admin token. Use ≥32 bytes in production. |
| `APFLOW_API_JWT_ALGORITHM` | `api.jwt_algorithm` | HS256 | Allowed algorithm(s); comma-separated for multiple (e.g. `RS256,RS512`) |
| `APFLOW_API_JWT_PUBLIC_KEY` | `api.jwt_public_key` | None | PEM public key for **asymmetric** algorithms (RS*/ES*/PS*); used to *verify* tokens |
| `APFLOW_API_JWT_PUBLIC_KEY_PATH` | `api.jwt_public_key_path` | None | Path to a PEM public-key file (alternative to inline `jwt_public_key`) |
| `APFLOW_API_JWT_AUDIENCE` | `api.jwt_audience` | None | Expected `aud` claim (optional) |
| `APFLOW_API_JWT_ISSUER` | `api.jwt_issuer` | None | Expected `iss` claim (optional) |

> **HS256 vs RS256.** Symmetric (HS*) verifies with `jwt_secret`. Asymmetric
> (RS*/ES*/PS*) verifies with the **public key** — set `jwt_algorithm=RS256` and
> provide `jwt_public_key` (or `_path`); the private key signs tokens elsewhere
> and is never needed by the servers.

## Storage

| Variable | Config Key | Default | Description |
|----------|-----------|---------|-------------|
| `DATABASE_URL` | — | SQLite file | Database connection string |
| `APFLOW_DATABASE_URL` | — | — | Alternative to DATABASE_URL |
| `APFLOW_STORAGE_DIALECT` | `storage.dialect` | sqlite | Storage backend (sqlite/postgresql) |
| `APFLOW_STORAGE_PATH` | `storage.path` | .data/apflow.db | SQLite file path |

## Governance

| Variable | Config Key | Default | Description |
|----------|-----------|---------|-------------|
| `APFLOW_GOVERNANCE_DEFAULT_POLICY` | `governance.default_policy` | None | Default cost policy name |
| `APFLOW_GOVERNANCE_DOWNGRADE_CHAIN` | `governance.downgrade_chain` | [] | Comma-separated model names |

## Durability

| Variable | Config Key | Default | Description |
|----------|-----------|---------|-------------|
| `APFLOW_DURABILITY_MAX_ATTEMPTS` | `durability.max_attempts` | 3 | Default retry attempts |
| `APFLOW_DURABILITY_BACKOFF_STRATEGY` | `durability.backoff_strategy` | exponential | fixed/exponential/linear |
| `APFLOW_DURABILITY_CIRCUIT_BREAKER_THRESHOLD` | `durability.circuit_breaker_threshold` | 5 | Consecutive failures to trip breaker |

## Distributed

| Variable | Default | Description |
|----------|---------|-------------|
| `APFLOW_CLUSTER_ENABLED` | false | Enable distributed mode |
| `APFLOW_NODE_ROLE` | auto | Node role (auto/leader/worker) |
| `APFLOW_NODE_ID` | auto-generated | Unique node identifier |
| `APFLOW_MAX_PARALLEL_TASKS` | 4 | Max concurrent task executions |

## YAML Config File

Place `apflow.yaml` in your project root or `~/.aiperceivable/apflow/`:

```yaml
api:
  server_url: http://localhost:8000
  timeout: 60.0
  # HS256 (symmetric): one shared secret signs and verifies.
  jwt_secret: ${APFLOW_API_JWT_SECRET}  # env var reference
  jwt_algorithm: HS256
  # RS256 (asymmetric): set algorithm + public key; the private key signs elsewhere.
  # jwt_algorithm: RS256
  # jwt_public_key_path: /etc/apflow/jwt_public.pem

storage:
  dialect: sqlite
  path: .data/apflow.db

governance:
  default_policy: auto-downgrade
  downgrade_chain:
    - claude-opus-4
    - claude-sonnet-4
    - claude-haiku-4

durability:
  max_attempts: 5
  backoff_strategy: exponential
  circuit_breaker_threshold: 10
```
