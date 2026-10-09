import asyncio
import os
import sys
import tempfile
from pathlib import Path

from conftest import register
from pydantic import SecretStr
from pysync_client_cli.config import Config, save_config
from pysync_client_cli.daemon import rpc


async def run_cli(directory: Path, *args: str, env: dict | None = None):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pysync_client_cli.main",
        "--config-dir",
        str(directory),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    return process.returncode, stdout.decode() + stderr.decode()


async def eventually(predicate, timeout=15):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await predicate():
                return
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("Daemon operation timed out")


async def test_two_independent_daemon_processes_and_cli(server):
    with tempfile.TemporaryDirectory(prefix="pysync-e2e-") as temp:
        workspace = Path(temp).resolve()
        configs, roots, processes = [], [], []
        try:
            for name in ("client-a", "client-b"):
                directory = workspace / name
                project = workspace / (name + "-files")
                project.mkdir()
                device = await register(server.url, name)
                save_config(
                    directory,
                    Config(
                        server_url=server.url,
                        device_id=device["id"],
                        device_name=name,
                        token=SecretStr(device["token"]),
                        poll_seconds=0.2,
                        debounce_ms=100,
                    ),
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "pysync_client_cli.main",
                    "--config-dir",
                    str(directory),
                    "daemon",
                    "run",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                processes.append(process)
                configs.append(directory)
                roots.append(project)

                async def ready(d=directory):
                    return bool(await rpc(d, "status"))

                await eventually(ready)
                code, output = await run_cli(
                    directory,
                    "project",
                    "add",
                    str(project),
                    "--name",
                    "process-demo",
                    "--accept-local",
                    "--yes",
                )
                assert code == 0, output

            async def both_ready():
                status = await asyncio.gather(*(rpc(d, "status") for d in configs))
                return all(s["projects"][0]["initialized"] for s in status)

            await eventually(both_ready)
            source = roots[0] / "subdir" / "file with spaces.py"
            source.parent.mkdir()
            source.write_text("created through a real daemon")
            target = roots[1] / "subdir" / source.name

            async def copied():
                return target.exists() and target.read_text() == source.read_text()

            await eventually(copied)
            source.write_text("modified through a real daemon")
            await eventually(copied)
            code, output = await run_cli(configs[0], "status")
            assert code == 0 and "process-demo" in output, output
            code, output = await run_cli(configs[0], "daemon", "run")
            assert code == 1 and "already manages" in output, output
            await rpc(configs[0], "stop")
            await asyncio.wait_for(processes[0].wait(), 10)
            # CLI start creates a detached subprocess; exercise its lifecycle and IPC.
            code, output = await run_cli(configs[0], "daemon", "start")
            assert code == 0, output
            source.write_text("after daemon restart")
            await eventually(copied)
            source.unlink()

            async def deleted():
                return not target.exists()

            await eventually(deleted)
            code, output = await run_cli(configs[0], "project", "remove", "process-demo")
            assert code == 0 and "retained" in output, output
        finally:
            for directory in configs:
                try:
                    await rpc(directory, "stop")
                except ValueError:
                    pass
            for process in processes:
                if process.returncode is None:
                    await asyncio.wait_for(process.wait(), 10)
            await asyncio.sleep(0.4)


async def test_cli_init_and_login(server):
    with tempfile.TemporaryDirectory(prefix="pysync-cli-") as temp:
        directory = Path(temp).resolve() / "state"
        code, output = await run_cli(directory, "init", "--server-url", server.url)
        assert code == 0, output
        env = os.environ.copy()
        from conftest import OWNER

        env["PYSYNC_OWNER_TOKEN"] = OWNER
        code, output = await run_cli(directory, "login", "--device-name", "cli-login", env=env)
        assert code == 0 and "Registered cli-login" in output, output
        assert (directory / "config.json").stat().st_mode & 0o077 == 0
        code, output = await run_cli(directory, "status")
        assert code == 1 and "Daemon is not running" in output, output


async def test_large_json_body_is_rejected(server):
    import httpx

    async with httpx.AsyncClient(base_url=server.url) as http:
        response = await http.post("/api/v1/devices/register", json={"name": "x" * 20000})
        assert response.status_code == 413


async def test_disposable_development_demo_runs_real_server_and_clients():
    import signal
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "scripts/local_demo.py",
        "--port",
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        first = (await asyncio.wait_for(process.stdout.readline(), 15)).decode().strip()
        second = (await asyncio.wait_for(process.stdout.readline(), 15)).decode().strip()
        assert first.startswith("macbook:"), first
        assert second.startswith("linux:"), second
        a = Path(first.split(": ", 1)[1].split(" (project", 1)[0])
        b = Path(second.split(": ", 1)[1].split(" (project", 1)[0])
        (a / "smoke.txt").write_text("full demo")

        async def copied():
            return (b / "smoke.txt").exists() and (b / "smoke.txt").read_text() == "full demo"

        await eventually(copied)
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, (stdout + stderr).decode()
