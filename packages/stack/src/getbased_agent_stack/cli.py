"""getbased-stack — orchestration CLI.

Subcommands:
  init           — interactive one-time setup: token, API key, systemd units
  install        — install/refresh the bundled systemd user units
  uninstall      — stop, disable, and remove the systemd units
  status         — show env file, units, and linger state
  set KEY=VALUE  — upsert a single var in the shared env file
  mcp-config CLIENT — print the MCP client config snippet
  version        — print installed package versions
  info / serve / everything else → delegate to the `lens` CLI

Deliberate zero-dep: argparse + stdlib only. No typer, no click, no
python-dotenv. The env file format is simple enough that we own it.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sys
from pathlib import Path

from . import env_file, mcp_configs, units


# ── helpers ───────────────────────────────────────────────────────────


def _default_api_key_file() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "getbased" / "lens" / "api_key"


def _ensure_api_key(path: Path) -> str:
    """Generate a key if missing; return the key text either way. Mode 0600."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    path.write_text(key + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return key


def _prompt(msg: str, default: str = "", secret: bool = False) -> str:
    """Non-intrusive readline prompt. Treats EOF/Ctrl-D as 'use default'."""
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass

            answer = getpass.getpass(f"{msg}{suffix}: ")
        else:
            answer = input(f"{msg}{suffix}: ")
    except EOFError:
        return default
    return answer.strip() or default


def _yesno(msg: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{msg} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


# ── subcommand implementations ────────────────────────────────────────


def cmd_init(args: argparse.Namespace) -> int:
    print("getbased-stack init — one-time setup")
    print(
        "Writes ~/.config/getbased/env, (optionally) installs + starts systemd\n"
        "user units for rag and dashboard. Idempotent: safe to re-run."
    )
    print()

    # 1. token (optional)
    existing = env_file.read_env_file()
    current_token = existing.get("GETBASED_TOKEN", "")
    masked = "****" + current_token[-4:] if current_token else "(unset)"
    print(f"[1/4] getbased sync token (current: {masked})")
    token = _prompt(
        "Paste GETBASED_TOKEN (press Enter to keep current / skip)",
        default=current_token,
        secret=True,
    )

    # 2. API key
    key_path = Path(existing.get("LENS_API_KEY_FILE", str(_default_api_key_file())))
    print(f"\n[2/4] rag API key file ({key_path})")
    if key_path.exists():
        print("  existing key found — reusing (init is idempotent).")
    else:
        print("  no key found — one will be generated on first service start.")
    key_value = _ensure_api_key(key_path)
    print(f"  key ready (length {len(key_value)} chars, mode 0600)")

    # 3. write env file
    print("\n[3/4] writing ~/.config/getbased/env")
    merged = {**existing}
    merged["GETBASED_STACK_MANAGED"] = "1"
    if token:
        merged["GETBASED_TOKEN"] = token
    merged["LENS_API_KEY_FILE"] = str(key_path)
    merged.setdefault("LENS_URL", "http://127.0.0.1:8322")
    path = env_file.write_env_file(merged)
    print(f"  wrote {path} (mode 0600)")

    # 4. install units
    print("\n[4/4] install systemd user units?")
    if _yesno("install + start getbased-rag + getbased-dashboard?", default=True):
        mgr = units.UnitManager()
        for line in mgr.install(enable=True, start=True):
            print(f"  {line}")
    else:
        print("  skipped — run `getbased-stack install` later to enable.")

    # 5. linger check
    print("\n── Post-install ──")
    _print_linger_hint(strict=False)

    # 6. MCP config pointers
    print("\nConfigure your MCP client(s):")
    for client in mcp_configs.SUPPORTED_CLIENTS:
        print(f"  getbased-stack mcp-config {client}")

    return 0


def _print_linger_hint(strict: bool) -> None:
    user = os.environ.get("USER", "")
    if not user:
        return
    try:
        linger_on = units.has_linger(user)
    except FileNotFoundError:
        # loginctl not on PATH (uncommon but possible in minimal containers)
        print("  loginctl not found — cannot check linger status.")
        return
    gui = units.is_gui_session()
    if linger_on:
        print("  linger: enabled ✓ (services will survive logout + reboot)")
        return
    # Not on.
    if gui:
        # Laptop with GUI login — user will be logged in when they use this,
        # so linger is nice-to-have, not blocking.
        print("  linger: off (fine for laptops; services only run while you're logged in)")
        print(f"         enable with: sudo loginctl enable-linger {user}")
        return
    # Headless + no linger = services die on logout. This is the "silent
    # breakage after reboot" failure mode.
    print("  ⚠ linger: off AND no GUI session detected (headless).")
    print(f"    Without linger, rag + dashboard will stop as soon as this SSH session ends.")
    print(f"    Run this once, then re-enable with `systemctl --user start getbased-rag.service`:")
    print(f"      sudo loginctl enable-linger {user}")


def cmd_install(args: argparse.Namespace) -> int:
    mgr = units.UnitManager()
    for line in mgr.install(enable=not args.no_enable, start=not args.no_start):
        print(line)
    _print_linger_hint(strict=False)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    mgr = units.UnitManager()
    for line in mgr.uninstall():
        print(line)
    if args.delete_env:
        p = env_file.env_file_path()
        if p.exists():
            p.unlink()
            print(f"removed {p}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # env file
    path = env_file.env_file_path()
    if path.exists():
        keys = sorted(env_file.read_env_file(path).keys())
        print(f"env file: {path}  ({len(keys)} keys)")
        for k in keys:
            value = env_file.read_env_file(path)[k]
            # Mask anything that looks sensitive
            if any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
                value = "****" + value[-4:] if len(value) > 4 else "****"
            print(f"  {k}={value}")
    else:
        print(f"env file: {path} (not present — run `getbased-stack init`)")

    # systemd units
    print("\nunits:")
    mgr = units.UnitManager()
    try:
        for svc in mgr.status():
            flags = []
            flags.append("installed" if svc["installed"] else "not installed")
            flags.append("enabled" if svc["enabled"] else "not enabled")
            flags.append("active" if svc["active"] else "inactive")
            print(f"  {svc['name']}: {', '.join(flags)}")
    except FileNotFoundError:
        print("  systemctl not found — cannot check unit status.")

    # linger
    print()
    _print_linger_hint(strict=False)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    if "=" not in args.assignment:
        print("usage: getbased-stack set KEY=VALUE", file=sys.stderr)
        return 2
    key, _, value = args.assignment.partition("=")
    key = key.strip()
    value = value.strip()
    path = env_file.set_env_var(key, value)
    print(f"{key} updated in {path}")
    # If rag/dashboard are running, they'll pick up the change on next
    # restart — remind the user.
    print("restart services to apply: systemctl --user restart getbased-rag getbased-dashboard")
    return 0


def cmd_mcp_config(args: argparse.Namespace) -> int:
    try:
        snippet = mcp_configs.emit(args.client)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(snippet, end="")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    try:
        import importlib.metadata as md

        import getbased_agent_stack

        print(f"getbased-agent-stack {getbased_agent_stack.__version__}")
        for pkg in ("getbased-mcp", "getbased-rag", "getbased-dashboard"):
            try:
                print(f"  {pkg} {md.version(pkg)}")
            except md.PackageNotFoundError:
                print(f"  {pkg} (not installed)")
        return 0
    except ImportError as e:
        print(f"Missing dependency: {e}", file=sys.stderr)
        return 1


def _delegate_to_lens(argv: "list[str]") -> int:
    """Historical behavior: unknown subcommands fall through to `lens`
    so the user can do `getbased-stack serve` / `info` / `ingest` without
    needing to remember a separate binary."""
    try:
        from lens.cli import app as lens_app

        sys.argv = ["lens"] + argv
        try:
            lens_app()
            return 0
        except SystemExit as e:
            return int(e.code or 0)
    except ImportError:
        print(
            "getbased-rag not installed — install with "
            "`pipx install getbased-agent-stack[full]`",
            file=sys.stderr,
        )
        return 1


# ── argparse wiring ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="getbased-stack",
        description=(
            "One-command orchestrator for the getbased agent stack. "
            "Use `init` for first-time setup."
        ),
    )
    sub = p.add_subparsers(dest="command")

    sub.add_parser("init", help="Interactive one-time setup (token, API key, units).")

    pi = sub.add_parser("install", help="Install + start the systemd user units.")
    pi.add_argument("--no-enable", action="store_true", help="Copy files only; don't enable.")
    pi.add_argument("--no-start", action="store_true", help="Enable but don't start now.")

    pu = sub.add_parser("uninstall", help="Stop, disable, and remove the systemd units.")
    pu.add_argument(
        "--delete-env",
        action="store_true",
        help="Also delete ~/.config/getbased/env (keeps API key + data).",
    )

    sub.add_parser("status", help="Show env file, unit state, linger.")

    ps = sub.add_parser("set", help="Upsert a single key in the shared env file.")
    ps.add_argument("assignment", help="KEY=VALUE")

    pm = sub.add_parser("mcp-config", help="Print an MCP client config snippet.")
    pm.add_argument(
        "client",
        choices=mcp_configs.SUPPORTED_CLIENTS,
        help="Which client to emit for.",
    )

    sub.add_parser("version", help="Print installed package versions.")

    return p


COMMANDS = {
    "init": cmd_init,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "status": cmd_status,
    "set": cmd_set,
    "mcp-config": cmd_mcp_config,
    "version": cmd_version,
}


def main(argv: "list[str] | None" = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    # Fast path: no args → show our help, not lens's.
    if not argv or argv[0] in ("-h", "--help", "help"):
        build_parser().print_help()
        return 0

    # New commands take priority; everything else falls through to lens
    # (preserves the old thin-wrapper behavior for `serve`, `info`, etc).
    if argv[0] in COMMANDS:
        args = build_parser().parse_args(argv)
        return COMMANDS[args.command](args)

    return _delegate_to_lens(argv)


if __name__ == "__main__":
    sys.exit(main())
