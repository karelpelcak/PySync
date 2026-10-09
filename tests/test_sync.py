import asyncio
import json
from uuid import uuid4

from conftest import converge, register, root
from pydantic import SecretStr
from pysync_client_cli.config import Config
from pysync_client_cli.daemon import Command, dispatch
from pysync_client_cli.engine import Engine


async def test_create_modify_delete_rename_unicode_and_no_loops(pair):
    a, b = pair
    source = root(a) / "src" / "日本語 space.py"
    source.parent.mkdir()
    source.write_text("first")
    await converge(pair)
    destination = root(b) / "src" / source.name
    assert destination.read_text() == "first"
    source.write_text("second")
    source.chmod(0o755)
    await converge(pair)
    assert destination.read_text() == "second"
    assert destination.stat().st_mode & 0o100
    moved = source.with_name("renamed.py")
    source.rename(moved)
    await converge(pair)
    assert not destination.exists()
    assert (root(b) / "src" / "renamed.py").read_text() == "second"
    moved.unlink()
    await converge(pair)
    assert not (root(b) / "src" / "renamed.py").exists()
    revision = a.db.projects()[0]["cursor"]
    await converge(pair, passes=4)
    assert a.db.projects()[0]["cursor"] == revision
    assert not a.db.pending(a.db.projects()[0]["id"])


async def test_ignores_and_ignore_change_does_not_delete(pair):
    a, b = pair
    (root(a) / "node_modules").mkdir()
    (root(a) / "node_modules" / "huge.js").write_text("ignore")
    (root(a) / ".env").write_text("secret")
    (root(a) / "keep.txt").write_text("safe")
    await converge(pair)
    assert not (root(b) / ".env").exists()
    assert not (root(b) / "node_modules").exists()
    a.config.ignore.append("keep.txt")
    (root(a) / "keep.txt").unlink()
    await converge(pair)
    assert (root(b) / "keep.txt").read_text() == "safe"


async def test_offline_queue_client_restart_server_restart(pair, server):
    a, b = pair
    await server.stop()
    (root(a) / "offline.py").write_text("durable edit")
    await a.tick(force=True)
    assert not a.connected
    assert len(a.db.pending(a.db.projects()[0]["id"])) == 1
    config, directory = a.config, a.directory
    await a.close()
    a = Engine(config, directory)
    pair[0] = a
    assert len(a.db.pending(a.db.projects()[0]["id"])) == 1
    await server.start()
    await converge(pair)
    assert (root(b) / "offline.py").read_text() == "durable edit"
    assert not a.db.pending(a.db.projects()[0]["id"])


async def test_offline_conflict_keeps_both_contents(pair):
    a, b = pair
    (root(a) / "main.py").write_text("base")
    await converge(pair)
    (root(a) / "main.py").write_text("macbook offline")
    await a.scan(a.db.projects()[0])
    (root(b) / "main.py").write_text("linux accepted")
    await b.tick(force=True)
    await converge(pair)
    assert (root(a) / "main.py").read_text() == "linux accepted"
    assert (root(b) / "main.py").read_text() == "linux accepted"
    copies = list(root(a).glob("main.conflict-*.py"))
    assert len(copies) == 1
    assert copies[0].read_text() == "macbook offline"
    assert (root(b) / copies[0].name).read_text() == "macbook offline"
    assert a.status()["conflicts"]


async def test_stale_deletion_cannot_remove_new_edit(pair):
    a, b = pair
    (root(a) / "main.py").write_text("base")
    await converge(pair)
    (root(a) / "main.py").unlink()
    await a.scan(a.db.projects()[0])
    (root(b) / "main.py").write_text("new accepted edit")
    await b.tick(force=True)
    await converge(pair)
    assert (root(a) / "main.py").read_text() == "new accepted edit"
    assert a.status()["conflicts"]


async def test_latest_offline_edits_preserve_original_base(pair):
    a, b = pair
    (root(a) / "main.py").write_text("base")
    await converge(pair)
    (root(a) / "main.py").write_text("queued version")
    await a.scan(a.db.projects()[0])
    (root(a) / "main.py").write_text("latest offline version")
    (root(b) / "main.py").write_text("accepted remote")
    await b.tick(force=True)
    await converge(pair)
    assert (root(a) / "main.py").read_text() == "accepted remote"
    assert {p.read_text() for p in root(a).glob("main.conflict-*.py")} == {
        "queued version",
        "latest offline version",
    }


async def new_client(server, tmp_path, name, root_directory, accept_local=False):
    device = await register(server.url, name)
    engine = Engine(
        Config(
            server_url=server.url,
            device_id=device["id"],
            device_name=name,
            token=SecretStr(device["token"]),
        ),
        tmp_path / (name + "-state"),
    )
    await dispatch(
        engine,
        Command(command="add", root=str(root_directory), name="demo", accept_local=accept_local),
    )
    return engine


async def test_initial_nonempty_preserved_without_upload(pair, server, tmp_path):
    a, _b = pair
    (root(a) / "main.py").write_text("server content")
    await converge(pair)
    directory = tmp_path / "preexisting"
    directory.mkdir()
    (directory / "main.py").write_text("precious local content")
    (directory / "local-only.py").write_text("local only")
    c = await new_client(server, tmp_path, "initial", directory)
    try:
        await c.tick(force=True)
        assert c.db.projects()[0]["error"] is None
        assert (directory / "main.py").read_text() == "server content"
        assert (directory / "local-only.py").read_text() == "local only"
        assert "precious local content" in [
            p.read_text() for p in (directory / ".pysync-recovery").iterdir()
        ]
        await converge([a, c])
        assert not (root(a) / "local-only.py").exists()
        assert c.status()["conflicts"]
    finally:
        await c.close()


async def test_initial_tombstone_keeps_preexisting_local(pair, server, tmp_path):
    a, _b = pair
    (root(a) / "old.py").write_text("deleted remote")
    await converge(pair)
    (root(a) / "old.py").unlink()
    await converge(pair)
    directory = tmp_path / "old-local"
    directory.mkdir()
    (directory / "old.py").write_text("preexisting file")
    c = await new_client(server, tmp_path, "tombstone", directory)
    try:
        await c.tick(force=True)
        assert (directory / "old.py").read_text() == "preexisting file"
        await converge([a, c])
        assert not (root(a) / "old.py").exists()
    finally:
        await c.close()


async def test_simultaneous_writes_conflict(pair):
    a, b = pair
    (root(a) / "race.txt").write_text("a")
    (root(b) / "race.txt").write_text("b")
    await asyncio.gather(*(c.tick(force=True) for c in pair))
    await converge(pair)
    contents = {(root(a) / "race.txt").read_text()} | {
        p.read_text() for p in root(a).glob("race.conflict-*.txt")
    }
    assert contents == {"a", "b"}


async def test_crash_during_remote_application_recovers_before_scan(pair):
    a, b = pair
    (root(a) / "crash.txt").write_text("old")
    await converge(pair)
    (root(a) / "crash.txt").write_text("new")
    await a.tick(force=True)
    info = next(
        f
        for f in (
            await a.request("GET", f"/api/v1/projects/{a.db.projects()[0]['id']}/manifest")
        ).json()["files"]
        if f["path"] == "crash.txt"
    )
    project = b.db.projects()[0]
    source = await b.download(project, info)
    recovery_name = uuid4().hex
    intent = {
        "info": info,
        "target": "crash.txt",
        "source": str(source),
        "expected": [b.db.states(project["id"])["crash.txt"]["hash"], False],
        "recovery_name": recovery_name,
        "initial": False,
    }
    with b.db.connection:
        b.db.connection.execute(
            "INSERT INTO applications VALUES(?,?,?)",
            (project["id"], "crash.txt", json.dumps(intent)),
        )
    recovery = root(b) / ".pysync-recovery"
    recovery.mkdir(exist_ok=True)
    (root(b) / "crash.txt").rename(recovery / recovery_name)
    config, state = b.config, b.directory
    await b.close()
    b = Engine(config, state)
    pair[1] = b
    await converge(pair)
    assert (root(b) / "crash.txt").read_text() == "new"
    assert (recovery / recovery_name).read_text() == "old"
    assert not b.db.pending(project["id"])


async def test_real_watchers_websockets_and_automatic_reconnect(pair, server):
    a, b = pair
    tasks = [asyncio.create_task(c.run()) for c in pair]

    async def eventually(predicate):
        for _ in range(120):
            if predicate():
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"Synchronization timed out: {[c.status() for c in pair]}")

    try:
        await eventually(lambda: a.websocket is not None and b.websocket is not None)
        directory = root(a) / "nested" / "deep"
        directory.mkdir(parents=True)
        source = directory / "live.txt"
        target = root(b) / "nested" / "deep" / "live.txt"
        source.write_text("watchdog live transfer")
        await eventually(lambda: target.exists() and target.read_text() == "watchdog live transfer")
        source.write_text("live update")
        await eventually(lambda: target.exists() and target.read_text() == "live update")
        await server.stop()
        source.write_text("offline live edit")
        await eventually(lambda: bool(a.db.pending(a.db.projects()[0]["id"])))
        await server.start()
        await eventually(lambda: target.exists() and target.read_text() == "offline live edit")
        source.rename(directory / "moved.txt")
        renamed = target.with_name("moved.txt")
        await eventually(lambda: renamed.exists() and not target.exists())
        (directory / "moved.txt").unlink()
        await eventually(lambda: not renamed.exists())
    finally:
        for c in pair:
            c.stopping.set()
            c.wake.set()
        await asyncio.gather(*tasks)


async def test_replaced_root_is_not_interpreted_as_mass_deletion(pair):
    a, b = pair
    (root(a) / "precious.txt").write_text("keep")
    await converge(pair)
    original = root(a)
    original.rename(original.with_name(original.name + "-moved"))
    original.mkdir()
    await a.tick(force=True)
    assert "identity changed" in a.db.projects()[0]["error"]
    assert not a.db.pending(a.db.projects()[0]["id"])
    await b.tick(force=True)
    assert (root(b) / "precious.txt").read_text() == "keep"


async def test_unignore_fetches_updates_missed_while_ignored(pair):
    a, b = pair
    (root(a) / "file.txt").write_text("first")
    await converge(pair)
    b.config.ignore.append("file.txt")
    (root(a) / "file.txt").write_text("second")
    await converge(pair)
    assert (root(b) / "file.txt").read_text() == "first"
    b.config.ignore.remove("file.txt")
    await converge(pair)
    assert (root(b) / "file.txt").read_text() == "second"


async def test_case_only_rename_uses_tombstone_before_creation(pair):
    a, b = pair
    (root(a) / "lower.txt").write_text("rename")
    await converge(pair)
    (root(a) / "lower.txt").rename(root(a) / "LOWER.txt")
    await converge(pair)
    assert (root(b) / "LOWER.txt").read_text() == "rename"


async def test_server_rollback_does_not_rewind_client_state(pair, server):
    from pysync_server.database.models import Project
    from sqlalchemy import select

    a, b = pair
    (root(a) / "preserve.txt").write_text("new revision")
    await converge(pair)
    project = a.db.projects()[0]
    async with server.app.state.sessions.begin() as session:
        row = await session.scalar(select(Project).where(Project.id == project["id"]))
        row.revision = 0
    await a.tick(force=True)
    assert "behind local state" in a.db.projects()[0]["error"]
    assert a.db.projects()[0]["cursor"] == 1
    assert (root(a) / "preserve.txt").read_text() == "new revision"


async def test_new_ignore_rules_pause_existing_queue_without_blocking_sync(pair):
    a, b = pair
    (root(a) / "private.txt").write_text("unsent private data")
    await a.scan(a.db.projects()[0])
    a.config.ignore.append("private.txt")
    (root(a) / "normal.txt").write_text("sync normally")
    await converge(pair)
    assert not (root(b) / "private.txt").exists()
    assert (root(b) / "normal.txt").read_text() == "sync normally"
    assert a.status()["projects"][0]["paused_pending"] == 1
    a.config.ignore.remove("private.txt")
    await converge(pair)
    assert (root(b) / "private.txt").read_text() == "unsent private data"
