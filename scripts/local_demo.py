#!/usr/bin/env python3
"""Start a disposable localhost server and two real clients; keep them running until Ctrl-C."""

import argparse
import asyncio
import os
import secrets
import signal
import sys
import tempfile
from pathlib import Path

import httpx
from pydantic import SecretStr
from pysync_client_cli.config import Config, save_config
from pysync_client_cli.daemon import rpc


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pysync-demo-") as temporary:
        directory = Path(temporary).resolve()
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopped.set)
        processes = []
        states = []
        try:
            owner = secrets.token_urlsafe(32)
            env = os.environ | {
                "PYSYNC_SERVER_OWNER_TOKEN": owner,
                "PYSYNC_SERVER_DATA_DIR": str(directory / "server"),
                "PYSYNC_SERVER_PORT": str(args.port),
            }
            server_log = (directory / "server.log").open("wb")
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "pysync_server.main",
                    env=env,
                    stdout=server_log,
                    stderr=server_log,
                )
            )
            server_log.close()
            url = f"http://127.0.0.1:{args.port}"
            async with httpx.AsyncClient(base_url=url) as http:
                for _ in range(100):
                    try:
                        if processes[0].returncode is not None:
                            raise RuntimeError("Demo server failed to start; check its log")
                        response = await http.get("/api/v1/health")
                        if response.is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Demo server did not start")
                for name in ("macbook", "linux"):
                    state, project = directory / (name + "-state"), directory / name
                    project.mkdir()
                    device = (
                        await http.post(
                            "/api/v1/devices/register",
                            headers={"Authorization": "Bearer " + owner},
                            json={"name": name},
                        )
                    ).json()
                    save_config(
                        state,
                        Config(
                            server_url=url,
                            device_name=name,
                            device_id=device["id"],
                            token=SecretStr(device["token"]),
                            poll_seconds=1,
                        ),
                    )
                    output = (state / "startup.log").open("wb")
                    processes.append(
                        await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "pysync_client_cli.main",
                            "--config-dir",
                            str(state),
                            "daemon",
                            "run",
                            stdout=output,
                            stderr=output,
                        )
                    )
                    output.close()
                    states.append(state)
                    for _ in range(100):
                        try:
                            await rpc(state, "status")
                            break
                        except ValueError:
                            await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError("Demo daemon did not start")
                    plan = await rpc(
                        state, "add", root=str(project), name="demo", accept_local=True
                    )
                    print(f"{name}: {project} (project {plan['project_id']})", flush=True)
            print(
                f"Edit either directory. Status: uv run pysync-client-cli --config-dir '{states[0]}' status",
                flush=True,
            )
            print(
                f"Logs and state: {directory}. Ctrl-C stops everything and removes this disposable demo.",
                flush=True,
            )
            await stopped.wait()
        finally:
            for state in states:
                try:
                    await rpc(state, "stop")
                except (ValueError, OSError):
                    pass
            for process in reversed(processes):
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 15)
                    except TimeoutError:
                        process.kill()
                        await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
