import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pysync_shared.paths import fingerprint, paths_collide

from pysync_client_cli.config import load_config
from pysync_client_cli.engine import Engine, friendly_error
from pysync_client_cli.filesystem import Scanner

log = logging.getLogger("pysync.daemon")


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["status", "sync", "stop", "add", "remove", "resolve", "list_projects"]
    name: str | None = Field(default=None, max_length=100)
    root: str | None = Field(default=None, max_length=4096)
    server_project: str | None = Field(default=None, max_length=100)
    ignore: list[str] = Field(default_factory=list, max_length=200)
    accept_local: bool = False
    preview: bool = False
    discard_pending: bool = False
    conflict_id: str | None = None


class DaemonUnavailable(ValueError):
    """The configured daemon socket is absent or has no listener."""


async def rpc(directory: Path, command: str, **kwargs) -> dict:
    socket_path = directory / "daemon.sock"
    if not socket_path.exists():
        raise DaemonUnavailable("Daemon is not running. Start it with 'daemon start'.")
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=4 * 1024 * 1024)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise DaemonUnavailable("Daemon is not running. Start it with 'daemon start'.") from error
    try:
        writer.write((json.dumps({"command": command, **kwargs}) + "\n").encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=120)
        if not raw:
            raise ValueError("Daemon closed the connection; check 'logs'")
        response = json.loads(raw)
        if not response["ok"]:
            raise ValueError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


async def dispatch(engine: Engine, command: Command) -> dict:
    if command.command == "status":
        status = engine.status()
        if command.name:
            selected = engine.db.project(command.name)["id"]
            status["projects"] = [p for p in status["projects"] if p["id"] == selected]
        return status
    if command.command == "stop":
        engine.stopping.set()
        engine.wake.set()
        return {"stopping": True}
    if command.command == "list_projects":
        return {"projects": (await engine.request("GET", "/api/v1/projects")).json()}
    if command.command == "sync":
        selected = engine.db.project(command.name)["id"] if command.name else None
        await engine.tick(force=True, project_id=selected)
        return engine.status()
    async with engine.lock:
        if command.command == "remove":
            project = engine.db.project(command.name or "")
            if engine.db.pending(project["id"]) and not command.discard_pending:
                raise ValueError("Project has pending changes; sync first or use --discard-pending")
            with engine.db.connection:
                engine.db.connection.execute("DELETE FROM projects WHERE id=?", (project["id"],))
            root = Path(project["root"])
            for directory in list(engine.watches):
                if directory == root or root in directory.parents:
                    engine.observer.unschedule(engine.watches.pop(directory))
            return {"removed": project["name"], "local_files_retained": True}
        if command.command == "resolve":
            cid = command.conflict_id or ""
            UUID(cid)
            rows = engine.db.connection.execute(
                "SELECT * FROM conflicts WHERE id=?", (cid,)
            ).fetchone()
            if not rows:
                raise ValueError("Unknown conflict")
            meta = json.loads(rows["metadata"])
            if not meta.get("kind"):
                await engine.request(
                    "POST", f"/api/v1/projects/{rows['project_id']}/conflicts/{cid}/resolve"
                )
            with engine.db.connection:
                engine.db.connection.execute("DELETE FROM conflicts WHERE id=?", (cid,))
            return {"resolved": cid, "files_retained": True}
        if command.command == "add":
            if not command.root:
                raise ValueError("Project directory is required")
            supplied = Path(command.root).absolute()
            if supplied.is_symlink() or not supplied.is_dir():
                raise ValueError("Project directory must exist and must not be a symlink")
            root = supplied.resolve()
            if (
                root == engine.directory
                or root in engine.directory.parents
                or engine.directory in root.parents
            ):
                raise ValueError("Project must not overlap the daemon state directory")
            for existing in engine.db.projects():
                other = Path(existing["root"])
                if other == root or other in root.parents or root in other.parents:
                    raise ValueError("Project directories cannot overlap")
            name = command.name or root.name
            if any(p["name"] == name for p in engine.db.projects()):
                raise ValueError("Local project name already exists")
            server_projects = (await engine.request("GET", "/api/v1/projects")).json()
            selected = command.server_project or name
            project = next(
                (p for p in server_projects if p["id"] == selected or p["name"] == selected), None
            )
            if project is None:
                if command.server_project:
                    raise ValueError("Server project not found")
                project = (
                    await engine.request("POST", "/api/v1/projects", json={"name": name})
                ).json()
            await engine.request("POST", f"/api/v1/projects/{project['id']}/join")
            manifest = (
                await engine.request("GET", f"/api/v1/projects/{project['id']}/manifest")
            ).json()
            scanner = Scanner(
                root, engine.config.ignore + command.ignore, engine.config.max_file_size
            )
            files, errors = await asyncio.to_thread(scanner.list_files)
            if errors:
                raise ValueError(
                    "Fix unsafe or inaccessible files before adding project: " + str(errors)
                )
            remote = {i["path"]: i for i in manifest["files"] if not i["deleted"]}
            collisions = []
            for local_path in files:
                for remote_path in remote:
                    if paths_collide(local_path, remote_path):
                        raise ValueError(
                            f"Unsafe filename collision: {local_path} and {remote_path}"
                        )
            for path, raw in files.items():
                if path in remote:
                    local = await asyncio.to_thread(fingerprint, root, raw)
                    if local != (remote[path]["hash"], remote[path]["executable"]):
                        collisions.append(path)
            plan = {
                "project_id": project["id"],
                "name": name,
                "local_files": len(files),
                "downloads": len(set(remote) - set(files)),
                "collisions": collisions,
                "local_only": len(set(files) - set(remote)),
                "accept_local": command.accept_local,
            }
            if command.preview:
                return plan
            with engine.db.connection:
                engine.db.connection.execute(
                    "INSERT INTO projects(id,name,root,accept_local,ignore_json,root_device,root_inode) VALUES(?,?,?,?,?,?,?)",
                    (
                        project["id"],
                        name,
                        str(root),
                        command.accept_local,
                        json.dumps(command.ignore),
                        root.stat().st_dev,
                        root.stat().st_ino,
                    ),
                )
            engine.wake.set()
            return plan
    raise ValueError("Unsupported command")


async def serve(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError("Configuration directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    lock = (directory / "daemon.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("A daemon already manages this configuration") from None
    engine = None
    server = None
    clients: set[asyncio.Task] = set()
    socket = directory / "daemon.sock"
    try:
        config = load_config(directory)
        if not config.device_id or not config.token.get_secret_value():
            raise ValueError("Register a device using 'login' before starting the daemon")
        engine = Engine(config, directory)
        engine.loop = asyncio.get_running_loop()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            task = asyncio.current_task()
            clients.add(task)
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=10)
                command = Command.model_validate_json(raw)
                result = await dispatch(engine, command)
                response = {"ok": True, "result": result}
            except Exception as error:
                response = {"ok": False, "error": friendly_error(error)}
            writer.write((json.dumps(response) + "\n").encode())
            with contextlib.suppress(Exception):
                await writer.drain()
            writer.close()
            await writer.wait_closed()
            clients.discard(task)

        socket.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(handle, path=str(socket), limit=1024 * 1024)
        os.chmod(socket, 0o600)
        (directory / "daemon.pid").write_text(str(os.getpid()))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, engine.stopping.set)
        log.info("daemon_ready device=%s", config.device_name)
        async with server:
            await engine.run(close_on_exit=False)
    finally:
        if server:
            server.close()
            await server.wait_closed()
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if engine:
            await engine.close()
        socket.unlink(missing_ok=True)
        (directory / "daemon.pid").unlink(missing_ok=True)
        lock.close()
