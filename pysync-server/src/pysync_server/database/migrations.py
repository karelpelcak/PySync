"""Forward migrations: never change a released migration, append a new version."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pysync_server.database.models import Base

SCHEMA_VERSION = 1


async def migrate(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        version = await connection.scalar(
            text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        )
        if version > SCHEMA_VERSION:
            raise RuntimeError("Database is newer than this server; upgrade PySync")
        if version < 1:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("INSERT INTO schema_migrations(version) VALUES (1)"))
