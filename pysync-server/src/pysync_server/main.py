import asyncio
import contextlib
import fcntl
import hashlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import TypeAdapter, ValidationError
from pysync_shared.logging import configure_logging
from pysync_shared.protocol import (
    Acknowledge,
    DeviceRegistration,
    Event,
    Heartbeat,
    Mutation,
    ProjectCreate,
    SyncRequest,
)
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pysync_server.config import Settings
from pysync_server.database.migrations import migrate
from pysync_server.database.models import Conflict, Device, File, Membership, Project, Revision
from pysync_server.security import BodyLimit
from pysync_server.services.store import Store

log = logging.getLogger("pysync.server")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Hub:
    def __init__(self) -> None:
        self.clients: dict[WebSocket, tuple[str, asyncio.Queue]] = {}

    async def broadcast(self, app: FastAPI, messages: list[dict]) -> None:
        async with app.state.sessions() as session:
            for message in messages:
                project = message.get("project_id")
                allowed = None
                if project:
                    allowed = set(
                        await session.scalars(
                            select(Membership.device_id).where(Membership.project_id == project)
                        )
                    )
                for ws, (device_id, queue) in list(self.clients.items()):
                    if allowed is not None and device_id not in allowed:
                        continue
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull:
                        # Bounded memory; reconnecting clients recover from the journal.
                        self.clients.pop(ws, None)
                        with contextlib.suppress(Exception):
                            await ws.close(code=1013)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if len(settings.owner_token.get_secret_value()) < 24:
            raise RuntimeError(
                "Set PYSYNC_SERVER_OWNER_TOKEN to a random token of at least 24 characters"
            )
        settings.data_dir = settings.data_dir.absolute()
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if settings.data_dir.is_symlink():
            raise RuntimeError("Server data directory must not be a symlink")
        settings.data_dir.chmod(0o700)
        lock_file = (settings.data_dir / "server.lock").open("a+")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError("Another server already owns this data directory") from None
        engine = create_async_engine(f"sqlite+aiosqlite:///{settings.data_dir / 'metadata.sqlite'}")

        @event.listens_for(engine.sync_engine, "connect")
        def pragmas(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        try:
            await migrate(engine)
            app.state.sessions = async_sessionmaker(engine, expire_on_commit=False)
            app.state.store = Store(settings, app.state.sessions)
            app.state.hub = Hub()
            for directory in (app.state.store.blobs, app.state.store.temp):
                directory.mkdir(mode=0o700, exist_ok=True)
                if directory.is_symlink():
                    raise RuntimeError("Storage directory must not be a symlink")
                directory.chmod(0o700)
            for temp in app.state.store.temp.iterdir():
                temp.unlink()  # Incomplete streams never have committed references.
            async with app.state.sessions() as session:
                for revision in await session.scalars(select(Revision)):
                    info = revision.info
                    if not info["deleted"] and not (app.state.store.blobs / info["hash"]).is_file():
                        raise RuntimeError(
                            "Committed content is missing; restore server from backup"
                        )
            log.info("server_ready data_dir=%s", settings.data_dir)
            yield
        finally:
            await engine.dispose()
            lock_file.close()

    app = FastAPI(title="PySync", version="1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.settings = settings

    async def authenticate(authorization: str | None = Header(default=None)) -> Device:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Device authentication required")
        async with app.state.sessions() as session:
            device = await session.scalar(
                select(Device).where(Device.token_hash == token_hash(authorization[7:]))
            )
        if device is None:
            raise HTTPException(401, "Invalid or revoked device token")
        return device

    def owner(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not secrets.compare_digest(
            authorization, "Bearer " + settings.owner_token.get_secret_value()
        ):
            raise HTTPException(401, "Owner authentication required")

    async def authorize(project_id: UUID, device: Device = Depends(authenticate)) -> str:
        pid = str(project_id)
        async with app.state.sessions() as session:
            project = await session.get(Project, pid)
            if not project or project.deleted:
                raise HTTPException(404, "Project not found")
            if not await session.get(Membership, (pid, device.id)):
                raise HTTPException(403, "Join the project before accessing files")
        return pid

    @app.exception_handler(OSError)
    async def filesystem_error(_request: Request, error: OSError):
        log.exception("storage_error", exc_info=error)
        return JSONResponse(
            status_code=507, content={"detail": "Storage unavailable; check server logs"}
        )

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "protocol_version": 1}

    @app.post("/api/v1/devices/register", dependencies=[Depends(owner)], status_code=201)
    async def register(body: DeviceRegistration):
        token = secrets.token_urlsafe(32)
        device = Device(
            id=str(uuid4()), name=body.name, token_hash=token_hash(token), created_at=time.time()
        )
        async with app.state.sessions.begin() as session:
            session.add(device)
        return {"id": device.id, "name": device.name, "token": token}

    @app.get("/api/v1/devices")
    async def devices(_device: Device = Depends(authenticate)):
        async with app.state.sessions() as session:
            rows = (await session.scalars(select(Device))).all()
        return [
            {
                "id": d.id,
                "name": d.name,
                "online": any(entry[0] == d.id for entry in app.state.hub.clients.values()),
            }
            for d in rows
        ]

    @app.delete("/api/v1/devices/{device_id}", dependencies=[Depends(owner)])
    async def revoke(device_id: UUID):
        async with app.state.sessions.begin() as session:
            device = await session.get(Device, str(device_id))
            if not device:
                raise HTTPException(404, "Device not found")
            device.token_hash = token_hash(secrets.token_urlsafe(32))
        for ws, (did, _queue) in list(app.state.hub.clients.items()):
            if did == str(device_id):
                await ws.close(code=1008)
        return {"revoked": True}

    @app.post("/api/v1/projects", status_code=201)
    async def create_project(body: ProjectCreate, device: Device = Depends(authenticate)):
        async with app.state.store.lock, app.state.sessions.begin() as session:
            if await session.scalar(select(Project).where(Project.name == body.name)):
                raise HTTPException(409, "Project name already exists")
            project = Project(id=str(uuid4()), name=body.name, revision=0, created_at=time.time())
            session.add(project)
            await session.flush()
            session.add(Membership(project_id=project.id, device_id=device.id))
        return {"id": project.id, "name": project.name, "revision": 0}

    @app.get("/api/v1/projects")
    async def projects(_device: Device = Depends(authenticate)):
        # Owner-issued devices are trusted to discover and explicitly join any project.
        async with app.state.sessions() as session:
            rows = await session.scalars(select(Project).where(Project.deleted.is_(False)))
            return [{"id": p.id, "name": p.name, "revision": p.revision} for p in rows]

    @app.post("/api/v1/projects/{project_id}/join")
    async def join(project_id: UUID, device: Device = Depends(authenticate)):
        pid = str(project_id)
        async with app.state.store.lock, app.state.sessions.begin() as session:
            project = await session.get(Project, pid)
            if not project or project.deleted:
                raise HTTPException(404, "Project not found")
            if not await session.get(Membership, (pid, device.id)):
                session.add(Membership(project_id=pid, device_id=device.id))
        return {"joined": True}

    @app.get("/api/v1/projects/{project_id}")
    async def get_project(pid: str = Depends(authorize)):
        async with app.state.sessions() as session:
            p = await session.get(Project, pid)
            return {"id": p.id, "name": p.name, "revision": p.revision}

    @app.delete("/api/v1/projects/{project_id}", dependencies=[Depends(owner)])
    async def delete_project(project_id: UUID):
        async with app.state.store.lock, app.state.sessions.begin() as session:
            project = await session.get(Project, str(project_id))
            if not project:
                raise HTTPException(404, "Project not found")
            project.deleted = (
                True  # Retain history and blobs; clients stop, never delete local trees.
            )
        return {"archived": True}

    @app.get("/api/v1/projects/{project_id}/manifest")
    async def manifest(pid: str = Depends(authorize)):
        async with app.state.store.lock, app.state.sessions() as session:
            p = await session.get(Project, pid)
            rows = await session.scalars(
                select(File).where(File.project_id == pid).order_by(File.path)
            )
            return {"project_id": pid, "revision": p.revision, "files": [f.info for f in rows]}

    @app.get("/api/v1/projects/{project_id}/changes")
    async def changes(
        pid: str = Depends(authorize),
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=1000),
    ):
        async with app.state.sessions() as session:
            rows = await session.scalars(
                select(Revision)
                .where(Revision.project_id == pid, Revision.revision > after)
                .order_by(Revision.revision)
                .limit(limit)
            )
            return [{**r.info, "operation": r.operation, "moved_from": r.moved_from} for r in rows]

    @app.post("/api/v1/projects/{project_id}/files/upload")
    async def upload(
        request: Request,
        pid: str = Depends(authorize),
        device: Device = Depends(authenticate),
        x_pysync_operation: str = Header(max_length=8192),
    ):
        try:
            mutation = Mutation.model_validate_json(x_pysync_operation)
        except (ValueError, ValidationError) as error:
            raise HTTPException(422, "Invalid operation metadata") from error
        temp = None
        async with app.state.store.uploads:
            try:
                if mutation.operation == "put":
                    try:
                        temp = await asyncio.wait_for(
                            app.state.store.receive(request, mutation),
                            timeout=settings.upload_timeout,
                        )
                    except TimeoutError as error:
                        raise HTTPException(
                            408, "Upload timed out; retry the same operation"
                        ) from error
                response, events = await app.state.store.commit(pid, device, mutation, temp)
            finally:
                if temp:
                    temp.unlink(missing_ok=True)
        await app.state.hub.broadcast(app, events)
        return response

    @app.get("/api/v1/projects/{project_id}/files/download")
    async def download(pid: str = Depends(authorize), revision: int = Query(ge=1)):
        async with app.state.sessions() as session:
            row = await session.get(Revision, (pid, revision))
            if not row or row.info["deleted"]:
                raise HTTPException(404, "File revision not found")
        return FileResponse(
            app.state.store.blobs / row.info["hash"],
            media_type="application/octet-stream",
            filename="pysync-download",
            headers={"X-Content-SHA256": row.info["hash"], "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/projects/{project_id}/conflicts")
    async def conflicts(pid: str = Depends(authorize)):
        async with app.state.sessions() as session:
            rows = await session.scalars(
                select(Conflict).where(Conflict.project_id == pid, Conflict.resolved.is_(False))
            )
            return [
                {
                    "id": c.id,
                    "project_id": c.project_id,
                    "path": c.path,
                    "conflict_path": c.conflict_path,
                    "device_id": c.device_id,
                    "accepted_revision": c.accepted_revision,
                }
                for c in rows
            ]

    @app.post("/api/v1/projects/{project_id}/conflicts/{conflict_id}/resolve")
    async def resolve(conflict_id: UUID, pid: str = Depends(authorize)):
        async with app.state.sessions.begin() as session:
            row = await session.get(Conflict, str(conflict_id))
            if not row or row.project_id != pid:
                raise HTTPException(404, "Conflict not found")
            row.resolved = True
        return {"resolved": True}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        try:
            device = await authenticate(ws.headers.get("authorization"))
        except HTTPException:
            await ws.close(code=1008)
            return
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        app.state.hub.clients[ws] = (device.id, queue)
        await queue.put({"type": "sync_status", "device_id": device.id, "online": True})

        async def sender():
            while True:
                message = await queue.get()
                await asyncio.wait_for(ws.send_json(message), timeout=15)

        sender_task = asyncio.create_task(sender())
        await app.state.hub.broadcast(
            app, [{"type": "presence", "device_id": device.id, "online": True}]
        )
        try:
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=90)
                if len(raw) > 8192:
                    await ws.close(code=1009)
                    break
                try:
                    message = TypeAdapter(Event).validate_json(raw)
                except ValidationError:
                    await queue.put({"type": "error", "message": "Invalid control message"})
                    continue
                if isinstance(message, Heartbeat):
                    await queue.put({"type": "heartbeat"})
                elif isinstance(message, SyncRequest):
                    await queue.put({"type": "sync_status", "device_id": device.id, "online": True})
                elif isinstance(message, Acknowledge):
                    async with app.state.sessions.begin() as session:
                        member = await session.get(Membership, (str(message.project_id), device.id))
                        project = await session.get(Project, str(message.project_id))
                        if member and project and message.revision <= project.revision:
                            member.acknowledged_revision = max(
                                member.acknowledged_revision, message.revision
                            )
                        else:
                            await queue.put({"type": "error", "message": "Invalid acknowledgement"})
        except Exception:
            log.info("websocket_disconnected device_id=%s", device.id)
        finally:
            app.state.hub.clients.pop(ws, None)
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender_task
            await app.state.hub.broadcast(
                app, [{"type": "presence", "device_id": device.id, "online": False}]
            )

    return app


app = create_app()


def main() -> None:
    configure_logging()
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ssl_certfile=settings.tls_cert,
        ssl_keyfile=settings.tls_key,
        ws_max_size=8192,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        log_config=None,
    )


if __name__ == "__main__":
    main()
