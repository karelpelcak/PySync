# Installation and first synchronization

This guide takes you from a clean checkout to a running server and two registered clients. Use the **local demo** to try the application, or install a **persistent server** for your real projects. A Raspberry Pi acts as the central server; macOS/Linux computers run the clients.

## 1. Requirements

| Component | Requirements |
| --- | --- |
| Server | Linux or macOS, Python 3.12+, uv, enough disk space for current files and retained revisions |
| Raspberry Pi | 64-bit Raspberry Pi OS/Linux, Python 3.12+, preferably reliable external storage |
| CLI client | macOS or Linux, Python 3.12+, uv, read/write access to each project directory |
| Desktop GUI | The client requirements plus a graphical session and Qt runtime libraries; UI labels are currently Czech |
| Network | Clients can reach the central server; use HTTPS/private VPN for remote machines |

Windows and 32-bit Raspberry Pi installations are not supported. Keep all four packages and the root `uv.lock` together; the packages use a uv workspace and are not independently published to PyPI.

### Install Git and uv

On macOS, install the command-line developer tools if Git is missing:

```sh
xcode-select --install
```

If Homebrew is already installed, install uv with:

```sh
brew install uv
```

On Debian/Ubuntu/Raspberry Pi OS, install basic tools:

```sh
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

The official uv installer works on macOS and Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a new terminal after installation, then check `uv --version` and `git --version`. These installation methods are described in the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

### Clone the complete workspace

```sh
git clone https://github.com/karelpelcak/PySync.git
cd PySync
uv python install 3.12
```

`uv python install 3.12` installs a managed Python when necessary; you do not need to change the operating system's default Python. See [uv Python versions](https://docs.astral.sh/uv/concepts/python-versions/). Systemd server setup below has a separate interpreter-location requirement.

## 2. Try the disposable two-client demo

From the workspace root:

```sh
uv sync --locked
uv run python scripts/local_demo.py
```

The script starts one real backend and two independent daemons. It prints a `macbook` root, a `linux` root and the temporary state directory. Create a disposable file in either printed root and check the other. Changes, renames and deletions synchronize through the backend.

For an occupied port, run `uv run python scripts/local_demo.py --port 8010`. Stop with Ctrl-C. **The entire demo directory is removed when it stops. Do not place real work there.**

To view this demo in the GUI, keep the demo running, install the GUI extra in a second terminal and point it at the printed state directory's `macbook-state` subdirectory:

```sh
uv sync --locked --extra gui
uv run --extra gui pysync-client-gui --config-dir '/printed/demo/directory/macbook-state'
```

Substitute the actual printed directory. Registration is already performed by the demo; the GUI controls its existing daemon. Use the persistent setup for normal use.

## 3. Persistent local backend

For a local server in a terminal, work from the repository root:

```sh
uv sync --locked --project pysync-server
mkdir -p .pysync-data
chmod 700 .pysync-data
```

Generate a private owner token once. The following command fails rather than replacing an existing token:

```sh
(umask 077; .venv/bin/python -c 'import secrets; from pathlib import Path; p = Path(".pysync-data/owner-token"); p.open("x").write(secrets.token_urlsafe(32) + "\n")')
```

Start the backend using that token:

```sh
export PYSYNC_SERVER_OWNER_TOKEN="$(cat .pysync-data/owner-token)"
uv run --locked --project pysync-server pysync-server
```

It binds to `127.0.0.1:8000`. In another terminal, check readiness:

```sh
curl --fail http://127.0.0.1:8000/api/v1/health
# Expected: {"status":"ok","protocol_version":1}
```

Server data persists in `.pysync-data` relative to the working directory. Stop with Ctrl-C; reuse the same token and data directory on restart. Set `PYSYNC_SERVER_DATA_DIR` to an absolute private path for a service or a different working directory. API documentation is available at `http://127.0.0.1:8000/docs`.

The **owner token** is the server's administrative credential: it authorizes registering/revoking devices and archiving projects. Copy its value into the GUI's registration field or the CLI's hidden login prompt. Each registered device receives a separate token for routine synchronization. The GUI does not store the owner token, and the CLI only accepts it at registration. Local tokens and data are ignored by Git; keep an owner-token backup in your password manager.

Loopback is only reachable from this computer. For another computer, deploy the Linux/Raspberry Pi server and private HTTPS endpoint below.

## 4. macOS/Linux desktop client

On each computer, clone the complete repository as in section 1, then run from its root:

```sh
uv sync --locked --no-dev --extra gui
uv run --locked --no-dev --extra gui pysync-client-gui
```

Alternatively, from `pysync-client-gui`, run `uv sync --locked --no-dev` and `uv run --no-dev pysync-client-gui`; uv installs only the GUI and its client dependencies. If you return to the workspace root, include `--extra gui` when using uv so the desktop dependencies remain installed.

### Linux GUI prerequisites

Use a supported desktop distribution with a graphical login session. Qt needs OpenGL and its X11/xcb or Wayland platform libraries. Typical Ubuntu/Debian X11 runtime packages include:

```sh
sudo apt-get install -y libgl1 libegl1 libopengl0 libxkbcommon0 libxkbcommon-x11-0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-xinerama0 libxcb-xkb1
```

Package names vary by distribution. Consult the [Qt Linux requirements](https://doc.qt.io/qt-6/linux-requirements.html) if the GUI reports a missing platform plugin. On a headless machine, use the CLI instead. The test suite's offscreen platform is for automation, not an interactive desktop.

### Register and attach through the GUI

1. Open **Nastavení** or **Připojit zařízení** on the overview.
2. Enter the same reachable server origin on both clients, such as `https://raspberrypi.YOUR-TAILNET.ts.net`. Use `http://127.0.0.1:8000` only for a client on the local server's computer.
3. Choose a different device name on each computer, such as `macbook` and `linux`.
4. Paste the owner token into **Token vlastníka serveru**, then click **Připojit zařízení**. The field clears after successful registration; the private device credential is saved.
5. Click **Spustit synchronizaci**. The daemon now runs independently of the window.
6. On the first computer, choose **Připojit projekt**, select an existing project root, assign a project name and check **Nahrát také existující lokální soubory** if those contents should be uploaded. Review **Zkontrolovat změny**, then explicitly accept.
7. On the second computer, create an empty directory, choose the existing server project and review/accept its attachment. Files will download into that directory.
8. Create or edit a test file in the first project's root and confirm it appears on the second computer. Check **Přehled** for server connectivity and pending operations.

An existing CLI registration appears automatically. Closing the GUI leaves the daemon running. **Pozastavit synchronizaci** stops it safely; offline server connectivity still allows a running daemon to capture changes in its persistent queue. A daemon that is stopped cannot watch live events, but scans for changes when restarted.

Initial synchronization does not blindly delete pre-existing files. Without the initial-upload checkbox, initial local-only files stay local and differing contents are retained in `.pysync-recovery` before the server version is downloaded. Review collisions carefully. Do not configure overlapping project roots or two independent client configurations for the same root.

## 5. CLI-only client

For a smaller installation, enter the CLI package directory:

```sh
cd pysync-client-cli
uv sync --locked --no-dev
uv run --no-dev pysync-client-cli init --server-url https://raspberrypi.YOUR-TAILNET.ts.net
uv run --no-dev pysync-client-cli login --device-name macbook
uv run --no-dev pysync-client-cli daemon start
uv run --no-dev pysync-client-cli project add '/absolute/path/to/project' --name my-project --accept-local
uv run --no-dev pysync-client-cli status
```

Login prompts for the owner token without echoing it. For scripted registration, `PYSYNC_OWNER_TOKEN` is supported, but interactive input avoids storing it in shell history. Project attachment displays counts and differing paths, then requests acceptance. Do not use `--yes` until you have reviewed the plan.

On the second client, initialize the same URL, register a distinct name and attach an empty root:

```sh
uv run --no-dev pysync-client-cli init --server-url https://raspberrypi.YOUR-TAILNET.ts.net
uv run --no-dev pysync-client-cli login --device-name linux
uv run --no-dev pysync-client-cli daemon start
mkdir -p '/absolute/path/to/project'
uv run --no-dev pysync-client-cli project add '/absolute/path/to/project' --server-project my-project
```

If a configuration already exists, skip `init`; if already registered, skip `login`. A different server identity requires a fresh configuration directory. Pass `--config-dir '/absolute/path/to/state'` before the command or set `PYSYNC_CONFIG_DIR`.

## 6. Raspberry Pi/Linux server with systemd

Use a 64-bit Linux installation. The following layout matches the supplied service:

| Path | Contents |
| --- | --- |
| `/opt/pysync` | Repository and virtual environment |
| `/opt/pysync-python` | Optional managed Python outside a user home |
| `/etc/pysync/server.env` | Root-owned private owner credential and configuration |
| `/var/lib/pysync` | Service-owned SQLite metadata and content blobs |

### Install code and Python

With Git and uv already available, create a clean installation directory and clone:

```sh
sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" /opt/pysync
git clone https://github.com/karelpelcak/PySync.git /opt/pysync
cd /opt/pysync
```

The unit uses `ProtectHome=true`, so its interpreter must be outside `/home`. If `/usr/bin/python3` is already Python 3.12+, use it explicitly:

```sh
/usr/bin/python3 --version
uv sync --locked --no-dev --project pysync-server --python /usr/bin/python3
```

If the operating system Python is older, install managed Python into `/opt` instead:

```sh
sudo "$(command -v uv)" python install 3.12 --install-dir /opt/pysync-python --no-bin
UV_PYTHON_INSTALL_DIR=/opt/pysync-python uv sync --locked --no-dev --project pysync-server --python 3.12
```

Check the resolved interpreter path:

```sh
.venv/bin/python -c 'import sys; from pathlib import Path; print(Path(sys.executable).resolve())'
```

It must be executable by the service account and must not point into a protected home directory. Keep the same `--python`/`UV_PYTHON_INSTALL_DIR` choice for later updates. No Qt dependencies are installed for the server.

### Create service account and configuration

```sh
sudo useradd --system --home-dir /var/lib/pysync --shell /usr/sbin/nologin pysync
sudo install -d -m 0700 /etc/pysync
sudo install -m 0600 docs/server.env.example /etc/pysync/server.env
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
sudoedit /etc/pysync/server.env
```

Copy the generated secret into `PYSYNC_SERVER_OWNER_TOKEN`, replacing the placeholder; save it securely for device registration. Do not overwrite an existing `server.env` during an upgrade. The template also sets file-size/storage limits and upload timeout. The server stores this owner credential in the environment; device tokens are hashed in its metadata database.

### Enable and verify

```sh
sudo install -m 0644 docs/pysync-server.service /etc/systemd/system/pysync-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now pysync-server
sudo systemctl status pysync-server
curl --fail http://127.0.0.1:8000/api/v1/health
```

`StateDirectory=pysync` creates `/var/lib/pysync` with service ownership and private permissions. Run exactly one process per server data directory, with no multiple Uvicorn workers. To inspect logs or stop:

```sh
journalctl -u pysync-server -f
sudo systemctl stop pysync-server
```

## 7. Reach the server securely from other computers

Install/authenticate [Tailscale](https://tailscale.com/download) on the server and both clients. Keep PySync bound to loopback, then enable a private HTTPS proxy on the server:

```sh
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status
```

Use the HTTPS URL printed by Serve on **both** clients. Serve is private to your tailnet; restrict access to your devices and do not enable Funnel. Consult the [Serve CLI documentation](https://tailscale.com/docs/reference/tailscale-cli/serve) for HTTPS prerequisites and configuration.

Clients still need the PySync owner token to register and their issued device tokens for file/WebSocket access. HTTPS maps to WSS automatically, and tokens are never sent in WebSocket query parameters. A private reverse proxy with WebSocket upgrades is also supported; direct Uvicorn TLS uses `PYSYNC_SERVER_TLS_CERT`/`PYSYNC_SERVER_TLS_KEY`.

Plain HTTP outside loopback requires an explicit trusted-network opt-in (`init --allow-insecure` or the GUI checkbox). Never expose token-bearing HTTP/WS to the public internet. PySync is designed for trusted private-network use, not a hardened public multi-tenant service.

## 8. Start clients automatically after login

After a client is registered, return to the repository root. Generate reviewable service files with the installed interpreter; this command does not change the environment or install a service:

```sh
.venv/bin/python scripts/service_files.py --output-dir ./service-files
```

For a custom client state directory, add `--config-dir '/absolute/path/to/state'`. Stop the detached daemon if it is running:

```sh
.venv/bin/pysync-client-cli daemon stop
```

The generated files include the actual executable and configuration paths. Regenerate if you move the checkout/virtual environment. The root `service-files/` directory is ignored by Git because it contains machine-specific paths.

### macOS launchd

```sh
mkdir -p ~/Library/LaunchAgents
cp service-files/dev.pysync.client.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/dev.pysync.client.plist
launchctl kickstart "gui/$(id -u)/dev.pysync.client"
launchctl print "gui/$(id -u)/dev.pysync.client"
```

To stop/remove the service before changing configuration:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/dev.pysync.client.plist
```

macOS may require filesystem access for the Python interpreter if you synchronize protected user folders. Keep the checkout in a stable accessible location.

### Linux systemd user service

```sh
mkdir -p ~/.config/systemd/user
cp service-files/pysync-client.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pysync-client
systemctl --user status pysync-client
```

Use `systemctl --user stop pysync-client` and `systemctl --user start pysync-client` once systemd owns the daemon. Optional user lingering allows execution after logout; see [deployment](docs/deployment.md).

Stop/unload a manager-owned service before changing settings. The GUI directly controls the daemon process and does not install or administer launchd/systemd. Its close button does not stop the service.

## 9. Configuration, updates and removal

Client configuration lives at `~/Library/Application Support/pysync` on macOS or typically `~/.config/pysync` on Linux. Files include private `config.json`, `state.sqlite`, durable spool data, `daemon.log` and the live Unix socket. Credentials require file mode `0600`, and the directory should remain private. GUI settings edit global ignore patterns, debounce, polling and maximum file size while stopped. Project-specific patterns are entered on attachment. See [example configuration](docs/client.config.example.json).

To update, stop the relevant service, back up server metadata/blobs or client state/project roots, then `git pull --ff-only` and repeat the appropriate `uv sync --locked` command for that role. Preserve the owner token, device identity, database and queues. Startup migrations run automatically and reject databases from newer versions; do not downgrade without a matching backup. Recreate service files only if paths changed.

To stop using a client, stop/unload its service and detach projects with `project remove NAME`. Local working files and server history remain intact. Pending operations must finish before ordinary removal. Preserve queue/recovery data before uninstalling the environment or checkout; there is no automatic destructive data-removal command.

## 10. Troubleshooting

| Symptom | What to check |
| --- | --- |
| `uv: command not found` | Open a new terminal after installation; check the path printed by the uv installer. |
| Server refuses startup | Owner token must be at least 24 characters; another server must not own the same data directory. |
| Port 8000 is occupied | Stop the identified PySync process or set `PYSYNC_SERVER_PORT` and match the client URL. |
| GUI cannot reach the server | Use the reachable HTTPS origin, not `localhost` on another computer; verify VPN, service status and health. |
| Registration returns 401 | Use the owner token from this server; a device token cannot register another device. |
| GUI cannot load a Qt platform plugin | Install the distribution's Qt runtime dependencies and use a graphical session. |
| Daemon is not running | Register first, then start it; inspect `startup.log` and `daemon.log` in client state. |
| Credentials are too permissive | Restore `chmod 600` on `config.json` and private access to the state directory. |
| Root identity changed | Restore the original mounted root or deliberately reattach after preserving all unsent work. |
| Conflict appeared | Open the preserved copy, merge manually into the original, allow synchronization, then mark resolved. |
| macOS/Linux service cannot start Python | Check its absolute executable/interpreter paths and permissions; server `ProtectHome` blocks home-directory Python. |

For disk-space errors, checksum failures, ignore changes and recovery behavior, read [operations](docs/operations.md). Include only sanitized diagnostics in public issue reports.
