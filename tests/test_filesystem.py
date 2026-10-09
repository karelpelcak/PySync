import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from conftest import converge, root
from pydantic import SecretStr
from pysync_client_cli.config import Config, load_config, save_config
from pysync_client_cli.filesystem import Scanner
from pysync_shared.paths import UnsafePath, apply_file, normalize_path, paths_collide, read_file


def test_path_normalization_and_directory_case_collisions():
    assert normalize_path("cafe\u0301.py") == "café.py"
    assert paths_collide("Src/a", "src/b")
    assert paths_collide("A", "a/b")
    assert not paths_collide("a/x", "a/y")
    assert not paths_collide("a", "b/a")


def test_symlinks_and_special_files_are_rejected(tmp_path):
    root_dir, outside = tmp_path / "root", tmp_path / "outside"
    root_dir.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("must stay outside")
    (root_dir / "escape").symlink_to(outside, target_is_directory=True)
    (root_dir / "link").symlink_to(outside / "secret")
    os.mkfifo(root_dir / "socket-like")
    files, errors = Scanner(root_dir, [], 1024).list_files()
    assert files == {}
    assert set(errors) == {"escape", "link", "socket-like"}
    for path in ("escape/secret", "link", "socket-like"):
        with pytest.raises((OSError, UnsafePath)):
            read_file(root_dir, path)
    source = tmp_path / "download"
    source.write_text("remote")
    with pytest.raises(OSError):
        apply_file(root_dir, "escape/secret", source, False)
    assert (outside / "secret").read_text() == "must stay outside"


def test_safe_publication_preserves_old_inode_and_permissions(tmp_path):
    directory = tmp_path / "root"
    directory.mkdir()
    source = tmp_path / "verified"
    source.write_bytes(b"remote data" * 1024)
    destination = directory / "file"
    destination.write_bytes(b"old content")
    old = destination.open("rb")
    try:
        backup = apply_file(directory, "file", source, True)
        assert old.read() == b"old content"
        assert backup.read_bytes() == b"old content"
        assert destination.read_bytes() == source.read_bytes()
        assert destination.stat().st_mode & 0o100
        assert not list(directory.glob(".pysync-*")) == []  # Recovery folder remains.
        backup2 = apply_file(directory, "file", None, False)
        assert not destination.exists()
        assert backup2.read_bytes() == source.read_bytes()
    finally:
        old.close()


def test_editor_intervening_save_is_not_overwritten(tmp_path):
    directory = tmp_path / "root"
    directory.mkdir()
    source = tmp_path / "verified"
    source.write_text("remote")
    destination = directory / "file"
    destination.write_text("previous")
    original_link = os.link

    def intervening_link(src, dst, **kwargs):
        destination.write_text("concurrent editor")
        return original_link(src, dst, **kwargs)

    with patch("pysync_shared.paths.os.link", side_effect=intervening_link):
        with pytest.raises(FileExistsError):
            apply_file(directory, "file", source, False)
    assert destination.read_text() == "concurrent editor"
    assert [p.read_text() for p in (directory / ".pysync-recovery").iterdir()] == ["previous"]


def test_private_credentials_and_tls_validation(tmp_path):
    config = Config(token=SecretStr("test-secret"), server_url="https://pi.example")
    save_config(tmp_path, config)
    assert load_config(tmp_path).token.get_secret_value() == "test-secret"
    assert (tmp_path / "config.json").stat().st_mode & 0o077 == 0
    (tmp_path / "config.json").chmod(0o644)
    with pytest.raises(ValueError, match="private permissions"):
        load_config(tmp_path)
    with pytest.raises(ValueError, match="HTTPS"):
        Config(server_url="http://pi.local").check_transport()
    Config(server_url="http://pi.local", allow_insecure=True).check_transport()


async def test_incomplete_scan_never_infers_deletion(pair):
    a, b = pair
    (root(a) / "known").write_text("safe")
    await converge(pair)
    (root(a) / "known").unlink()
    (root(a) / "unsafe").symlink_to(root(b) / "known")
    await converge(pair)
    assert (root(b) / "known").read_text() == "safe"
    assert a.status()["projects"][0]["scan_errors"]


async def test_download_checksum_mismatch_keeps_local_contents(pair):
    a, b = pair
    (root(a) / "main.py").write_text("good")
    await a.tick(force=True)
    manifest = (
        await b.request("GET", f"/api/v1/projects/{b.db.projects()[0]['id']}/manifest")
    ).json()
    info = manifest["files"][0]
    original = b.http
    b.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"bad", request=request)
        ),
        base_url="http://test",
    )
    try:
        with pytest.raises(ValueError, match="SHA-256"):
            await b.download(b.db.projects()[0], info)
        assert not (root(b) / "main.py").exists()
        assert not list(b.spool.iterdir())
    finally:
        await b.http.aclose()
        b.http = original


async def test_queued_operation_identity_survives_lost_response(pair):
    a, b = pair
    (root(a) / "retry.py").write_text("retry content")
    project = a.db.projects()[0]
    await a.scan(project)
    row = a.db.pending(project["id"])[0]
    meta = json.loads(row["metadata"])
    response = await a.request(
        "POST",
        f"/api/v1/projects/{project['id']}/files/upload",
        headers={"X-PySync-Operation": json.dumps(meta)},
        content=Path(row["blob"]).read_bytes(),
    )
    assert response.json()["revision"] == 1
    # No local completion: emulate losing the first successful HTTP response.
    await converge(pair)
    assert a.db.projects()[0]["cursor"] == 1
    assert (root(b) / "retry.py").read_text() == "retry content"


async def test_duplicate_watchdog_events_only_queue_one_snapshot(pair):
    a, _b = pair
    (root(a) / "dup").write_text("unchanged")
    for _ in range(10):
        await a.scan(a.db.projects()[0])
    assert len(a.db.pending(a.db.projects()[0]["id"])) == 1
    await converge(pair)
    assert a.db.projects()[0]["cursor"] == 1


async def test_new_local_file_colliding_with_remote_is_preserved(pair):
    a, b = pair
    (root(a) / "Readme").write_text("server")
    await a.tick(force=True)
    (root(b) / "README").write_text("precious local")
    await b.tick(force=True)
    assert (root(b) / "README").read_text() == "precious local"
    assert b.db.projects()[0]["error"]


def test_unchanged_files_do_not_write_snapshots(tmp_path):
    project, spool = tmp_path / "project", tmp_path / "spool"
    project.mkdir()
    spool.mkdir()
    (project / "file").write_text("unchanged")
    scanner = Scanner(project, [], 1024)
    info, snapshot = scanner.snapshot("file", spool)
    snapshot.unlink()
    with patch("pathlib.Path.open", side_effect=AssertionError("Unexpected snapshot write")):
        same, snapshot = scanner.snapshot("file", spool, (info["hash"], info["executable"]))
    assert same["hash"] == info["hash"]
    assert snapshot is None
    assert not list(spool.iterdir())


def test_internal_temporary_files_are_always_ignored(tmp_path):
    (tmp_path / ".pysync-interrupted-write").write_text("temporary")
    scanner = Scanner(tmp_path, ["!.pysync-*"], 1024)
    files, errors = scanner.list_files()
    assert files == {}
    assert errors == {}
