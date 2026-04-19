"""Dashboard CLI.

  getbased-dashboard serve            Run the web UI
  getbased-dashboard info             Show resolved config + key status
"""

from __future__ import annotations

import typer
import uvicorn

from .config import DashboardConfig
from .server import create_app

app = typer.Typer(
    name="getbased-dashboard",
    help="Web dashboard for getbased-agents.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def serve(
    host: str | None = typer.Option(None, help="Bind host (default 127.0.0.1)"),
    port: int | None = typer.Option(None, help="Bind port (default 8323)"),
    lens_url: str | None = typer.Option(None, help="URL of the rag server"),
    reload: bool = typer.Option(False, help="Hot-reload on code changes (dev only)"),
) -> None:
    """Start the dashboard web server."""
    cfg = DashboardConfig.from_env()
    if host:
        cfg.host = host
    if port:
        cfg.port = port
    if lens_url:
        cfg.lens_url = lens_url

    typer.echo(f"getbased-dashboard → http://{cfg.host}:{cfg.port}")
    typer.echo(f"  rag:         {cfg.lens_url}")
    typer.echo(f"  api key:     {cfg.api_key_file}")
    if not cfg.read_api_key():
        typer.echo("  ⚠ no key found — start getbased-rag to generate one")

    if reload:
        uvicorn.run(
            "getbased_dashboard.server:create_app",
            host=cfg.host,
            port=cfg.port,
            reload=True,
            factory=True,
        )
    else:
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)


@app.command()
def info() -> None:
    """Show resolved configuration and whether we can see a rag API key."""
    cfg = DashboardConfig.from_env()
    typer.echo(f"host:            {cfg.host}")
    typer.echo(f"port:            {cfg.port}")
    typer.echo(f"lens_url:        {cfg.lens_url}")
    typer.echo(f"api_key_file:    {cfg.api_key_file}")
    typer.echo(f"api_key_present: {bool(cfg.read_api_key())}")
    typer.echo(f"activity_log:    {cfg.activity_log}")
