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
    cli.add_command(info)
    cli.add_command(worker)

    return cli


@click.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option("--name", default="apflow", help="Agent name in A2A Agent Card")
@click.option("--explorer", is_flag=True, help="Enable A2A Explorer UI")
@click.option("--metrics", is_flag=True, help="Enable /metrics endpoint")
@click.option(
    "--sys-modules",
    is_flag=True,
    help="Expose apcore system modules (sys.*) as A2A skills",
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
    sys_modules: bool,
    cors: Optional[str],
    db: Optional[str],
    cluster: bool,
    log_level: Optional[str],
) -> None:
    """Start A2A HTTP server (internal network service)."""
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

    app = create_app(connection_string=db, cluster=False)

    cors_origins = [s.strip() for s in cors.split(",")] if cors else None

    click.echo(f"Starting A2A server on {host}:{port}")
    click.echo(f"Modules: {len(list(app.registry.list()))}")
    if explorer:
        click.echo(f"Explorer: http://{host}:{port}/explorer")

    from apcore_a2a import serve as a2a_serve

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
        cors_origins=cors_origins,
        log_level=log_level,
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
@click.option("--db", default=None, help="Database connection string")
@click.option("--log-level", default=None, help="Log level")
def mcp(
    transport: str,
    host: str,
    port: int,
    explorer: bool,
    metrics: bool,
    approval: bool,
    db: Optional[str],
    log_level: Optional[str],
) -> None:
    """Start MCP server (AI agent tool integration)."""
    from apflow.app import create_app

    app = create_app(connection_string=db)

    if transport != "stdio":
        click.echo(f"Starting MCP server ({transport}) on {host}:{port}")
        click.echo(f"Tools: {len(list(app.registry.list()))}")
        if explorer:
            click.echo(f"Explorer: http://{host}:{port}/explorer")
        if approval:
            click.echo("Approval: enabled (InMemoryApprovalStore — dev only)")
    else:
        # stdio mode — no console output (would corrupt protocol)
        pass

    from apcore_mcp import InMemoryApprovalStore, serve as mcp_serve

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
@click.option("--log-level", default=None, help="Log level")
def worker(db: str, node_id: Optional[str], log_level: Optional[str]) -> None:
    """Start a distributed worker node (requires PostgreSQL)."""
    import asyncio

    from apflow.app import create_app

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
