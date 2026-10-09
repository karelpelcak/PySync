from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[float] = mapped_column(Float)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)


class Membership(Base):
    __tablename__ = "project_memberships"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    acknowledged_revision: Mapped[int] = mapped_column(Integer, default=0)


class File(Base):
    __tablename__ = "files"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    path_key: Mapped[str] = mapped_column(String, index=True)
    info: Mapped[dict] = mapped_column(JSON)


class Revision(Base):
    """Append-only journal; each row also holds immutable file-version metadata."""

    __tablename__ = "file_revisions"
    __table_args__ = (CheckConstraint("revision > 0"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    info: Mapped[dict] = mapped_column(JSON)
    operation: Mapped[str] = mapped_column(String)
    moved_from: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class Conflict(Base):
    __tablename__ = "conflicts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    path: Mapped[str] = mapped_column(String)
    conflict_path: Mapped[str | None] = mapped_column(String, nullable=True)
    base_revision: Mapped[int] = mapped_column(Integer)
    accepted_revision: Mapped[int] = mapped_column(Integer)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)


class Operation(Base):
    __tablename__ = "transfer_operations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    request: Mapped[dict] = mapped_column(JSON)
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
