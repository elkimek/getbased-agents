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

    key = cfg.read_api_key()
    base_url = f"http://{cfg.host}:{cfg.port}"
    typer.echo(f"getbased-dashboard → {base_url}")
    typer.echo(f"  rag:         {cfg.lens_url}")
    typer.echo(f"  api key:     {cfg.api_key_file}")
    if key:
        # Magic login URL — frontend auto-captures ?key=... on first
        # load and stores it in localStorage, so users don't need to
        # grab the key from the terminal and paste it. Matches the
        # Jupyter Lab / Open WebUI / code-server convention. The key
        # is the same bearer the user would paste anyway; having it
        # in the URL once, on the loopback interface, is a net UX win
        # over requiring a terminal hop. Tagged with [LOGIN-URL] so
        # users grepping `journalctl` on a systemd deploy can find it.
        typer.echo("")
        typer.echo("  Open the dashboard with one click:")
        typer.echo(f"  [LOGIN-URL] {base_url}/?key={key}")
        typer.echo(
            "  (lost this URL? run `getbased-dashboard login-url` to re-print it)"
        )
    else:
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
    # Never echoes the key itself — only whether it was found. That's
    # deliberate: `info` is the command you paste into a support thread.
    # If the user wants the login URL for a second browser, they call
    # `login-url` which makes the secret-exposure intent explicit.


@app.command("login-url")
def login_url() -> None:
    """Print the one-click dashboard login URL.

    The URL embeds the bearer key as a query parameter — same pattern
    as Jupyter Lab / Open WebUI / code-server. Intended uses:

      * Reconnect after closing the original `serve` terminal
      * Open the dashboard from a second browser or device
      * Fetch the URL when the dashboard runs as a systemd / launchd
        service and nothing was printed to a terminal

    Exits non-zero if the key file is missing — there's nothing useful
    to print and scripts can branch on the status code."""
    cfg = DashboardConfig.from_env()
    key = cfg.read_api_key()
    if not key:
        typer.echo(
            "No API key on disk — start getbased-rag first so it can "
            f"generate one. Expected at: {cfg.api_key_file}",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"http://{cfg.host}:{cfg.port}/?key={key}")
