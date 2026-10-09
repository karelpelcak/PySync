from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pysync_shared.paths import normalize_path


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceRegistration(Model):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w .-]+$")


class ProjectCreate(Model):
    name: str = Field(min_length=1, max_length=100)


class FileInfo(Model):
    path: str
    hash: str | None = None
    size: int = 0
    mtime: float = 0
    executable: bool = False
    revision: int = 0
    deleted: bool = False
    device_id: str = ""

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return normalize_path(value)


class Mutation(Model):
    operation_id: UUID
    path: str = Field(max_length=1024)
    base_revision: int = Field(ge=0)
    hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size: int = Field(default=0, ge=0)
    mtime: float = Field(default=0, ge=0, allow_inf_nan=False)
    executable: bool = False
    operation: Literal["put", "delete"] = "put"
    # Moves use a linked delete/create pair: each path has its own optimistic base revision.
    moved_from: str | None = None

    @field_validator("path", "moved_from")
    @classmethod
    def valid_path(cls, value: str | None) -> str | None:
        return normalize_path(value) if value is not None else None


class FileChanged(Model):
    type: Literal["file_changed", "file_deleted", "file_moved"] = "file_changed"
    project_id: str
    revision: int
    path: str
    operation: Literal["put", "delete"]
    origin_device_id: str
    moved_from: str | None = None


class Presence(Model):
    type: Literal["presence"] = "presence"
    device_id: str
    online: bool


class Heartbeat(Model):
    type: Literal["heartbeat"] = "heartbeat"


class SyncRequest(Model):
    type: Literal["sync_request"] = "sync_request"


class SyncStatus(Model):
    type: Literal["sync_status"] = "sync_status"
    device_id: str
    online: bool = True


class Acknowledge(Model):
    type: Literal["ack"] = "ack"
    project_id: UUID
    revision: int = Field(ge=0)


class ConflictEvent(Model):
    type: Literal["conflict"] = "conflict"
    project_id: str
    path: str
    conflict_path: str | None
    conflict_id: str


class ErrorEvent(Model):
    type: Literal["error"] = "error"
    message: str


Event = Annotated[
    FileChanged
    | Presence
    | Heartbeat
    | SyncRequest
    | SyncStatus
    | Acknowledge
    | ConflictEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
