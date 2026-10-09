import asyncio
import hashlib
import os
import time
from pathlib import Path
from uuid import uuid4

import aiofiles
from fastapi import HTTPException, Request
from pysync_shared.paths import fsync_directory, path_key, paths_collide
from pysync_shared.protocol import FileInfo, Mutation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pysync_server.config import Settings
from pysync_server.database.models import Conflict, Device, File, Operation, Project, Revision


class Store:
    def __init__(self, settings: Settings, sessions: async_sessionmaker):
        self.settings = settings
        self.sessions = sessions
        self.blobs = settings.data_dir / "blobs"
        self.temp = settings.data_dir / "transfers"
        self.lock = asyncio.Lock()
        self.uploads = asyncio.Semaphore(settings.max_uploads)

    async def receive(self, request: Request, mutation: Mutation) -> Path:
        if mutation.hash is None or mutation.size > self.settings.max_file_size:
            raise HTTPException(413, "Missing hash or file exceeds configured size limit")
        temp = self.temp / uuid4().hex
        size, digest = 0, hashlib.sha256()
        try:
            async with aiofiles.open(temp, "xb") as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > self.settings.max_file_size or size > mutation.size:
                        raise HTTPException(413, "Upload exceeds declared size")
                    digest.update(chunk)
                    await stream.write(chunk)
                await stream.flush()
                await asyncio.to_thread(os.fsync, stream.fileno())
            if size != mutation.size or digest.hexdigest() != mutation.hash:
                raise HTTPException(422, "Upload size or SHA-256 checksum mismatch")
            return temp
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    async def commit(
        self, project_id: str, device: Device, mutation: Mutation, temp: Path | None
    ) -> tuple[dict, list[dict]]:
        request_data = mutation.model_dump(mode="json")
        async with self.lock, self.sessions.begin() as session:
            previous = await session.get(Operation, str(mutation.operation_id))
            if previous:
                if (
                    previous.device_id != device.id
                    or previous.project_id != project_id
                    or previous.request != request_data
                ):
                    raise HTTPException(409, "Operation ID already used for a different request")
                return previous.response, []
            project = await session.get(Project, project_id)
            if not project or project.deleted:
                raise HTTPException(404, "Project does not exist")
            current = await session.get(File, (project_id, mutation.path))
            info = FileInfo.model_validate(current.info) if current else None
            if mutation.base_revision > project.revision:
                raise HTTPException(409, "Base revision is ahead of server")
            stale = mutation.base_revision != (info.revision if info else 0)
            same = info is not None and (
                (mutation.operation == "delete" and info.deleted)
                or (
                    mutation.operation == "put"
                    and not info.deleted
                    and info.hash == mutation.hash
                    and info.executable == mutation.executable
                )
            )
            events = []
            conflict = None
            target = mutation.path
            if stale and not same:
                conflict_id = str(mutation.operation_id)
                if mutation.operation == "put":
                    path = Path(target)
                    # Server revision + operation UUID, never a client clock.
                    suffix = f".conflict-{device.id[:8]}-r{project.revision}-{conflict_id[:8]}"
                    stem = path.stem.encode("utf-8")[:80].decode("utf-8", errors="ignore") or "file"
                    extension = path.suffix.encode("utf-8")[:32].decode("utf-8", errors="ignore")
                    name = stem + suffix + extension
                    target = str(path.with_name(name))
                    if len(target.encode("utf-8")) > 1024:
                        target = name
                else:
                    target = None  # A stale delete must never remove the accepted server version.
                conflict = {
                    "id": conflict_id,
                    "path": mutation.path,
                    "conflict_path": target,
                    "base_revision": mutation.base_revision,
                    "accepted_revision": info.revision if info else 0,
                }
                session.add(
                    Conflict(
                        **conflict,
                        project_id=project_id,
                        device_id=device.id,
                        created_at=time.time(),
                    )
                )
                events.append(
                    {
                        "type": "conflict",
                        "project_id": project_id,
                        "path": mutation.path,
                        "conflict_path": target,
                        "conflict_id": conflict_id,
                    }
                )
            result_info = info
            if not same and target is not None:
                # Reject case/normalization collisions AND file/directory prefix collisions.
                files = (
                    await session.scalars(select(File).where(File.project_id == project_id))
                ).all()
                key = path_key(target)
                for other in files:
                    if other.info["deleted"] or other.path == target:
                        continue
                    if paths_collide(other.path, target):
                        raise HTTPException(409, "Portable filename or file/directory collision")
                if conflict and await session.get(File, (project_id, target)):
                    raise HTTPException(
                        409, "Generated conflict filename already exists; original data retained"
                    )
                if mutation.operation == "put":
                    blob = self.blobs / mutation.hash
                    if not blob.exists():
                        used = sum(item.stat().st_size for item in self.blobs.iterdir())
                        if used + mutation.size > self.settings.max_storage_size:
                            raise HTTPException(507, "Server storage quota reached")
                        # Publish durable blob before DB commit. Orphans are harmless after a crash.
                        os.replace(temp, blob)
                        await asyncio.to_thread(fsync_directory, self.blobs)
                project.revision += 1
                result_info = FileInfo(
                    path=target,
                    hash=mutation.hash if mutation.operation == "put" else None,
                    size=mutation.size if mutation.operation == "put" else 0,
                    mtime=mutation.mtime,
                    executable=mutation.executable,
                    revision=project.revision,
                    deleted=mutation.operation == "delete",
                    device_id=device.id,
                )
                data = result_info.model_dump()
                row = await session.get(File, (project_id, target))
                if row:
                    row.info = data
                else:
                    session.add(File(project_id=project_id, path=target, path_key=key, info=data))
                session.add(
                    Revision(
                        project_id=project_id,
                        revision=project.revision,
                        info=data,
                        operation=mutation.operation,
                        moved_from=mutation.moved_from,
                        created_at=time.time(),
                    )
                )
                kind = (
                    "file_deleted"
                    if result_info.deleted
                    else ("file_moved" if mutation.moved_from else "file_changed")
                )
                events.append(
                    {
                        "type": kind,
                        "project_id": project_id,
                        "revision": project.revision,
                        "path": target,
                        "operation": mutation.operation,
                        "origin_device_id": device.id,
                        "moved_from": mutation.moved_from,
                    }
                )
            response = {
                "file": result_info.model_dump() if result_info else None,
                "conflict": conflict,
                "revision": project.revision,
            }
            session.add(
                Operation(
                    id=str(mutation.operation_id),
                    device_id=device.id,
                    project_id=project_id,
                    request=request_data,
                    response=response,
                    created_at=time.time(),
                )
            )
        return response, events
