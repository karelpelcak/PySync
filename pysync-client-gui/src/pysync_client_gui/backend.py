"""UI operations reuse the client's configuration, lifecycle and private daemon RPC."""

import asyncio
import fcntl
import json
import os
import sqlite3
from collections import deque
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from pysync_client_cli.config import Config, load_config, save_config
from pysync_client_cli.daemon import DaemonUnavailable, rpc
from pysync_client_cli.lifecycle import start_daemon
from pysync_shared.protocol import DeviceRegistration


@contextmanager
def configuration_lock(directory: Path):
    if directory.is_symlink():
        raise ValueError("Configuration directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "daemon.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Nejprve zastavte synchronizační službu, pak uložte nastavení."
            ) from None
        yield
    finally:
        os.close(fd)


class Backend:
    def __init__(self, directory: Path):
        self.directory = directory.absolute()

    def preferences(self) -> dict[str, Any]:
        config = load_config(self.directory)
        return {
            key: getattr(config, key)
            for key in (
                "server_url",
                "device_name",
                "device_id",
                "allow_insecure",
                "poll_seconds",
                "debounce_ms",
                "max_file_size",
                "ignore",
            )
        }

    async def status(self) -> dict[str, Any]:
        preferences = self.preferences()
        try:
            status = await rpc(self.directory, "status")
            status["running"] = True
        except DaemonUnavailable:
            status = await asyncio.to_thread(self.stopped_status)
            status["running"] = False
        status["preferences"] = preferences
        return status

    def stopped_status(self) -> dict[str, Any]:
        """Read persisted diagnostics without creating a database or starting an engine."""
        result = {
            "connected": False,
            "projects": [],
            "conflicts": [],
            "error": None,
            "transfer": None,
        }
        path = self.directory / "state.sqlite"
        if not path.exists():
            return result
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
            database.row_factory = sqlite3.Row
            for row in database.execute("SELECT * FROM projects ORDER BY name"):
                project = dict(row)
                project["pending"] = database.execute(
                    "SELECT COUNT(*) FROM pending WHERE project_id=?", (project["id"],)
                ).fetchone()[0]
                project["scan_errors"] = [
                    dict(r)
                    for r in database.execute(
                        "SELECT path,message FROM scan_errors WHERE project_id=?", (project["id"],)
                    )
                ]
                result["projects"].append(project)
            result["conflicts"] = [
                json.loads(row[0]) for row in database.execute("SELECT metadata FROM conflicts")
            ]
        return result

    async def start(self) -> dict[str, Any]:
        await start_daemon(self.directory)
        return await self.status()

    async def stop(self) -> dict[str, Any]:
        await rpc(self.directory, "stop")
        for _ in range(400):
            try:
                await rpc(self.directory, "status")
            except DaemonUnavailable:
                return await self.status()
            await asyncio.sleep(0.1)
        raise ValueError("Služba stále dokončuje operaci. Zkuste za chvíli obnovit stav.")

    async def sync(self, name: str | None = None) -> dict[str, Any]:
        await rpc(self.directory, "sync", name=name)
        return await self.status()

    async def projects(self) -> dict[str, Any]:
        return await rpc(self.directory, "list_projects")

    async def plan(self, **kwargs: Any) -> dict[str, Any]:
        return await rpc(self.directory, "add", preview=True, **kwargs)

    async def add(self, **kwargs: Any) -> dict[str, Any]:
        return await rpc(self.directory, "add", **kwargs)

    async def remove(self, name: str) -> dict[str, Any]:
        # No discard flag: pending user data cannot be thrown away by the GUI.
        return await rpc(self.directory, "remove", name=name)

    async def resolve(self, conflict_id: str) -> dict[str, Any]:
        return await rpc(self.directory, "resolve", conflict_id=conflict_id)

    async def configure(self, values: dict[str, Any], owner_token: str = "") -> dict[str, Any]:
        with configuration_lock(self.directory):
            current = load_config(self.directory)
            config = Config.model_validate(current.model_dump() | values)
            config.check_transport()
            if current.device_id and config.server_url != current.server_url:
                raise ValueError(
                    "Registrované zařízení je vázané na svůj server. Změňte server pomocí nové konfigurace klienta."
                )
            if not config.device_id:
                registration = DeviceRegistration(name=config.device_name.strip())
                if len(owner_token.strip()) < 24:
                    raise ValueError("Zadejte token vlastníka serveru (alespoň 24 znaků).")
                async with httpx.AsyncClient(timeout=15) as http:
                    response = await http.post(
                        config.server_url + "/api/v1/devices/register",
                        headers={"Authorization": "Bearer " + owner_token.strip()},
                        json=registration.model_dump(),
                    )
                    response.raise_for_status()
                device = response.json()
                config.device_id = device["id"]
                config.device_name = device["name"]
                config.token = SecretStr(device["token"])
            save_config(self.directory, config)
        return self.preferences()

    def logs(self, count: int = 200) -> str:
        path = self.directory / "daemon.log"
        if not path.exists():
            return "Služba zatím nevytvořila žádné záznamy."
        with path.open() as stream:
            lines = deque(stream, maxlen=count)
        formatted = []
        for line in lines:
            try:
                row = json.loads(line)
                formatted.append(
                    f"{row.get('time', '')}  {row.get('level', ''):<7}  {row.get('message', '')}"
                )
            except ValueError:
                formatted.append(line.rstrip())
        return "\n".join(formatted)
