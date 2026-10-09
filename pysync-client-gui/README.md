# PySync desktop

A native macOS/Linux desktop client written with PySide6 Widgets. It controls the same daemon as `pysync-client-cli`; opening another window does not create another synchronization engine or watchers. The interface is in Czech.

## Start

From the repository root:

```sh
uv sync --locked --extra gui
uv run --extra gui pysync-client-gui
```

Or from this package directory:

```sh
uv sync --locked
uv run pysync-client-gui
```

An existing CLI registration and its projects appear automatically. To use a separate configuration, pass `--config-dir '/absolute/path/to/state'` or set `PYSYNC_CONFIG_DIR`. Do not use two configurations for the same working directory. A desktop session is required; run the CLI alone on a headless Raspberry Pi.

## First connection

1. Start your [PySync server](../docs/deployment.md), retaining its owner token securely.
2. Open **Nastavení** or choose **Připojit zařízení** on the overview.
3. Enter the HTTPS server origin, a device name, and the owner token. Loopback HTTP works for local development; remote HTTP requires an explicit trusted-network opt-in.
4. Click **Připojit zařízení**. Failed validation or authentication leaves the entered values available to correct and retry. Successful registration clears the owner-token field and stores only the device credential in the private client configuration.
5. Click **Spustit synchronizaci** in the sidebar.
6. Choose **Připojit projekt**, select an existing local directory and a server project or new name, then click **Zkontrolovat změny**. Review the counts and differing paths before **Připojit a synchronizovat**.

An empty directory is the simplest way to join an existing project. **Nahrát také existující lokální soubory** explicitly accepts initial uploads. Without it, initial local-only files stay local and differing contents are preserved in `.pysync-recovery` before downloading the server version. The plan can create/join a server project for inspection; it does not attach the local root until accepted. Changes between preview and acceptance still go through optimistic conflict handling and preservation.

## Daily use

- **Přehled**: daemon/server connectivity, project count, durable queued operations, unresolved conflicts, last successful synchronization and sampled file-transfer progress. Offline changes continue to queue in the daemon.
- **Projekty**: local roots, per-project errors and revision, open a folder, request synchronization or detach a project. Detachment retains files; the GUI refuses to discard pending work.
- **Konflikty**: open the preserved copy's directory, merge content manually, and confirm resolution. Marking resolved retains every copy.
- **Logy**: the last 200 daemon log entries, refreshed while this page is open.
- **Nastavení**: edit debounce, polling, maximum size and global ignore patterns while synchronization is paused. Save explicitly; refresh does not overwrite unfinished form edits. Registered identities remain bound to their server. Custom per-project ignores are entered when attaching a project.

Closing the window leaves the daemon running. **Pozastavit synchronizaci** requests a clean daemon shutdown without deleting queues or recovery copies. Closing while an operation is running waits for that operation to finish. If launchd/systemd manages the daemon, use that service manager to stop it before changing settings: its restart policy may start it again. Automatic startup after login is configured with the existing [service templates](../docs/deployment.md), not installed by the GUI.

## Architecture and verification

`backend.py` shares configuration, registration and daemon lifecycle code with the CLI and uses private IPC for project/sync/conflict actions. Stopped diagnostics are read from SQLite in read-only mode. It never constructs an engine. `window.py` renders actual status; `dialogs.py` implements reviewed onboarding; `workers.py` runs requests off the UI thread and delivers results through Qt signals. `theme.py` and `widgets.py` contain the visual style and reusable widgets. Forms use labeled fields, keyboard focus and a concealed token input.

```sh
uv run --extra gui pytest -q tests/test_gui.py
uv run --extra gui pytest -q
uv run --extra gui ruff check .
uv run --extra gui ruff format --check .
```

The desktop suite uses offscreen Qt widgets and temporary roots. It includes GUI-driven owner registration, starting two independent daemon subprocesses, reviewing and attaching projects, preserved pre-existing local content, live Unicode file creation/modification/rename/deletion, log display and daemon restart. Desktop tests skip when the optional Qt package is absent. Native Linux rendering and service-manager installation still require validation on their actual platforms.

There is no packaged `.app`/installer, tray icon, graphical conflict editor, automatic service installer, device administration or server-identity migration wizard in this version. Credentials use mode-0600 files rather than the OS keychain. See the [operational limitations](../docs/operations.md).
