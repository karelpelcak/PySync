# PySync client

Part of the PySync uv workspace. Run `uv sync`, then `uv run pysync-client-cli --help`.

Register with `init` and `login`, start the daemon, and attach directories with `project add`. The CLI manages one background engine through a private local Unix socket. Use `daemon run` for foreground execution, systemd, or launchd. Each device needs its own configuration and SQLite state.

See the [repository README](../README.md), [service setup](../docs/deployment.md), and [filesystem/conflict behavior](../docs/operations.md).
