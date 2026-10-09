"""Daemon lifecycle shared by CLI and GUI; no synchronization engine is created here."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from pysync_client_cli.config import load_config
from pysync_client_cli.daemon import DaemonUnavailable, rpc


async def start_daemon(directory: Path) -> dict:
    try:
        return await rpc(directory, "status")
    except DaemonUnavailable:
        pass
    config = load_config(directory)
    if not config.device_id or not config.token.get_secret_value():
        raise ValueError("Register the device before starting the daemon")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "startup.log").open("ab") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pysync_client_cli.main",
                "--config-dir",
                str(directory),
                "daemon",
                "run",
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
            env={
                k: v
                for k, v in os.environ.items()
                if k not in ("PYSYNC_OWNER_TOKEN", "PYSYNC_SERVER_OWNER_TOKEN")
            },
        )
    for _ in range(100):
        try:
            return await rpc(directory, "status")
        except DaemonUnavailable:
            if process.poll() is not None:
                raise ValueError(
                    f"Daemon failed to start. Read {directory / 'startup.log'}"
                ) from None
            await asyncio.sleep(0.1)
    raise ValueError("Daemon did not become ready; check startup.log")
