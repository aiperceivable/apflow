"""
apflow CLI entry point.

Provides commands:
  apflow serve   — Start A2A HTTP server
  apflow mcp     — Start MCP server (stdio or HTTP)
  apflow info    — Show version and config info
"""

import sys
from typing import Optional

import click

from apflow.logger import get_logger

logger = get_logger(__name__)


@click.group()
@click.version_option(package_name="apflow")
def cli() -> None:
    """apflow — AI-Perceivable Distributed Orchestration"""
    pass


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option("--name", default="apflow", help="Agent name in A2A Agent Card")
@click.option("--explorer", is_flag=True, help="Enable A2A Explorer UI")
@click.option("--metrics", is_flag=True, help="Enable /metrics endpoint")
@click.option("--cors", default=None, help="CORS origins (comma-separated)")
@click.option("--db", default=None, help="Database connection string")
@click.option(
    "--cluster", is_flag=True, help="Enable distributed cluster mode (requires PostgreSQL)"
)
@click.option("--log-level", default=None, help="Log level (DEBUG/INFO/WARNING/ERROR)")
def serve(
    host: str,
    port: int,
    name: str,
    explorer: bool,
    metrics: bool,
    cors: Optional[str],
    db: Optional[str],
    cluster: bool,
    log_level: Optional[str],
) -> None:
    """Start A2A HTTP server (internal network service)."""
    from apflow.app import create_app

    app = create_app(connection_string=db, cluster=cluster)

    cors_origins = [s.strip() for s in cors.split(",")] if cors else None

    click.echo(f"Starting A2A server on {host}:{port}")
    click.echo(f"Modules: {len(list(app.registry.list()))}")
    if explorer:
        click.echo(f"Explorer: http://{host}:{port}/explorer")

    from apcore_a2a import serve as a2a_serve

    a2a_serve(
        app.registry,
        host=host,
        port=port,
        name=name,
        description="apflow AI-Perceivable Distributed Orchestration",
        url=f"http://{host}:{port}",
        explorer=explorer,
        metrics=metrics,
        cors_origins=cors_origins,
        log_level=log_level,
    )


@cli.command()
@click.option(
    "--transport",
    default="stdio",
    type=click.Choice(["stdio", "streamable-http", "sse"]),
    help="MCP transport mode",
)
@click.option("--host", default="127.0.0.1", help="Bind host (HTTP modes)")
@click.option("--port", default=8001, type=int, help="Bind port (HTTP modes)")
@click.option("--explorer", is_flag=True, help="Enable MCP Tool Explorer UI")
@click.option("--db", default=None, help="Database connection string")
@click.option("--log-level", default=None, help="Log level")
def mcp(
    transport: str,
    host: str,
    port: int,
    explorer: bool,
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
    else:
        # stdio mode — no console output (would corrupt protocol)
        pass

    from apcore_mcp import serve as mcp_serve

    mcp_serve(
        app.registry,
        transport=transport,
        host=host,
        port=port,
        name="apflow",
        explorer=explorer,
        log_level=log_level,
    )


@cli.command()
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


@cli.command()
@click.option("--db", required=True, help="PostgreSQL connection string (required for cluster)")
@click.option("--node-id", default=None, help="Worker node ID (auto-generated if omitted)")
@click.option("--log-level", default=None, help="Log level")
def worker(db: str, node_id: Optional[str], log_level: Optional[str]) -> None:
    """Start a distributed worker node (requires PostgreSQL)."""
    import asyncio

    click.echo(f"Starting worker node: {node_id or 'auto'}")

    from apflow.app import create_app

    create_app(connection_string=db, cluster=True)

    try:
        from apflow.core.distributed.config import DistributedConfig
        from apflow.core.distributed.worker import WorkerRuntime

        config = DistributedConfig.from_env()
        if node_id:
            config.node_id = node_id

        worker_rt = WorkerRuntime(config)
        click.echo(f"Worker {config.node_id} running (Ctrl+C to stop)")
        asyncio.run(worker_rt.start())
    except ImportError:
        click.echo("Error: distributed module not available", err=True)
    except KeyboardInterrupt:
        click.echo("Worker stopped")


def main() -> None:
    """Entry point for apflow CLI."""
    cli()


if __name__ == "__main__":
    main()
