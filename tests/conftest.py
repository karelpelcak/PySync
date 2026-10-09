import asyncio
import socket
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from pysync_client_cli.config import Config
from pysync_client_cli.daemon import Command, dispatch
from pysync_client_cli.engine import Engine
from pysync_server.config import Settings
from pysync_server.main import create_app

OWNER = "test-owner-token-at-least-32-characters"


@dataclass
class RunningServer:
    directory: Path
    port: int
    server: uvicorn.Server | None = None
    task: asyncio.Task | None = None
    app: object = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, **kwargs) -> None:
        self.app = create_app(
            Settings(data_dir=self.directory, owner_token=SecretStr(OWNER), **kwargs)
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
            )
        )
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                return
            if self.task.done():
                await self.task
                raise RuntimeError("Server did not start")
            await asyncio.sleep(0.02)
        raise RuntimeError("Server startup timed out")

    async def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
            await asyncio.wait_for(self.task, 10)
            self.server = None


@pytest.fixture
async def server(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    instance = RunningServer(tmp_path / "server", port)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


async def register(url: str, name: str) -> dict:
    async with httpx.AsyncClient(base_url=url) as http:
        response = await http.post(
            "/api/v1/devices/register",
            headers={"Authorization": "Bearer " + OWNER},
            json={"name": name},
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture
async def pair(server, tmp_path):
    clients = []
    for name in ("macbook", "linux"):
        device = await register(server.url, name)
        config = Config(
            server_url=server.url,
            device_id=device["id"],
            device_name=name,
            token=SecretStr(device["token"]),
            debounce_ms=100,
            poll_seconds=0.2,
        )
        engine = Engine(config, tmp_path / (name + "-state"))
        root = tmp_path / name
        root.mkdir()
        await dispatch(
            engine, Command(command="add", root=str(root), name="demo", accept_local=True)
        )
        await engine.tick(force=True)
        clients.append(engine)
    try:
        yield clients
    finally:
        for engine in clients:
            if not engine.http.is_closed:
                await engine.close()


async def converge(clients: list[Engine], passes: int = 3) -> None:
    for _ in range(passes):
        for client in clients:
            await client.tick(force=True)
            for project in client.db.projects():
                assert project["error"] is None, project["error"]


def root(engine: Engine) -> Path:
    return Path(engine.db.projects()[0]["root"])
