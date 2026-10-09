#!/usr/bin/env python3
"""Generate service files for review; enable them using docs/deployment.md."""

import argparse
import json
import plistlib
import sys
from pathlib import Path

from pysync_client_cli.config import config_dir


def systemd_quote(value: str) -> str:
    return json.dumps(value).replace("%", "%%")


def generate(output: Path, executable: Path, directory: Path) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    unit = output / "pysync-client.service"
    command = " ".join(
        systemd_quote(v) for v in [str(executable), "--config-dir", str(directory), "daemon", "run"]
    )
    unit.write_text(f"""[Unit]
Description=PySync file synchronization client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=5
UMask=0077
TimeoutStopSec=45

[Install]
WantedBy=default.target
""")
    plist = output / "dev.pysync.client.plist"
    with plist.open("wb") as stream:
        plistlib.dump(
            {
                "Label": "dev.pysync.client",
                "ProgramArguments": [
                    str(executable),
                    "--config-dir",
                    str(directory),
                    "daemon",
                    "run",
                ],
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 5,
                "StandardOutPath": str(directory / "service.stdout.log"),
                "StandardErrorPath": str(directory / "service.stderr.log"),
                "Umask": 0o077,
            },
            stream,
        )
    return [unit, plist]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("service-files"))
    parser.add_argument("--config-dir", type=Path, default=config_dir())
    parser.add_argument(
        "--executable", type=Path, default=Path(sys.executable).parent / "pysync-client-cli"
    )
    args = parser.parse_args()
    # No writes into service-manager locations; files are explicitly installed by the user.
    for path in generate(
        args.output_dir.absolute(), args.executable.absolute(), args.config_dir.absolute()
    ):
        print(path)


if __name__ == "__main__":
    main()
