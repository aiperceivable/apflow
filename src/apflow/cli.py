"""
apflow CLI entry point.

Uses apcore-cli to provide full module discovery and execution CLI,
plus apflow-specific commands:
  apflow serve   — Start A2A HTTP server
  apflow mcp     — Start MCP server (stdio or HTTP)
  apflow info    — Show version and config info
  apflow worker  — Start a distributed worker node
"""

import sys
from typing import Any, Optional

import click

from apflow.logger import get_logger

logger = get_logger(__name__)


def _require_jwt_key(auth: bool, face: str) -> None:
    """Fail early if --auth is requested without a usable JWT verification key.

    HS256 verifies with api.jwt_secret; RS*/ES*/PS* verify with a public key.
    Enabling auth without the relevant key would otherwise raise deep in the
    serve call (or run unauthenticated); surface a clear actionable error.
    """
    if not auth:
        return
    from apflow.api.auth import resolve_verification_key
    from apflow.core.config_manager import get_config_manager

    if not resolve_verification_key(get_config_manager()):
        raise click.ClickException(
            f"{face} --auth requires a JWT verification key. Set APFLOW_API_JWT_SECRET "
            "for HS256, or APFLOW_API_JWT_PUBLIC_KEY / api.jwt_public_key_path for "
            "RS256 (matching api.jwt_algorithm)."
        )


def _require_webhook_secret(webhook: bool, auth: bool, webhook_secret: Optional[str]) -> None:
    """Fail early if the webhook is mounted under --auth without its own HMAC secret.

    The webhook path is JWT-exempt (it carries standalone HMAC auth), so mounting
    it with --auth but no --webhook-secret would silently leave POST
    /webhook/trigger/{task_id} unauthenticated while the operator believes --auth
    protects the whole surface. Refuse that combination loudly.
    """
    if webhook and auth and not webhook_secret:
        raise click.ClickException(
            "--webhook is exempt from JWT auth, so under --auth it must carry its "
            "own credential: pass --webhook-secret to HMAC-protect the inbound "
            "webhook (otherwise POST /webhook/trigger/{task_id} would be "
            "unauthenticated)."
        )


def _build_module_subgroups(module_group: Any) -> list[click.Group]:
    """Extract task.* and schedule.* module commands into top-level Click groups.

    Transforms flat dot-namespaced names (task.create, schedule.set) into
    proper subgroups so users invoke `apflow task create` instead of
    `apflow apflow task.create`.

    Works with apcore-cli's _LazyGroup, which exposes commands via
    list_commands()/get_command() rather than a pre-populated .commands dict.
    """
    group_meta: dict[str, str] = {
        "task": "Task management (create, list, get, update, clone, link, etc.).",
        "schedule": "Schedule management (set, due, trigger, complete, etc.).",
    }
    groups: dict[str, click.Group] = {
        name: click.Group(name, help=help_text) for name, help_text in group_meta.items()
    }

    names: list[str] = (
        module_group.list_commands(None) if hasattr(module_group, "list_commands") else []
    )
    for cmd_name in names:
        if "." not in cmd_name:
            continue
        prefix, subname = cmd_name.split(".", 1)
        if prefix not in groups:
            continue
        cmd = module_group.get_command(None, cmd_name)
        if cmd is not None:
            groups[prefix].add_command(cmd, name=subname)

    return [g for g in groups.values() if g.commands]


def _build_cli() -> click.Group:
    """Build the apflow CLI by extending apcore-cli with apflow-specific commands."""
    from apflow.app import create_app

    app = create_app()

    from apcore_cli import create_cli

    from apflow import __version__

    # apcore-cli >= 0.10 registers --version and applies the help/description itself
    # when given version=/description=, so the old click.params surgery is no longer
    # needed (it also no longer leaks the SDK's own version — see apcore-cli Issue #18).
    cli = create_cli(
        registry=app.registry,
        prog_name="apflow",
        version=__version__,
        description="apflow — AI-Perceivable Distributed Orchestration",
    )

    # Register apflow-specific commands
    cli.add_command(serve)
    cli.add_command(mcp)
    cli.add_command(rest)
    cli.add_command(info)
    cli.add_command(worker)
    cli.add_command(scheduler)

    # Promote task.* and schedule.* into top-level groups (apflow task create, etc.)
    # apcore-cli returns a _LazyGroup (not a plain click.Group), so we resolve it
    # via get_command() rather than accessing .commands directly.
    # get_command() also triggers _build_group_map() and caches the lazy group in
    # _group_cache["apflow"], so we can safely remove "apflow" from _group_map
    # to suppress the "Groups: apflow (25 commands)" line in --help output.
    # The group remains fully accessible as `apflow apflow <cmd>` via the cache.
    module_grp = cli.get_command(None, "apflow")
    if module_grp is not None:
        for grp in _build_module_subgroups(module_grp):
            cli.add_command(grp)
        if hasattr(cli, "_group_map"):
            cli._group_map.pop("apflow", None)

    return cli


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option("--name", default="apflow", help="Agent name in A2A Agent Card")
@click.option("--explorer", is_flag=True, help="Enable A2A Explorer UI")
@click.option("--metrics", is_flag=True, help="Enable /metrics endpoint")
@click.option(
    "--all",
    "all_protocols",
    is_flag=True,
    help="Serve REST + A2A + MCP together on one port (/, /a2a, /mcp)",
)
@click.option(
    "--sys-modules",
    is_flag=True,
    help="Expose apcore system modules (sys.*) as A2A skills",
)
@click.option(
    "--push-notifications",
    is_flag=True,
    help="Enable A2A push notifications (POST task results to a client-supplied webhook)",
)
@click.option(
    "--webhook",
    is_flag=True,
    help="With --all: mount POST /webhook/trigger/{task_id} for external schedulers",
)
@click.option(
    "--webhook-secret",
    default=None,
    help="Optional HMAC-SHA256 shared secret to authenticate webhook requests",
)
@click.option(
    "--auth",
    is_flag=True,
    help="Require a JWT Bearer token (needs a JWT verification key: HS256 secret or RS256 public key)",
)
@click.option(
    "--scheduler",
    is_flag=True,
    help="With --all: run the built-in scheduler poll loop in this process (single-node)",
)
@click.option("--cors", default=None, help="CORS origins (comma-separated)")
@click.option("--db", default=None, help="Database connection string")
@click.option(
    "--cluster",
    is_flag=True,
    help="Unsupported: serve does not run the cluster runtime — use `apflow worker`",
)
@click.option("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING/ERROR)")
def serve(
    host: str,
    port: int,
    name: str,
    explorer: bool,
    metrics: bool,
    all_protocols: bool,
    sys_modules: bool,
    push_notifications: bool,
    webhook: bool,
    webhook_secret: Optional[str],
    auth: bool,
    scheduler: bool,
    cors: Optional[str],
    db: Optional[str],
    cluster: bool,
    log_level: Optional[str],
) -> None:
    """Start the A2A HTTP server, or all protocols together with --all."""
    from apflow import __version__
    from apflow.app import create_app

    # `serve` runs a single A2A process and has no event loop hook to drive the
    # distributed runtime's leader election / lease loops. Constructing the runtime
    # without starting it (as before) silently bypassed leader gating, so every
    # `serve --cluster` node executed tasks uncoordinated. Fail loudly instead and
    # direct operators to the dedicated worker command.
    if cluster:
        raise click.ClickException(
            "`serve` does not run the distributed cluster runtime. "
            "Start distributed nodes with `apflow worker --db <postgres-url>` instead."
        )

    _require_jwt_key(auth, "serve")
    _require_webhook_secret(webhook, auth, webhook_secret)

    # The in-process scheduler needs the server's own event loop; the plain A2A
    # path runs apcore-a2a's blocking serve, which we can't hook. Require --all.
    if scheduler and not all_protocols:
        raise click.ClickException(
            "--scheduler needs the unified server's event loop; use `serve --all --scheduler` "
            "(or run the standalone `apflow scheduler` process)."
        )

    app = create_app(connection_string=db, cluster=False)

    cors_origins = [s.strip() for s in cors.split(",")] if cors else None

    if all_protocols:
        from apflow.api import serve_all

        click.echo(f"Starting unified REST + A2A + MCP server on {host}:{port}")
        click.echo(f"  REST: http://{host}:{port}/  (docs at /docs)")
        click.echo(f"  A2A:  http://{host}:{port}/a2a")
        click.echo(f"  MCP:  http://{host}:{port}/mcp")
        if webhook:
            click.echo(f"  Webhook: http://{host}:{port}/webhook/trigger/{{task_id}}")
        if auth:
            click.echo("  Auth: JWT Bearer required (REST modules, A2A, MCP)")
        if scheduler:
            click.echo("  Scheduler: in-process poll loop enabled")
        click.echo(f"Modules: {app.registry.count}")
        serve_all(
            app.registry,
            host=host,
            port=port,
            title=name,
            version=__version__,
            cors_origins=cors_origins,
            log_level=log_level,
            explorer=explorer,
            metrics=metrics,
            sys_modules=sys_modules,
            push_notifications=push_notifications,
            webhook=webhook,
            webhook_secret=webhook_secret,
            auth=auth,
            scheduler=scheduler,
        )
        return

    click.echo(f"Starting A2A server on {host}:{port}")
    click.echo(f"Modules: {len(list(app.registry.list()))}")
    if explorer:
        click.echo(f"Explorer: http://{host}:{port}/explorer")
    if auth:
        click.echo("Auth: JWT Bearer required")

    from apcore_a2a import serve as a2a_serve

    from apflow.api.auth import build_a2a_authenticator

    # build_a2a_authenticator returns None when no secret is set; _require_jwt_key
    # above already guarantees a secret when auth is on, so a2a_auth is non-None here.
    a2a_auth = build_a2a_authenticator() if auth else None

    # A2A protocol 1.0: the Agent Card now carries a version; apcore-a2a resolves
    # execution_timeout from the APCORE_A2A Config Bus when omitted.
    a2a_serve(
        app.registry,
        host=host,
        port=port,
        name=name,
        version=__version__,
        description="apflow AI-Perceivable Distributed Orchestration",
        url=f"http://{host}:{port}",
        explorer=explorer,
        metrics=metrics,
        sys_modules=sys_modules,
        auth=a2a_auth,
        cors_origins=cors_origins,
        log_level=log_level,
        push_notifications=push_notifications,
    )


@click.command()
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "streamable-http", "sse"]),
    help="MCP transport mode",
)
@click.option("--host", default="127.0.0.1", help="Bind host (HTTP modes)")
@click.option("--port", default=8001, type=int, help="Bind port (HTTP modes)")
@click.option("--explorer", is_flag=True, help="Enable MCP Tool Explorer UI")
@click.option(
    "--metrics",
    is_flag=True,
    help="Enable observability (metrics + usage endpoints under /api/usage)",
)
@click.option(
    "--approval",
    is_flag=True,
    help=(
        "Enable async human-approval workflow (Phase B). "
        "Registers the __apcore_approval_check meta-tool so AI agents can poll "
        "for out-of-band approvals without blocking the MCP connection. "
        "Uses InMemoryApprovalStore — suitable for local dev only. "
        "Production deployments should wire a persistent ApprovalStore via "
        "APCoreMCP(approval_store=...) directly."
    ),
)
@click.option(
    "--auth",
    is_flag=True,
    help="Require a JWT Bearer token on HTTP transports (HS256 secret or RS256 public key)",
)
@click.option("--db", default=None, help="Database connection string")
@click.option("--log-level", default=None, help="Log level")
def mcp(
    transport: str,
    host: str,
    port: int,
    explorer: bool,
    metrics: bool,
    approval: bool,
    auth: bool,
    db: Optional[str],
    log_level: Optional[str],
) -> None:
    """Start MCP server (AI agent tool integration)."""
    from apflow.app import create_app

    _require_jwt_key(auth, "mcp")

    app = create_app(connection_string=db)

    if transport != "stdio":
        click.echo(f"Starting MCP server ({transport}) on {host}:{port}")
        click.echo(f"Tools: {len(list(app.registry.list()))}")
        if explorer:
            click.echo(f"Explorer: http://{host}:{port}/explorer")
        if approval:
            click.echo("Approval: enabled (InMemoryApprovalStore — dev only)")
        if auth:
            click.echo("Auth: JWT Bearer required")
    else:
        # stdio mode — no console output (would corrupt protocol)
        pass

    from apcore_mcp import InMemoryApprovalStore, serve as mcp_serve

    from apflow.api.auth import build_mcp_authenticator

    # require_auth is passed explicitly (not left to apcore-mcp's default of True):
    # without it, the server would default to require_auth=True yet have no
    # authenticator wired, rejecting every request. auth=False → both off.
    authenticator = build_mcp_authenticator() if auth else None

    mcp_serve(
        app.registry,
        transport=transport,
        host=host,
        port=port,
        name="apflow",
        explorer=explorer,
        observability=metrics,
        log_level=log_level,
        approval_store=InMemoryApprovalStore() if approval else None,
        authenticator=authenticator,
        require_auth=auth,
    )


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8080, type=int, help="Bind port")
@click.option("--cors", default=None, help="CORS origins (comma-separated)")
@click.option(
    "--webhook",
    is_flag=True,
    help="Mount POST /webhook/trigger/{task_id} for external schedulers (cron/K8s)",
)
@click.option(
    "--webhook-secret",
    default=None,
    help="Optional HMAC-SHA256 shared secret to authenticate webhook requests",
)
@click.option(
    "--auth",
    is_flag=True,
    help="Require a JWT Bearer token on module calls (HS256 secret or RS256 public key)",
)
@click.option(
    "--scheduler",
    is_flag=True,
    help="Run the built-in scheduler poll loop in this process (single-node)",
)
@click.option("--db", default=None, help="Database connection string")
@click.option("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING/ERROR)")
def rest(
    host: str,
    port: int,
    cors: Optional[str],
    webhook: bool,
    webhook_secret: Optional[str],
    auth: bool,
    scheduler: bool,
    db: Optional[str],
    log_level: Optional[str],
) -> None:
    """Start REST/HTTP server (registry-driven JSON API + OpenAPI docs)."""
    from apflow import __version__
    from apflow.api import serve_rest
    from apflow.app import create_app

    _require_jwt_key(auth, "REST")
    _require_webhook_secret(webhook, auth, webhook_secret)

    app = create_app(connection_string=db)

    cors_origins = [s.strip() for s in cors.split(",")] if cors else None

    click.echo(f"Starting REST server on {host}:{port}")
    click.echo(f"Modules: {app.registry.count}")
    click.echo(f"Docs:    http://{host}:{port}/docs")
    if webhook:
        click.echo(f"Webhook: http://{host}:{port}/webhook/trigger/{{task_id}}")
    if auth:
        click.echo("Auth:    JWT Bearer required (module calls)")
    if scheduler:
        click.echo("Scheduler: in-process poll loop enabled")

    serve_rest(
        app.registry,
        host=host,
        port=port,
        title="apflow",
        version=__version__,
        cors_origins=cors_origins,
        log_level=log_level,
        webhook=webhook,
        webhook_secret=webhook_secret,
        auth=auth,
        scheduler=scheduler,
    )


@click.command()
def info() -> None:
    """Show apflow version and configuration."""
    from apflow import __version__
    from apflow.core.config_manager import get_config_manager

    cm = get_config_manager()

    click.echo(f"apflow {__version__}")
    click.echo(f"Python {sys.version.split()[0]}")
    click.echo()
    click.echo("Configuration:")
    click.echo(f"  Storage:    {cm.get('storage.dialect', 'sqlite')}")
    click.echo(f"  API Server: {cm.api_server_url or 'not configured'}")
    click.echo(f"  JWT Secret: {'configured' if cm.jwt_secret else 'not configured'}")
    click.echo()

    # Show registered modules
    try:
        from apflow.app import create_app

        app = create_app()
        modules = list(app.registry.list())
        click.echo(f"Modules: {len(modules)}")
        for m in sorted(modules):
            click.echo(f"  {m}")
    except Exception as e:
        click.echo(f"Registry: error ({e})")


@click.command()
@click.option("--db", required=True, help="PostgreSQL connection string (required for cluster)")
@click.option("--node-id", default=None, help="Worker node ID (auto-generated if omitted)")
@click.option(
    "--scheduler",
    is_flag=True,
    default=False,
    help="Enable leader-side scheduling: the elected leader dispatches due schedules "
    "as pending run instances for workers to execute.",
)
@click.option("--log-level", default=None, help="Log level")
def worker(db: str, node_id: Optional[str], scheduler: bool, log_level: Optional[str]) -> None:
    """Start a distributed worker node (requires PostgreSQL)."""
    import asyncio

    from apflow.app import create_app

    if log_level:
        from apflow.logger import setup_logging

        setup_logging(log_level)

    click.echo(f"Starting worker node: {node_id or 'auto'}")

    # The worker owns the distributed runtime lifecycle, so build the app without
    # cluster auto-init (which only constructs, never starts, the runtime).
    app = create_app(connection_string=db, cluster=False)

    try:
        from apflow.core.distributed.config import DistributedConfig, is_postgresql
        from apflow.core.distributed.runtime import DistributedRuntime
        from apflow.core.execution.task_executor import TaskExecutor
    except ImportError:
        click.echo("Error: distributed module not available", err=True)
        return

    # Distributed coordination needs PostgreSQL's atomic guarantees; reject other
    # backends loudly rather than silently degrading to the non-atomic SQLite path.
    if not is_postgresql(app.session):
        raise click.ClickException(
            "Worker mode requires a PostgreSQL --db connection (got a non-PostgreSQL "
            "database). Provide a postgresql:// connection string."
        )

    config = DistributedConfig.from_env()
    config.enabled = True
    if scheduler:
        config.scheduling_enabled = True
    if node_id:
        config.node_id = node_id
    # Validate timing invariants (renew < lease, positive intervals) and auto-generate
    # node_id when omitted, instead of constructing a misconfigured runtime.
    try:
        config.validate_and_initialize()
    except ValueError as exc:
        raise click.ClickException(f"Invalid distributed configuration: {exc}")

    async def execute_one(task: Any) -> dict:
        """Execute a single claimed task via the shared TaskExecutor singleton."""
        return await TaskExecutor().execute_tasks(
            [{"id": task.id}],
            require_existing_tasks=True,
        )

    runtime = DistributedRuntime.from_session(app.session, config, task_executor=execute_one)

    async def run() -> None:
        try:
            await runtime.start()
        finally:
            await runtime.shutdown()

    click.echo(f"Worker {config.node_id} running (Ctrl+C to stop)")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        click.echo("Worker stopped")


@click.command()
@click.option("--poll-interval", default=60, type=int, help="Seconds between due-task checks")
@click.option("--max-concurrent", default=10, type=int, help="Max tasks to execute concurrently")
@click.option("--user-id", default=None, help="Only process this user's scheduled tasks")
@click.option("--task-timeout", default=3600, type=int, help="Per-task execution timeout (seconds)")
@click.option("--db", default=None, help="Database connection string")
@click.option("--verbose", is_flag=True, help="Print each task execution result")
@click.option("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING/ERROR)")
def scheduler(
    poll_interval: int,
    max_concurrent: int,
    user_id: Optional[str],
    task_timeout: int,
    db: Optional[str],
    verbose: bool,
    log_level: Optional[str],
) -> None:
    """Run the internal scheduler: poll for due scheduled tasks and execute them.

    Runs in the foreground until interrupted (Ctrl+C). This is the pull-based
    complement to the inbound ``schedule.trigger`` module (push). Executes tasks
    in-process using direct database access.
    """
    import asyncio

    from apflow.scheduler.base import SchedulerConfig
    from apflow.scheduler.internal import run_scheduler

    if log_level:
        from apflow.logger import setup_logging

        setup_logging(log_level)

    # The scheduler's direct-DB path uses the global pooled session, which binds
    # its connection on first use from APFLOW_DATABASE_URL / config — not from
    # --db. Initialize the pool against the requested DB up front so --db is
    # honored end to end rather than silently connecting to the default database.
    if db:
        from apflow.core.storage.factory import get_session_pool_manager

        get_session_pool_manager().initialize(connection_string=db)

    config = SchedulerConfig(
        poll_interval=poll_interval,
        max_concurrent_tasks=max_concurrent,
        task_timeout=task_timeout,
        user_id=user_id,
    )

    click.echo(f"Starting scheduler (poll={poll_interval}s, max_concurrent={max_concurrent})")
    click.echo("Press Ctrl+C to stop")
    try:
        asyncio.run(run_scheduler(config, verbose=verbose))
    except KeyboardInterrupt:
        click.echo("Scheduler stopped")


# Lazily built Click group for use by tests (via CliRunner) and entry point.
_cli_instance: click.Group | None = None


def cli() -> click.Group:
    """Get the apflow CLI group (built once, cached)."""
    global _cli_instance
    if _cli_instance is None:
        _cli_instance = _build_cli()
    return _cli_instance


def main() -> None:
    """Entry point for apflow CLI."""
    cli()(standalone_mode=True)


if __name__ == "__main__":
    main()
