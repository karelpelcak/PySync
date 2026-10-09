# Deployment

For a clean checkout, dependencies, first registration and the complete two-device walkthrough, start with the [installation guide](../INSTALLATION.md). This page describes service deployment, transport and backups in more detail.

## Raspberry Pi or Linux server

Use a 64-bit Raspberry Pi OS or ordinary Linux host with Python 3.12+ and uv. A reliable SSD is preferable to relying on a heavily written SD card. Install the complete repository into `/opt/pysync` (including the shared package), with a stable system Python or uv-managed Python accessible to the service account.

```sh
cd /opt/pysync
uv sync --locked --no-dev --project pysync-server
```

The sample systemd service enables `ProtectHome=true`, so its Python interpreter must **not** reside in a user's home directory. If uv selected a home-directory interpreter, recreate the environment using a system Python 3.12+:

```sh
uv sync --locked --no-dev --project pysync-server --python /usr/bin/python3.12
```

Use your installed system interpreter's actual path (for example `/usr/bin/python3.13`). Check `.venv/bin/python` before enabling the service. A symlink to a Python interpreter under `/home/...` will not work with this unit's protections.

Create a dedicated service user and configure the owner credential:

```sh
sudo useradd --system --home-dir /var/lib/pysync --shell /usr/sbin/nologin pysync
sudo install -d -m 0700 /etc/pysync
sudo install -m 0600 docs/server.env.example /etc/pysync/server.env
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
sudoedit /etc/pysync/server.env
```

Replace the example token with the generated secret. Keep it in a password manager: it grants device registration, revocation, and project archival. Do not commit it. The systemd manager reads the root-owned environment file. The service user needs read/execute access to `/opt/pysync` and its interpreter, and write access only to `/var/lib/pysync` and its private temporary directory.

```sh
sudo install -m 0644 docs/pysync-server.service /etc/systemd/system/pysync-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now pysync-server
sudo systemctl status pysync-server
journalctl -u pysync-server -f
curl http://127.0.0.1:8000/api/v1/health
```

`StateDirectory=pysync` creates the private persistent directory with the correct ownership. The sample unit restarts after failures, sets a private umask, and makes the rest of the filesystem read-only. Those features are documented by [systemd](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml). Start exactly **one** server process per data directory: revision commits are serialized within that process, and a process lock rejects a second instance. Do not enable Uvicorn's multiple workers.

## Remote access with HTTPS inside Tailscale

Install and authenticate Tailscale on the server and both clients according to its platform documentation. Keep PySync bound to loopback on the server. Use Serve to proxy the loopback application over private HTTPS:

```sh
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status
```

Use the HTTPS URL printed by Tailscale, such as `https://raspberrypi.tail12345.ts.net`, in both clients. Serve provides private access within the tailnet; HTTPS provisioning may require enabling it in your tailnet. Follow the official [Serve setup](https://tailscale.com/docs/features/tailscale-serve) and [CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve). Restrict tailnet access to your own devices. Do not enable Funnel for this service.

PySync still requires its own device bearer token for HTTP and WebSocket access. Tailscale access alone does not register a PySync device. HTTPS automatically maps to WSS in the client. Neither client nor server puts authentication tokens into WebSocket URLs.

You can also terminate TLS with a private reverse proxy that supports WebSocket upgrades, or set `PYSYNC_SERVER_TLS_CERT` and `PYSYNC_SERVER_TLS_KEY` for direct Uvicorn TLS. The server supports Uvicorn's documented [TLS settings](https://www.uvicorn.org/settings/). Certificate verification is enabled in the client. For a private CA, provide a valid trust store through `SSL_CERT_FILE`; there is no `--skip-verify` shortcut.

For an explicitly trusted VPN or local network, `init --allow-insecure --server-url http://...` enables plaintext transport. Ensure the VPN encrypts the entire connection. **Never expose token-bearing HTTP or WS to the public internet.** PySync is designed for trusted devices behind private network access and has no public-service abuse-rate protection.

## macOS and Linux clients

Install the whole workspace on each machine in a stable path, then register a different device identity on each:

```sh
uv sync --locked
uv run pysync-client-cli init --server-url https://raspberrypi.YOUR-TAILNET.ts.net
uv run pysync-client-cli login --device-name macbook
uv run pysync-client-cli daemon start
uv run pysync-client-cli project add '/path with spaces/my-project' --name my-project --accept-local
```

On the second machine, use another device name and an empty directory:

```sh
uv run pysync-client-cli login --device-name linux
uv run pysync-client-cli daemon start
mkdir -p '/path/to/my-project'
uv run pysync-client-cli project add '/path/to/my-project' --server-project my-project
```

Run `init` with the same remote HTTPS URL before `login` on the second machine as well. macOS may prompt for filesystem access if a root is in a protected user folder; grant access to the Python interpreter/service or choose a normal developer directory. Linux requires read/write permission on each project root. No root privileges are required for a client.

For graphical setup instead, run `uv sync --locked --extra gui` and `uv run --extra gui pysync-client-gui` at the repository root on each desktop. Register through **Nastavení**, start synchronization and attach the local root after reviewing the displayed plan. Existing CLI identities/projects are recognized. Linux needs a graphical desktop and the normal Qt platform runtime libraries supplied by the distribution (Wayland or X11/xcb); headless machines should use the CLI. See the [desktop guide](../pysync-client-gui/README.md).

`daemon start` runs detached from your terminal but does not itself install automatic login startup. Service-manager setup below provides that. Stop the detached daemon before enabling a service.

The GUI's start/pause controls manage the daemon process directly. When a service manager owns that process, stop/unload the service through systemd/launchd before saving configuration, since its restart policy may otherwise restart a paused daemon. Closing the GUI does not stop synchronization.

## Generate automatic-start service files

```sh
uv run python scripts/service_files.py --output-dir ./service-files
uv run pysync-client-cli daemon stop
```

The generator embeds the actual virtual-environment executable and platform configuration directory. If you use custom state, include `--config-dir /absolute/path/to/state`. The generated files are reviewable and do not automatically modify the operating system. Regenerate them if you move the repository or virtual environment. Do not move an active synchronized root: root identity checks deliberately pause synchronization if it is replaced or a volume is unmounted.

### Linux systemd user service

```sh
mkdir -p ~/.config/systemd/user
cp service-files/pysync-client.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pysync-client
systemctl --user status pysync-client
journalctl --user -u pysync-client -f
```

This starts after login. To keep it running after logout, optionally enable lingering using `loginctl enable-linger USERNAME` with the permissions your host requires. Stop/start using the service manager once installed:

```sh
systemctl --user stop pysync-client
systemctl --user start pysync-client
```

### macOS launchd

```sh
mkdir -p ~/Library/LaunchAgents
cp service-files/dev.pysync.client.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/dev.pysync.client.plist
launchctl kickstart "gui/$(id -u)/dev.pysync.client"
launchctl print "gui/$(id -u)/dev.pysync.client"
```

Unload it before changing credentials or removing the service:

```sh
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/dev.pysync.client.plist
```

The launchd file starts after login, restarts failures, and records service stdout/stderr in the private client state directory. `daemon run` writes JSON operational logs to `daemon.log`.

## Updates and backups

Stop the service, back up the entire server data directory (including SQLite WAL files if present and `blobs`), update code, run `uv sync --locked`, then restart. Server and client schema migrations run at startup and refuse databases from newer application versions. Do not downgrade without restoring a matching backup.

Back up each client configuration/state directory together with its project directories, including `.pysync-recovery`. Client snapshots can contain unsent work. Never delete a client SQLite database or spool as a troubleshooting shortcut. Device credentials must stay paired with their local queues.

The SQLite metadata and content blobs form one logical backup. The simplest consistent backup is taken while the server is stopped. Restoring an older server backup rolls revisions back: clients detect a server revision behind their stored state and pause; restore matching client state or reattach to a fresh empty directory while retaining the original local roots and snapshots.
