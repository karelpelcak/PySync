import asyncio
import getpass
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from pydantic import SecretStr
from pysync_shared.logging import configure_logging
from rich.console import Console
from rich.table import Table

from pysync_client_cli.config import Config, config_dir, load_config, save_config
from pysync_client_cli.daemon import rpc, serve
from pysync_client_cli.engine import friendly_error
from pysync_client_cli.lifecycle import start_daemon

app = typer.Typer(
    no_args_is_help=True, help="PySync: manage your central file synchronization daemon."
)
projects = typer.Typer(no_args_is_help=True)
daemon = typer.Typer(no_args_is_help=True)
devices = typer.Typer(no_args_is_help=True)
app.add_typer(projects, name="project")
app.add_typer(daemon, name="daemon")
app.add_typer(devices, name="device")
console = Console()
state_directory: Path | None = None


def directory() -> Path:
    return state_directory or config_dir()


@app.callback()
def options(
    config_dir_option: Annotated[
        Path | None, typer.Option("--config-dir", help="Independent client state directory")
    ] = None,
):
    global state_directory
    state_directory = config_dir_option.absolute() if config_dir_option else config_dir()


def call(command: str, **kwargs) -> dict:
    return asyncio.run(rpc(directory(), command, **kwargs))


def ensure_stopped() -> None:
    try:
        call("status")
    except ValueError:
        return
    raise ValueError("Stop the daemon before changing device credentials")


@app.command()
def init(server_url: str = "http://127.0.0.1:8000", allow_insecure: bool = False):
    """Create a private configuration. HTTPS is required outside loopback by default."""
    ensure_stopped()
    if (directory() / "config.json").exists():
        raise ValueError("Configuration exists; edit config.json or use login")
    config = Config(server_url=server_url, allow_insecure=allow_insecure)
    config.check_transport()
    save_config(directory(), config)
    console.print(f"Configuration saved to {directory() / 'config.json'}")


@app.command()
def login(
    server_url: str | None = None, device_name: str | None = None, allow_insecure: bool = False
):
    """Register this device; owner token is prompted securely or read from PYSYNC_OWNER_TOKEN."""
    ensure_stopped()
    config = load_config(directory())
    if config.device_id:
        raise ValueError(
            "Device is already registered; use a new --config-dir for another identity"
        )
    if server_url:
        config.server_url = Config(server_url=server_url).server_url
    if allow_insecure:
        config.allow_insecure = True
    config.check_transport()
    owner = os.environ.get("PYSYNC_OWNER_TOKEN") or getpass.getpass("Server owner token: ")
    name = device_name or socket.gethostname().split(".")[0]
    response = httpx.post(
        config.server_url + "/api/v1/devices/register",
        headers={"Authorization": "Bearer " + owner},
        json={"name": name},
        timeout=15,
    )
    response.raise_for_status()
    device = response.json()
    config.device_id, config.device_name, config.token = (
        device["id"],
        device["name"],
        SecretStr(device["token"]),
    )
    save_config(directory(), config)
    console.print(f"Registered {device['name']} ({device['id']}). Start the daemon next.")


@devices.command("register")
def register(
    server_url: str | None = None, device_name: str | None = None, allow_insecure: bool = False
):
    login(server_url, device_name, allow_insecure)


@projects.command("add")
def add(
    path: Path,
    name: str | None = None,
    server_project: str | None = None,
    accept_local: bool = False,
    ignore: Annotated[list[str] | None, typer.Option()] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
):
    """Preview and attach a directory. --accept-local explicitly permits initial local uploads."""
    kwargs = {
        "root": str(path.absolute()),
        "name": name,
        "server_project": server_project,
        "accept_local": accept_local,
        "ignore": ignore or [],
    }
    plan = call("add", preview=True, **kwargs)
    console.print(
        f"Project {plan['name']}: {plan['local_files']} local files, "
        f"{plan['downloads']} missing remote files, {len(plan['collisions'])} differing collisions."
    )
    if accept_local:
        console.print(
            "Initial local files will upload. Differing collisions become server conflict copies."
        )
    else:
        console.print(
            "Local-only files stay local. Differing local contents are retained in .pysync-recovery."
        )
    for collision in plan["collisions"][:20]:
        console.print(f"  Collision: {collision}")
    if not yes and not typer.confirm("Apply this synchronization plan?"):
        raise typer.Abort()
    result = call("add", **kwargs)
    console.print(f"Attached {result['name']} ({result['project_id']}).")


def show_status(status: dict) -> None:
    console.print(
        f"Device: {status['device']} ({status['device_id']}) | "
        f"Server: {'connected' if status['connected'] else 'offline'}"
    )
    table = Table("Project", "Pending", "Applied revision", "Last sync", "Error")
    for project in status["projects"]:
        last = (
            datetime.fromtimestamp(project["last_sync"]).isoformat(timespec="seconds")
            if project.get("last_sync")
            else "never"
        )
        errors = project.get("error") or "; ".join(
            e["message"] for e in project.get("scan_errors", [])
        )
        table.add_row(
            project["name"], str(project["pending"]), str(project["cursor"]), last, errors
        )
    console.print(table)
    if status.get("transfer"):
        transfer = status["transfer"]
        console.print(
            f"{transfer['direction']}: {transfer['path']} {transfer['bytes']}/{transfer['total']} bytes"
        )
    if status.get("error"):
        console.print(status["error"], style="red")
    console.print(f"Unresolved conflicts: {len(status.get('conflicts', []))}")


@projects.command("list")
def project_list():
    show_status(call("status"))


@projects.command("status")
def project_status(name: str):
    show_status(call("status", name=name))


@projects.command("remove")
def remove(name: str, discard_pending: bool = False):
    """Detach locally; local files and server history remain available."""
    result = call("remove", name=name, discard_pending=discard_pending)
    console.print(f"Detached {result['removed']}; local files retained.")


@app.command()
def status():
    show_status(call("status"))


@app.command()
def sync(name: Annotated[str | None, typer.Argument()] = None):
    """Request reconciliation by the existing daemon."""
    show_status(call("sync", name=name))


@app.command()
def conflicts(resolve: str | None = None):
    """List unresolved conflicts; --resolve ID marks a manually reconciled conflict resolved."""
    if resolve:
        console.print(call("resolve", conflict_id=resolve))
        return
    table = Table("ID", "Path", "Preserved copy")
    for conflict in call("status")["conflicts"]:
        table.add_row(
            conflict["id"],
            conflict["path"],
            conflict.get("conflict_path") or "Stale deletion rejected",
        )
    console.print(table)


@daemon.command("run")
def daemon_run():
    """Run in the foreground (also the service-manager entry point)."""
    directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    configure_logging(str(directory() / "daemon.log"))
    asyncio.run(serve(directory()))


@daemon.command("start")
def daemon_start():
    """Start one detached daemon; install a service to start automatically at login."""
    asyncio.run(start_daemon(directory()))
    console.print("Daemon is running.")


@daemon.command("stop")
def daemon_stop():
    call("stop")
    console.print("Daemon shutdown requested; pending changes are retained.")


@daemon.command("status")
def daemon_status():
    try:
        show_status(call("status"))
    except ValueError:
        console.print("Daemon is stopped.")


@app.command()
def logs(lines: int = typer.Option(50, min=1, max=5000)):
    """Display recent structured daemon logs."""
    path = directory() / "daemon.log"
    if not path.exists():
        console.print("No daemon logs yet.")
        return
    from collections import deque

    with path.open() as stream:
        console.print("".join(deque(stream, maxlen=lines)), markup=False)


def main() -> None:
    try:
        app()
    except (ValueError, OSError, httpx.HTTPError) as error:
        console.print(friendly_error(error), style="red", markup=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
