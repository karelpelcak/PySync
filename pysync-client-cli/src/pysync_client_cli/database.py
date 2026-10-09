"""Single-daemon SQLite state, with persistent immutable operation snapshots."""

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

MIGRATION_001 = """
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, root TEXT UNIQUE NOT NULL,
 cursor INTEGER NOT NULL DEFAULT 0, initialized INTEGER NOT NULL DEFAULT 0,
 accept_local INTEGER NOT NULL DEFAULT 0, ignore_json TEXT NOT NULL DEFAULT '[]',
 last_sync REAL, error TEXT
);
CREATE TABLE files (
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 path TEXT NOT NULL, hash TEXT, executable INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(project_id, path)
);
CREATE TABLE pending (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 path TEXT NOT NULL, metadata TEXT NOT NULL, blob TEXT,
 UNIQUE(project_id, path)
);
CREATE TABLE conflicts (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 metadata TEXT NOT NULL
);
CREATE TABLE scan_errors (
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 path TEXT NOT NULL, message TEXT NOT NULL, PRIMARY KEY(project_id, path)
);
PRAGMA user_version=1;
"""


class Database:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(directory / "state.sqlite")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Local database is newer than this client; upgrade PySync")
        if version < 1:
            self.connection.executescript("BEGIN;\n" + MIGRATION_001 + "\nCOMMIT;")

        if version < 2:
            self.connection.executescript("""BEGIN;
                CREATE TABLE applications (
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL, metadata TEXT NOT NULL,
                    PRIMARY KEY(project_id,path)
                );
                PRAGMA user_version=2;
                COMMIT;
            """)

        if version < 3:
            self.connection.executescript("""BEGIN;
                ALTER TABLE projects ADD COLUMN root_device INTEGER;
                ALTER TABLE projects ADD COLUMN root_inode INTEGER;
                PRAGMA user_version=3;
                COMMIT;
            """)

    def projects(self) -> list[dict]:
        return [dict(r) for r in self.connection.execute("SELECT * FROM projects ORDER BY name")]

    def project(self, value: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id=? OR name=?", (value, value)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown local project: {value}")
        return dict(row)

    def states(self, pid: str) -> dict[str, dict]:
        return {
            r["path"]: dict(r)
            for r in self.connection.execute("SELECT * FROM files WHERE project_id=?", (pid,))
        }

    def pending(self, pid: str) -> list[dict]:
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM pending WHERE project_id=? ORDER BY sequence", (pid,)
            )
        ]

    def set_state(self, pid: str, info: dict) -> None:
        self.connection.execute(
            "INSERT INTO files(project_id,path,hash,executable,revision,deleted) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(project_id,path) DO UPDATE SET hash=excluded.hash, "
            "executable=excluded.executable,revision=excluded.revision,deleted=excluded.deleted",
            (
                pid,
                info["path"],
                info.get("hash"),
                bool(info.get("executable")),
                info.get("revision", 0),
                bool(info.get("deleted")),
            ),
        )

    def record_conflict(self, pid: str, data: dict) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO conflicts VALUES(?,?,?)", (data["id"], pid, json.dumps(data))
        )

    def close(self) -> None:
        self.connection.close()
