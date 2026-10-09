# Executed verification

The final implementation was verified on macOS on 2026-10-09 with Python **3.12.15**:

```text
.venv/bin/pytest -q --tb=short
56 passed in 25.96s

.venv/bin/ruff check .
All checks passed!

.venv/bin/ruff format --check .
47 files already formatted
```

The earlier 43-test suite also passed on Python 3.13.14 before the final additions. The 47-test server/CLI suite passed before the desktop implementation; nine optional GUI tests were subsequently added. No Linux or Raspberry Pi machine was available in this session. Actual systemd/launchd service installation and Tailscale/TLS deployment were documented, not executed on the host. TLS checks cover secure client URL policy and private credentials, not a live certificate/proxy deployment.

Verified behaviors include:

- Real HTTP server operation, project creation/membership authorization, owner/device authentication, invalid WebSocket authentication, and device revocation.
- Streamed creation, modification, deletion, file rename, case-only rename, recursive monitoring, executable flag, Unicode filenames and paths containing spaces.
- Initial synchronization, non-empty roots, preserved differing local content, local-only files, and pre-existing files protected from initial tombstones.
- Persistent offline snapshots, daemon restart, server restart, automatic WebSocket reconnection, and journal recovery.
- Simultaneous edits, stale edits, stale deletes, preservation of successive offline edits, deterministic conflict copies, and byte-safe long Unicode conflict filenames.
- Duplicate filesystem events, duplicate operation submissions, immutable historical download, and retry after an accepted request's response is lost.
- Ignored files/directories, changed ignore rules, paused previously queued ignored operations, and retrieval of remote updates missed while ignored.
- Directory traversal, case/prefix collisions, symlink escapes, FIFO rejection, private credentials and metadata body bounds.
- Interrupted uploads, maximum upload size, SHA-256 upload/download mismatches, complete inode publication, preserved previous inodes, and refusing to overwrite a concurrently recreated destination.
- Remote-application crash replay before deletion scanning, replaced/unmounted root detection, and server revision rollback detection.
- Singleton daemon locking, private Unix-socket CLI management, detached daemon start/stop, registration, project attachment/removal and status.
- Real offscreen Qt widgets, first-run onboarding, private owner-to-device registration, retained inputs after errors/refresh, background worker responsiveness, main-thread delivery, duplicate submission suppression and closing during an active request.
- GUI-driven startup of two independent daemon subprocesses, project-plan review before local attachment, preserved differing initial contents, Unicode file creation/modification/rename/deletion, conflict resolution retaining copies, log display and restart through the actual buttons.
- Read-only stopped diagnostics, prohibition of pending-work detachment, cleared connection errors after reconnection, and configuration locking while a daemon owns state.

Two tests provide full local-process demonstrations:

```sh
uv run pytest -q tests/test_daemon.py::test_two_independent_daemon_processes_and_cli
uv run pytest -q tests/test_daemon.py::test_disposable_development_demo_runs_real_server_and_clients
```

The first starts a real Uvicorn server and **two independent daemon subprocesses**, manages them using the actual CLI/Unix-socket interface, and checks creation, modification, deletion, singleton locking and restart. The second runs `scripts/local_demo.py`, which starts a **server subprocess and two daemon subprocesses**, and verifies a file transfer through that server before shutting everything down.

The live watcher/WebSocket test also exercises a server interruption with both engines running, offline queue capture, reconnect, rename and deletion. All data stays in temporary directories; no Raspberry Pi or user project is used.

The optional desktop suite runs with `uv run --extra gui pytest -q tests/test_gui.py`. Its end-to-end test creates two real windows, registers two device identities against the fixture's Uvicorn server, starts independent daemons using GUI controls and checks continuous synchronization. Qt uses its offscreen platform for automation. The real macOS window also passed a native startup smoke check; Linux desktop rendering is not verified here. Without `--extra gui`, these nine tests skip if PySide6 is absent.

Packaging checks completed:

- `uv sync --locked` at the workspace root.
- `uv sync --locked --project pysync-server`, followed by `uv run --locked --no-sync python -c 'import websockets; from pysync_server.main import app; print("server-only runtime:", app.title, websockets.__version__)'` in the server directory: `server-only runtime: PySync 17.2`.
- `uv sync --locked --project pysync-client-cli`, followed by `uv run --locked --no-sync pysync-client-cli --help` in the client directory.
- Desktop `uv sync --locked --project pysync-client-gui`, followed by `uv run --locked --no-sync pysync-client-gui --help`; offscreen startup using the actual console script from the GUI package directory.
- Server isolation rechecked after GUI addition: `uv sync --locked --project pysync-server` imports the server successfully with no installed `PySide6` module.
- Full workspace including the desktop restored using `uv sync --locked --extra gui` after package isolation checks.
- Python compilation of all four packages and development scripts.
- Generation and parsing of the macOS launchd plist, plus generation of the systemd user unit. Service managers themselves were not invoked.

The server and clients use one root workspace lockfile; obsolete package-local lockfiles were removed. All three executable package console scripts are registered in their respective `pyproject.toml` files. The desktop is an optional root extra, keeping server-only deployments free of Qt.

## Public-repository preparation

Before the first publication, the staged 63-file manifest was exported into a clean temporary checkout. `uv sync --locked --extra gui` built all four workspace packages in a new environment, followed by the complete 56-test suite (25.96 seconds), lint and formatting checks. This verifies the committed sources without relying on the developer environment or local runtime data.

Local Markdown/image links were checked. The publication manifest was checked for exact local owner/device credentials and their encoded forms, credential/private-key patterns, machine-specific paths and runtime filenames. No findings were present. Runtime storage, private configuration, token files, logs, databases, virtual environments, generated service files and legacy watcher scratch files are excluded from Git. Only placeholder credentials and the fixed ephemeral test credential are included in examples/tests.

The CI workflow installs the committed lockfile and GUI extra and runs lint, formatting and all tests on Ubuntu 24.04 and macOS 15 using Python 3.12. External actions are pinned to commit hashes, and the workflow requests read-only repository contents permission. Live CI results are available on the repository's Actions page; Raspberry Pi hardware and service-manager deployment remain separate validation steps.
