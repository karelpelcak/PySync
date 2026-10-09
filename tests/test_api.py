import hashlib
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import websockets
from conftest import OWNER, register
from websockets.exceptions import InvalidStatus


@asynccontextmanager
async def setup_api(server):
    device = await register(server.url, "api-device")
    http = httpx.AsyncClient(
        base_url=server.url, headers={"Authorization": "Bearer " + device["token"]}
    )
    response = await http.post("/api/v1/projects", json={"name": "api-test"})
    assert response.status_code == 201, response.text
    try:
        yield http, response.json()["id"]
    finally:
        await http.aclose()


def metadata(content=b"hello", path="main.py", revision=0, **kwargs):
    return {
        "operation_id": str(uuid4()),
        "path": path,
        "base_revision": revision,
        "hash": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        **kwargs,
    }


async def upload(http, pid, meta, content=b"hello"):
    return await http.post(
        f"/api/v1/projects/{pid}/files/upload",
        headers={"X-PySync-Operation": json.dumps(meta)},
        content=content,
    )


async def test_auth_and_membership(server):
    async with httpx.AsyncClient(base_url=server.url) as http:
        assert (await http.get("/api/v1/health")).status_code == 200
        assert (await http.get("/api/v1/projects")).status_code == 401
        assert (
            await http.post("/api/v1/devices/register", json={"name": "intruder"})
        ).status_code == 401
    async with setup_api(server) as (http, pid):
        other = await register(server.url, "other")
        async with httpx.AsyncClient(
            base_url=server.url, headers={"Authorization": "Bearer " + other["token"]}
        ) as h:
            assert (await h.get(f"/api/v1/projects/{pid}/manifest")).status_code == 403
            assert (
                await h.get(f"/api/v1/projects/{pid}/files/download", params={"revision": 1})
            ).status_code == 403
            assert (await h.post(f"/api/v1/projects/{pid}/join")).status_code == 200
            assert (await h.get(f"/api/v1/projects/{pid}/manifest")).status_code == 200
    with pytest.raises(InvalidStatus):
        async with websockets.connect(server.url.replace("http", "ws") + "/ws"):
            pass


@pytest.mark.parametrize(
    "path", ["../outside", "/tmp/file", "a/../../x", "a\\b", "a//b", ".pysync-recovery/x", "a\x00b"]
)
async def test_traversal_rejected(server, path):
    async with setup_api(server) as (http, pid):
        assert (await upload(http, pid, metadata(path=path))).status_code == 422
        manifest = (await http.get(f"/api/v1/projects/{pid}/manifest")).json()
        assert manifest["revision"] == 0


async def test_duplicate_operations_and_immutable_revisions(server):
    async with setup_api(server) as (http, pid):
        meta = metadata()
        first = await upload(http, pid, meta)
        assert first.status_code == 200, first.text
        assert (await upload(http, pid, meta)).json() == first.json()
        assert first.json()["revision"] == 1
        meta["path"] = "another"
        assert (await upload(http, pid, meta)).status_code == 409
        second = await upload(http, pid, metadata(b"updated", revision=1), b"updated")
        assert second.json()["revision"] == 2
        download = await http.get(f"/api/v1/projects/{pid}/files/download", params={"revision": 1})
        assert download.content == b"hello"
        assert (
            len(
                (
                    await http.get(
                        f"/api/v1/projects/{pid}/changes", params={"after": 0, "limit": 1}
                    )
                ).json()
            )
            == 1
        )


async def test_checksum_size_limits_and_interrupted_upload(server):
    async with setup_api(server) as (http, pid):
        assert (await upload(http, pid, metadata(), b"wrong")).status_code == 422
        assert (await upload(http, pid, metadata(), b"truncated")).status_code == 413
        large = metadata()
        large["size"] = 101 * 1024 * 1024
        assert (await upload(http, pid, large)).status_code == 413
        reader, writer = await __import__("asyncio").open_connection("127.0.0.1", server.port)
        token = http.headers["Authorization"]
        body_meta = json.dumps(metadata(b"a" * 10000))
        request = (
            f"POST /api/v1/projects/{pid}/files/upload HTTP/1.1\r\nHost: localhost\r\n"
            f"Authorization: {token}\r\nX-PySync-Operation: {body_meta}\r\n"
            "Content-Length: 10000\r\n\r\na"
        )
        writer.write(request.encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await __import__("asyncio").sleep(0.1)
        assert (await http.get(f"/api/v1/projects/{pid}/manifest")).json()["revision"] == 0
        assert not list((server.directory / "transfers").iterdir())


async def test_case_and_prefix_collisions(server):
    async with setup_api(server) as (http, pid):
        assert (await upload(http, pid, metadata(path="Readme"))).status_code == 200
        assert (await upload(http, pid, metadata(path="README"))).status_code == 409
        assert (await upload(http, pid, metadata(path="Readme/child"))).status_code == 409


async def test_conflict_and_stale_delete(server):
    async with setup_api(server) as (http, pid):
        assert (await upload(http, pid, metadata())).status_code == 200
        response = await upload(http, pid, metadata(b"offline", revision=0), b"offline")
        assert response.status_code == 200
        result = response.json()
        assert ".conflict-" in result["conflict"]["conflict_path"]
        assert (await upload(http, pid, metadata(operation="delete", revision=0), b"")).json()[
            "conflict"
        ]
        manifest = (await http.get(f"/api/v1/projects/{pid}/manifest")).json()
        accepted = next(f for f in manifest["files"] if f["path"] == "main.py")
        assert not accepted["deleted"]
        assert accepted["hash"] == hashlib.sha256(b"hello").hexdigest()
        assert len((await http.get(f"/api/v1/projects/{pid}/conflicts")).json()) == 2


async def test_server_restart_keeps_operations(server):
    async with setup_api(server) as (http, pid):
        meta = metadata(path="žluťoučký/日本語 file.txt")
        first = (await upload(http, pid, meta)).json()
        await server.stop()
        await server.start()
        assert (await upload(http, pid, meta)).json() == first
        response = await http.get(f"/api/v1/projects/{pid}/files/download", params={"revision": 1})
        assert response.content == b"hello"


async def test_owner_revokes_device(server):
    device = await register(server.url, "revoked")
    async with httpx.AsyncClient(base_url=server.url) as http:
        assert (
            await http.delete(
                f"/api/v1/devices/{device['id']}", headers={"Authorization": "Bearer " + OWNER}
            )
        ).status_code == 200
        assert (
            await http.get(
                "/api/v1/projects", headers={"Authorization": "Bearer " + device["token"]}
            )
        ).status_code == 401


async def test_unicode_conflict_name_respects_byte_limits(server):
    async with setup_api(server) as (http, pid):
        path = "文" * 75 + ".py"
        assert (await upload(http, pid, metadata(path=path))).status_code == 200
        response = await upload(http, pid, metadata(b"offline", path=path), b"offline")
        assert response.status_code == 200, response.text
        conflict_path = response.json()["conflict"]["conflict_path"]
        assert len(conflict_path.encode("utf-8")) <= 240
        assert ".conflict-" in conflict_path
