import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from uuid import uuid4

import httpx
import websockets
from pydantic import TypeAdapter
from pysync_shared.paths import apply_file, fingerprint, normalize_path, paths_collide
from pysync_shared.protocol import Event, FileInfo, Mutation
from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileSystemMovedEvent
from watchdog.observers import Observer

from pysync_client_cli.config import Config
from pysync_client_cli.database import Database
from pysync_client_cli.filesystem import Scanner

log = logging.getLogger("pysync.client")


class Handler(FileSystemEventHandler):
    def __init__(self, engine: "Engine", root: Path):
        self.engine, self.root = engine, root

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write", "closed"):
            return
        if isinstance(event, FileSystemMovedEvent) and not event.is_directory:
            try:
                source = normalize_path(
                    str(Path(os.fsdecode(event.src_path)).relative_to(self.root))
                )
                dest = normalize_path(
                    str(Path(os.fsdecode(event.dest_path)).relative_to(self.root))
                )
                self.engine.loop.call_soon_threadsafe(self.engine.moves.__setitem__, dest, source)
            except ValueError:
                pass
        self.engine.loop.call_soon_threadsafe(self.engine.wake.set)


class Engine:
    """One serialized reconciler; watchers and WebSockets only wake it up."""

    def __init__(self, config: Config, directory: Path):
        config.check_transport()
        self.config, self.directory = config, directory
        self.db = Database(directory)
        self.spool = directory / "spool"
        self.spool.mkdir(mode=0o700, exist_ok=True)
        self.http = httpx.AsyncClient(
            base_url=config.server_url,
            headers={"Authorization": "Bearer " + config.token.get_secret_value()},
            timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=4),
        )
        self.wake = asyncio.Event()
        self.stopping = asyncio.Event()
        self.lock = asyncio.Lock()
        self.connected = False
        self.websocket = None
        self.observer = Observer()
        self.watches: dict[Path, object] = {}
        self.moves: dict[str, str] = {}
        self.manifest_checks: dict[str, tuple[str, ...]] = {}
        self.transfer: dict | None = None
        self.last_error: str | None = None
        self.retry_delay = 0.5
        self.next_retry = 0.0
        self.loop: asyncio.AbstractEventLoop
        referenced = {
            r[0]
            for r in self.db.connection.execute("SELECT blob FROM pending WHERE blob IS NOT NULL")
        }
        for row in self.db.connection.execute("SELECT metadata FROM applications"):
            source = json.loads(row[0]).get("source")
            if source:
                referenced.add(source)
        for item in self.spool.iterdir():
            if str(item) not in referenced:
                item.unlink()
        for blob in referenced:
            if not Path(blob).is_file():
                raise RuntimeError("Queued snapshot is missing; restore client state from backup")

    def scanner(self, project: dict) -> Scanner:
        return Scanner(
            Path(project["root"]),
            self.config.ignore + json.loads(project["ignore_json"]),
            self.config.max_file_size,
        )

    def active_pending(self, project: dict) -> list[dict]:
        scanner = self.scanner(project)
        return [
            row
            for row in self.db.pending(project["id"])
            if not scanner.ignored(row["path"])
            and not any(
                scanner.ignored("/".join(row["path"].split("/")[:i]), True)
                for i in range(1, len(row["path"].split("/")))
            )
        ]

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def watch(self, scanner: Scanner) -> None:
        if not hasattr(self, "loop"):
            return
        for directory in scanner.directories:
            if directory not in self.watches:
                try:
                    self.watches[directory] = self.observer.schedule(
                        Handler(self, scanner.root), str(directory), recursive=False
                    )
                except OSError as error:
                    log.warning(
                        "watch_failed path=%s error=%s; periodic scans remain active",
                        directory,
                        error,
                    )

    async def scan(self, project: dict) -> None:
        if not project["initialized"] and not project["accept_local"]:
            return
        scanner = self.scanner(project)
        files, errors = await asyncio.to_thread(scanner.list_files)
        self.watch(scanner)
        states = self.db.states(project["id"])
        pending = {r["path"] for r in self.db.pending(project["id"])}
        for path, raw in files.items():
            if path in pending:
                continue
            try:
                state = states.get(path)
                expected = (
                    (state["hash"], bool(state["executable"]))
                    if state and not state["deleted"]
                    else None
                )
                info, blob = await asyncio.to_thread(scanner.snapshot, raw, self.spool, expected)
                if blob is None:
                    continue
                mutation = Mutation(
                    operation_id=uuid4(),
                    path=path,
                    base_revision=state["revision"] if state else 0,
                    **info,
                    moved_from=self.moves.pop(path, None),
                )
                with self.db.connection:
                    self.db.connection.execute(
                        "INSERT INTO pending(id,project_id,path,metadata,blob) VALUES(?,?,?,?,?)",
                        (
                            str(mutation.operation_id),
                            project["id"],
                            path,
                            mutation.model_dump_json(),
                            str(blob),
                        ),
                    )
            except (OSError, ValueError) as error:
                errors[path] = str(error)
        # No deletions after incomplete scans, inaccessible directories, or ignore changes.
        if not errors:
            for path, state in states.items():
                if (
                    path not in files
                    and path not in pending
                    and not state["deleted"]
                    and not scanner.ignored(path)
                    and not any(
                        scanner.ignored("/".join(path.split("/")[:i]), True)
                        for i in range(1, len(path.split("/")))
                    )
                ):
                    mutation = Mutation(
                        operation_id=uuid4(),
                        path=path,
                        base_revision=state["revision"],
                        operation="delete",
                    )
                    with self.db.connection:
                        self.db.connection.execute(
                            "INSERT INTO pending(id,project_id,path,metadata) VALUES(?,?,?,?)",
                            (
                                str(mutation.operation_id),
                                project["id"],
                                path,
                                mutation.model_dump_json(),
                            ),
                        )
        with self.db.connection:
            self.db.connection.execute(
                "DELETE FROM scan_errors WHERE project_id=?", (project["id"],)
            )
            self.db.connection.executemany(
                "INSERT INTO scan_errors VALUES(?,?,?)",
                [(project["id"], p, e) for p, e in errors.items()],
            )

    async def push(self, project: dict) -> None:
        rows = self.active_pending(project)
        rows.sort(key=lambda row: json.loads(row["metadata"])["operation"] != "delete")
        for row in rows:
            mutation = Mutation.model_validate_json(row["metadata"])
            self.transfer = {
                "project": project["name"],
                "path": mutation.path,
                "direction": "upload",
                "bytes": 0,
                "total": mutation.size,
            }

            async def chunks(blob_path: str | None = row["blob"]):
                if blob_path:
                    with Path(blob_path).open("rb") as stream:
                        while chunk := await asyncio.to_thread(stream.read, 256 * 1024):
                            self.transfer["bytes"] += len(chunk)
                            yield chunk

            try:
                response = await self.request(
                    "POST",
                    f"/api/v1/projects/{project['id']}/files/upload",
                    headers={
                        "X-PySync-Operation": json.dumps(
                            mutation.model_dump(mode="json"), ensure_ascii=True
                        )
                    },
                    content=chunks(),
                )
                result = response.json()
                with self.db.connection:
                    if result["conflict"]:
                        self.db.record_conflict(project["id"], result["conflict"])
                        # Retain original optimistic base for any further edits before remote apply.
                        self.db.set_state(
                            project["id"],
                            {
                                "path": mutation.path,
                                "hash": mutation.hash,
                                "executable": mutation.executable,
                                "revision": mutation.base_revision,
                                "deleted": mutation.operation == "delete",
                            },
                        )
                    elif result["file"]:
                        self.db.set_state(project["id"], result["file"])
                    self.db.connection.execute("DELETE FROM pending WHERE id=?", (row["id"],))
                if row["blob"]:
                    Path(row["blob"]).unlink(missing_ok=True)
            finally:
                self.transfer = None

    async def download(self, project: dict, info: dict) -> Path:
        temp = self.spool / uuid4().hex
        self.transfer = {
            "project": project["name"],
            "path": info["path"],
            "direction": "download",
            "bytes": 0,
            "total": info["size"],
        }
        digest, size = hashlib.sha256(), 0
        try:
            async with self.http.stream(
                "GET",
                f"/api/v1/projects/{project['id']}/files/download",
                params={"revision": info["revision"]},
            ) as response:
                response.raise_for_status()
                with temp.open("xb") as stream:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > self.config.max_file_size or size > info["size"]:
                            raise ValueError("Download exceeds configured or declared size")
                        digest.update(chunk)
                        await asyncio.to_thread(stream.write, chunk)
                        self.transfer["bytes"] = size
                    stream.flush()
                    await asyncio.to_thread(os.fsync, stream.fileno())
            if size != info["size"] or digest.hexdigest() != info["hash"]:
                raise ValueError("Downloaded content failed SHA-256 verification")
            return temp
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        finally:
            self.transfer = None

    async def apply(self, project: dict, raw_info: dict, initial: bool = False) -> bool:
        info = FileInfo.model_validate(
            {k: v for k, v in raw_info.items() if k in FileInfo.model_fields}
        ).model_dump()
        path, pid = info["path"], project["id"]
        scanner = self.scanner(project)
        if scanner.ignored(path) or any(
            scanner.ignored("/".join(path.split("/")[:i]), True)
            for i in range(1, len(path.split("/")))
        ):
            return True
        row = self.db.connection.execute(
            "SELECT * FROM files WHERE project_id=? AND path=?", (pid, path)
        ).fetchone()
        state = dict(row) if row else None
        if state and state["revision"] >= info["revision"]:
            return True
        if self.db.connection.execute(
            "SELECT 1 FROM pending WHERE project_id=? AND path=?", (pid, path)
        ).fetchone():
            return False
        root = Path(project["root"])
        files, errors = await asyncio.to_thread(scanner.list_files)
        if path in errors or "." in errors:
            raise ValueError("Cannot safely apply change: " + str(errors))
        for local_path in files:
            if paths_collide(local_path, path):
                raise ValueError(f"Unsafe local filename collision: {local_path} and {path}")
        target = files.get(path, path)
        current = await asyncio.to_thread(fingerprint, root, target)
        expected = (
            None if not state or state["deleted"] else (state["hash"], bool(state["executable"]))
        )
        desired = None if info["deleted"] else (info["hash"], info["executable"])
        if current != desired and current != expected and not initial:
            await self.scan(project)
            return False
        # Initial tombstones never remove pre-existing untracked local files.
        if initial and info["deleted"] and current is not None and not state:
            with self.db.connection:
                self.db.set_state(
                    pid,
                    {
                        "path": path,
                        "hash": current[0],
                        "executable": current[1],
                        "revision": info["revision"],
                        "deleted": False,
                    },
                )
            return True
        backup = None
        if current != desired:
            source = await self.download(project, info) if not info["deleted"] else None
            recovery_name = uuid4().hex
            intent = {
                "info": info,
                "target": target,
                "source": str(source) if source else None,
                "expected": list(current) if current else None,
                "recovery_name": recovery_name,
                "initial": initial,
            }
            with self.db.connection:
                self.db.connection.execute(
                    "INSERT OR REPLACE INTO applications VALUES(?,?,?)",
                    (pid, path, json.dumps(intent)),
                )
            backup = await asyncio.to_thread(
                apply_file, root, target, source, info["executable"], recovery_name
            )
            with self.db.connection:
                self.db.set_state(pid, info)
                self.db.connection.execute(
                    "DELETE FROM applications WHERE project_id=? AND path=?", (pid, path)
                )
            if source:
                source.unlink(missing_ok=True)
        with self.db.connection:
            self.db.set_state(pid, info)
            if backup and (initial and current != desired):
                self.db.record_conflict(
                    pid,
                    {
                        "id": uuid4().hex,
                        "path": path,
                        "conflict_path": str(backup),
                        "kind": "initial_local_preserved",
                    },
                )
        if backup:
            # A save racing with remote application is retained as a local conflict, never discarded.
            def recovery_hash() -> str:
                with backup.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest()

            digest = await asyncio.to_thread(recovery_hash)
            if current is None or digest != current[0]:
                with self.db.connection:
                    self.db.record_conflict(
                        pid,
                        {
                            "id": uuid4().hex,
                            "path": path,
                            "conflict_path": str(backup),
                            "kind": "concurrent_local_save_preserved",
                        },
                    )
                log.warning("concurrent_save_preserved path=%s recovery=%s", path, backup)
        return True

    async def recover_applications(self, project: dict) -> None:
        rows = self.db.connection.execute(
            "SELECT path,metadata FROM applications WHERE project_id=?", (project["id"],)
        ).fetchall()
        for row in rows:
            intent = json.loads(row["metadata"])
            info = intent["info"]
            root = Path(project["root"])
            current = await asyncio.to_thread(fingerprint, root, intent["target"])
            desired = None if info["deleted"] else (info["hash"], info["executable"])
            expected = tuple(intent["expected"]) if intent["expected"] else None
            source = Path(intent["source"]) if intent["source"] else None
            backup = root / ".pysync-recovery" / intent["recovery_name"]
            if current != desired and (current == expected or current is None):
                await asyncio.to_thread(
                    apply_file,
                    root,
                    intent["target"],
                    source,
                    info["executable"],
                    intent["recovery_name"],
                )
                current = desired
            with self.db.connection:
                if current == desired:
                    self.db.set_state(project["id"], info)
                if backup.exists():
                    self.db.record_conflict(
                        project["id"],
                        {
                            "id": uuid4().hex,
                            "path": info["path"],
                            "conflict_path": str(backup),
                            "kind": "interrupted_application_preserved",
                        },
                    )
                self.db.connection.execute(
                    "DELETE FROM applications WHERE project_id=? AND path=?",
                    (project["id"], row["path"]),
                )
            if source:
                source.unlink(missing_ok=True)

    async def initialize(self, project: dict) -> bool:
        manifest = (await self.request("GET", f"/api/v1/projects/{project['id']}/manifest")).json()
        if not project["accept_local"]:
            scanner = self.scanner(project)
            files, errors = await asyncio.to_thread(scanner.list_files)
            if errors:
                raise ValueError("Initial scan is incomplete: " + str(errors))
            remote_paths = {f["path"] for f in manifest["files"]}
            for path, raw in files.items():
                if path not in remote_paths:
                    current = await asyncio.to_thread(fingerprint, Path(project["root"]), raw)
                    with self.db.connection:
                        self.db.set_state(
                            project["id"],
                            {"path": path, "hash": current[0], "executable": current[1]},
                        )
        for info in sorted(manifest["files"], key=lambda item: not item["deleted"]):
            if not await self.apply(project, info, initial=True):
                return False
        with self.db.connection:
            self.db.connection.execute(
                "UPDATE projects SET initialized=1,cursor=? WHERE id=?",
                (manifest["revision"], project["id"]),
            )
        return True

    def verify_root(self, project: dict) -> None:
        root = Path(project["root"])
        if root.is_symlink():
            raise ValueError("Project root was replaced with a symlink; synchronization paused")
        info = root.stat()
        if project.get("root_inode") is None:
            with self.db.connection:
                self.db.connection.execute(
                    "UPDATE projects SET root_device=?,root_inode=? WHERE id=?",
                    (info.st_dev, info.st_ino, project["id"]),
                )
            project["root_device"], project["root_inode"] = info.st_dev, info.st_ino
        if (info.st_dev, info.st_ino) != (project["root_device"], project["root_inode"]):
            raise ValueError(
                "Project root identity changed (unmounted or replaced); synchronization paused"
            )

    async def reconcile(self, project: dict) -> None:
        remote = (await self.request("GET", f"/api/v1/projects/{project['id']}")).json()
        highest = self.db.connection.execute(
            "SELECT COALESCE(MAX(revision),0) FROM files WHERE project_id=?", (project["id"],)
        ).fetchone()[0]
        if remote["revision"] < max(project["cursor"], highest):
            raise ValueError(
                "Server revision is behind local state; restore matching backups or reattach safely"
            )
        # Drain successive local versions before applying remote history. Limit passes for fairness.
        for _ in range(4):
            await self.push(project)
            await self.scan(project)
            if not self.active_pending(project):
                break
        if self.active_pending(project):
            self.wake.set()
            return
        if not project["initialized"]:
            if not await self.initialize(project):
                return
            project = self.db.project(project["id"])
        ignore_signature = tuple(self.config.ignore + json.loads(project["ignore_json"]))
        if self.manifest_checks.get(project["id"]) != ignore_signature:
            manifest = (
                await self.request("GET", f"/api/v1/projects/{project['id']}/manifest")
            ).json()
            for info in sorted(manifest["files"], key=lambda item: not item["deleted"]):
                if not await self.apply(project, info):
                    self.wake.set()
                    return
            project["cursor"] = manifest["revision"]
            with self.db.connection:
                self.db.connection.execute(
                    "UPDATE projects SET cursor=? WHERE id=?", (project["cursor"], project["id"])
                )
            self.manifest_checks[project["id"]] = ignore_signature
        while True:
            changes = (
                await self.request(
                    "GET",
                    f"/api/v1/projects/{project['id']}/changes",
                    params={"after": project["cursor"]},
                )
            ).json()
            if not changes:
                break
            for info in changes:
                if not await self.apply(project, info):
                    self.wake.set()
                    return
                project["cursor"] = info["revision"]
                with self.db.connection:
                    self.db.connection.execute(
                        "UPDATE projects SET cursor=? WHERE id=?",
                        (project["cursor"], project["id"]),
                    )
            if len(changes) < 500:
                break
        conflicts = (
            await self.request("GET", f"/api/v1/projects/{project['id']}/conflicts")
        ).json()
        with self.db.connection:
            # Retain local recovery records; refresh unresolved server conflicts.
            self.db.connection.execute(
                "DELETE FROM conflicts WHERE project_id=? AND json_extract(metadata,'$.kind') IS NULL",
                (project["id"],),
            )
            for conflict in conflicts:
                self.db.record_conflict(project["id"], conflict)
            self.db.connection.execute(
                "UPDATE projects SET last_sync=?,error=NULL WHERE id=?",
                (time.time(), project["id"]),
            )
        if self.websocket:
            with contextlib.suppress(Exception):
                await self.websocket.send(
                    json.dumps(
                        {"type": "ack", "project_id": project["id"], "revision": project["cursor"]}
                    )
                )

    async def tick(self, force: bool = False, project_id: str | None = None) -> None:
        async with self.lock:
            projects = self.db.projects()
            if project_id:
                projects = [p for p in projects if p["id"] == project_id]
            for project in projects:
                try:
                    self.verify_root(project)
                    await self.recover_applications(project)
                    await self.scan(project)
                except Exception as error:
                    self.set_error(project["id"], str(error))
                    project["recovery_failed"] = True
            if not force and time.monotonic() < self.next_retry:
                return
            try:
                await self.request("GET", "/api/v1/devices")
                self.connected = True
                self.last_error = None
                self.retry_delay, self.next_retry = 0.5, 0
            except (httpx.HTTPError, OSError) as error:
                self.connected = False
                self.last_error = friendly_error(error)
                self.next_retry = time.monotonic() + random.uniform(
                    self.retry_delay / 2, self.retry_delay
                )
                self.retry_delay = min(self.retry_delay * 2, 60)
                return
            for project in projects:
                if project.get("recovery_failed"):
                    continue
                try:
                    await self.reconcile(project)
                except Exception as error:
                    self.set_error(project["id"], friendly_error(error))
                    log.warning(
                        "sync_failed project=%s error=%s", project["name"], friendly_error(error)
                    )

    def set_error(self, pid: str, error: str) -> None:
        with self.db.connection:
            self.db.connection.execute("UPDATE projects SET error=? WHERE id=?", (error, pid))

    async def websocket_loop(self) -> None:
        delay = 0.5
        url = (
            self.config.server_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        )
        while not self.stopping.is_set():
            try:
                async with websockets.connect(
                    url,
                    additional_headers={
                        "Authorization": "Bearer " + self.config.token.get_secret_value()
                    },
                    max_size=16384,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=10,
                ) as ws:
                    self.websocket = ws
                    self.wake.set()
                    delay = 0.5
                    while not self.stopping.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=25)
                            TypeAdapter(Event).validate_json(raw)
                            self.wake.set()
                        except TimeoutError:
                            await ws.send('{"type":"heartbeat"}')
            except Exception as error:
                log.info("websocket_retry error=%s", friendly_error(error))
            finally:
                self.websocket = None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self.stopping.wait(), timeout=random.uniform(delay / 2, delay)
                )
            delay = min(delay * 2, 60)

    async def run(self, close_on_exit: bool = True) -> None:
        self.loop = asyncio.get_running_loop()
        self.observer.start()
        websocket = asyncio.create_task(self.websocket_loop())
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                await self.tick()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=self.config.poll_seconds)
                # Debounce batches, with a bounded delay even during continuous filesystem activity.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), self.config.debounce_ms / 1000)
        finally:
            websocket.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await websocket
            self.observer.stop()
            await asyncio.to_thread(self.observer.join, 5)
            if close_on_exit:
                await self.close()

    async def close(self) -> None:
        await self.http.aclose()
        self.db.close()

    def status(self) -> dict:
        projects = self.db.projects()
        for project in projects:
            project["pending"] = len(self.db.pending(project["id"]))
            project["paused_pending"] = project["pending"] - len(self.active_pending(project))
            project["scan_errors"] = [
                dict(row)
                for row in self.db.connection.execute(
                    "SELECT path,message FROM scan_errors WHERE project_id=?", (project["id"],)
                )
            ]
        return {
            "connected": self.connected,
            "device": self.config.device_name,
            "device_id": self.config.device_id,
            "projects": projects,
            "error": self.last_error,
            "transfer": self.transfer,
            "conflicts": [
                json.loads(r[0])
                for r in self.db.connection.execute("SELECT metadata FROM conflicts")
            ],
        }


def friendly_error(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        try:
            detail = error.response.json().get("detail", "Request failed")
        except ValueError:
            detail = "Request failed"
        return f"Server returned {error.response.status_code}: {detail}"
    if isinstance(error, httpx.ConnectError):
        return "Server is unreachable; changes remain queued locally"
    return str(error) or error.__class__.__name__
