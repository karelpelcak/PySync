# PySync server

Part of the PySync uv workspace. Run `uv sync`, set `PYSYNC_SERVER_OWNER_TOKEN` to a random secret of at least 24 characters, then `uv run pysync-server`.

The default listener is `127.0.0.1:8000`; data is stored in `.pysync-data` relative to the current directory. Configure an absolute `PYSYNC_SERVER_DATA_DIR` for services. API documentation is available at `/docs`; file/project APIs require a device bearer token. Owner credentials register and revoke devices and archive projects.

See the [repository README](../README.md), [deployment guide](../docs/deployment.md), and [protocol](../docs/architecture.md). The server process must have exclusive ownership of its data directory; use one worker.
