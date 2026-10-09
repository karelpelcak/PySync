# Operations, limitations and troubleshooting

## Configuration

Configuration is deliberately JSON rather than TOML so the CLI can update credentials without an additional TOML-writing dependency. `init` creates it and `login` fills in the device identity. See `client.config.example.json`. Never commit the resulting credential file.

The normal state directory is `~/Library/Application Support/pysync` on macOS and the platform configuration directory (usually `~/.config/pysync`) on Linux. It contains `config.json`, `state.sqlite` and its WAL, `spool/`, daemon/service logs, the private Unix socket, and PID/lock files. `PYSYNC_CONFIG_DIR` or the global CLI `--config-dir` option selects another directory. Use a reasonably short path: Unix-domain socket pathname limits apply (about 100 bytes on macOS).

Stop the daemon before changing `config.json`; keep mode 0600 on that file and mode 0700 on the state directory. Do not change a registered `device_id` or `token` while queued work exists. To register another identity, use a separate state directory. Field names and ranges are validated by Pydantic Settings; `PYSYNC_CLIENT_*` variables supply defaults for settings not supplied by the saved configuration.

Global ignore rules are `ignore` in `config.json`. Additional per-project rules can be supplied repeatedly during attachment:

```sh
uv run pysync-client-cli project add ./my-project --ignore 'build/' --ignore '*.log' --accept-local
```

Rules use gitignore-style path matching, including negation, but PySync always reserves internal `.pysync-*` paths. Ignored directories are pruned during traversal; a negation cannot reinclude a child of a pruned directory unless its ancestors are also included. Configuration changes never generate deletion operations for ignored files. Previously queued operations that become ignored are retained but paused; status shows their count, and other project files continue syncing. Unignore the path to resume, or explicitly detach with `--discard-pending` after preserving all unsent snapshots. A manifest refresh when ignore rules change retrieves remote changes skipped while a file was ignored; differing local changes still use optimistic conflict handling. Per-project ignore rules currently require detaching and reattaching the directory to change through the CLI. Detaching never removes local files.

## Conflicts

```sh
uv run pysync-client-cli conflicts
```

Server conflict copies have names such as `main.conflict-12ab34cd-r152-89ef0123.py`. They use device IDs rather than mutable user-supplied names and server revisions rather than client clocks. Both original and conflict copy synchronize normally. Choose how to merge them, edit the original, wait for successful sync, and then mark the conflict resolved:

```sh
uv run pysync-client-cli sync my-project
uv run pysync-client-cli conflicts --resolve CONFLICT_UUID
```

Resolving the record does not remove either file. Remove an unwanted conflict copy manually after merging; its deletion then propagates normally. Stale deletion conflicts may have no extra content copy because the accepted content is already preserved at the original path. Local initial or interrupted-write conflicts show an absolute `.pysync-recovery` location and are local records. Resolving those records also retains their files.

## Filesystem policy

- Only regular files are synchronized. Symlinks, sockets, FIFOs, devices and other special files produce scan diagnostics and are never opened as content. Ignore them explicitly when they are intentionally present in a project.
- Paths are normalized to NFC Unicode, use `/`, and cannot contain traversal components, control characters, backslashes, portable unsafe filename characters, trailing spaces/dots or reserved internal names. A path is limited to 1024 UTF-8 bytes, each component to 240 bytes.
- Case-folded aliases and inconsistent parent-directory case are rejected so projects can be shared across normal macOS and Linux filesystems. If an existing local tree differs only in case from a remote path, the daemon pauses that operation and reports the collision; fix it manually without discarding either content. Reliable case-only file renames use deletion followed by creation.
- Only the executable flag is transferred, using the owner's executable bit. Remote non-executable files use 0644, executable files use 0755, subject to the daemon's umask. Ownership, ACLs, xattrs, resource forks, hard-link identity, full POSIX mode, modification timestamps and directory metadata are not reproduced.
- Empty directories are not synchronized. Directories left empty after deletion are retained. Converting a previously synchronized directory into a file may therefore require removing that empty local directory manually on receiving devices. File renames are a delete/create pair rather than a multi-path atomic transaction.
- Remote content is published as a complete inode, after retaining the previous inode. There is a small missing-path interval during replacement. Watcher events from remote writes are reconciled through hashes and durable state, without a fixed ignore window.
- A replaced or unmounted root is treated as an error, not a request to delete every file. Stop the daemon and reattach a moved root deliberately; preserve the original state/snapshots while investigating.

`.pysync-recovery` retains previous file contents on replacements and deletions. Recovery files use opaque UUID names; conflict records associate unexpected/initial recoveries with original paths. Routine recovery copies are a safety buffer, not an indexed restoration UI. They grow over time and are not synchronized or automatically pruned. To find a routine copy, inspect its content/hash. Remove old recoveries manually only after verifying synchronization and backups. An editor keeping an old inode open can continue writing to its recovery copy; in-place writes after a remote replacement are preserved there but cannot always be attributed automatically. PySync cannot prevent arbitrary external processes from modifying project trees or recovery directories.

## Diagnostics

`status` shows device identity, connectivity, applied project revisions, successful sync time, queue counts, transfer byte progress and errors. Query it during a large transfer from another terminal. Conflict counts and individual records are available through `conflicts`. `logs` prints recent JSON operational events. `startup.log` contains failures that occurred before the daemon became available; systemd/launchd have additional stdout/stderr logs.

| Symptom | Action |
| --- | --- |
| Server unreachable | Confirm health, URL, VPN, TLS and firewall. Queue snapshots remain durable; reconnection is automatic. |
| 401 | Confirm the correct device configuration/token, and that the owner has not revoked it. Never reset SQLite to fix authentication. |
| 403 | Join the server project through `project add --server-project NAME`. |
| 409 filename collision | Inspect differing case or file/directory prefixes. Preserve both local files and correct the naming manually. If an already queued path remains blocked, back up its spool snapshot, detach using `--discard-pending`, then reattach the corrected root; operations are never silently rewritten. |
| 413 size limit | Increase the server/client file-size setting deliberately or ignore that file. |
| 507 storage unavailable | Check server disk space/permissions/quota and logs. Completed metadata is retained; retries reuse operation IDs. |
| Local disk full | Free space outside queue/recovery data. Incomplete snapshots are discarded; complete queued snapshots remain. |
| Root identity changed | Confirm a volume is mounted or a root was moved. Reattach intentionally after preserving unsent state. |
| Credentials too permissive | `chmod 600 config.json`; keep the state directory private. |
| Watch limits exceeded | Periodic scans still run. Increase Linux inotify watch limits or reduce the synchronized tree. |
| Daemon already owns state | Use `daemon status`; stop the previous instance. The OS releases its lock after a crash. |
| Checksum failure | Investigate proxy/disk corruption. Local content remains intact; corrupt downloads are not applied. |
| Missing committed server blob | Restore a consistent server backup. Startup refuses to serve incomplete committed history. |

## Current limitations

This implementation is intended for a single owner and trusted devices on macOS/Linux. It is not a production-hardened public multi-tenant service. Known limitations are:

1. Full-file transfers only, with one serialized reconciler per daemon and up to four server uploads by default. No delta transfer, resumable chunks, or compression. Large interrupted transfers restart from the beginning.
2. Complete periodic scans hash regular-file content; unchanged files do not write spool snapshots. Very large trees need appropriate ignore rules and a longer `poll_seconds`; there is no large-scale performance benchmark or stat/hash index. Manifests are unpaginated, and server portable-collision checking reads current file metadata per mutation. The append-only change journal is paginated.
3. History, operation records, tombstones, orphan server blobs and local recovery copies have no automatic retention/GC. Total server blob storage is capped, but local snapshot/recovery data has no configurable quota. Monitor free space. Log files have no automatic rotation.
4. Renames use safe delete/create operations and may temporarily show both paths, a missing path, or a conflict-preserved old path. Directory metadata and empty directories are unsupported; directory-to-file conversions can require manual removal of empty directories.
5. Conflict merge is manual. The native desktop GUI lists conflicts and opens preserved-copy locations, but has no merge editor. There is no revision-browser/restore CLI, keychain integration, per-device project ACL administration, or public-endpoint rate limiting.
6. Client status reports the local daemon's projects and transfers; it does not administer other devices. `sync NAME` requests that project's reconciliation while continuous background synchronization of other projects continues normally.
7. Service files are generated and documented, but not automatically installed by the CLI. Automatic login startup requires the documented systemd or launchd setup.
8. Tests were executed on macOS with local simulated device directories and real networking/processes. Linux/Raspberry Pi deployment is supported by the implementation and templates but requires validation on those actual hosts. Unix sockets, descriptor-relative I/O and POSIX process locks exclude Windows.
9. Synchronization preserves recoverable content but cannot guarantee every transient intermediate editor write becomes a committed version. In-place writes to an inode already moved into recovery remain in recovery. Use filesystem backups for protection from external modification, hardware failure, or user deletion of recovery data.
10. The optional GUI requires a macOS/Linux desktop and Qt libraries; a headless server does not install them by default. There is no `.app`/installer or tray integration. Registering a different server identity uses a separate configuration; the GUI locks the registered server URL to protect existing project state. Per-project ignore patterns are configured at attachment; changing them requires detaching and reviewing reattachment after pending work finishes. Transfer progress is sampled every two seconds, so very short transfers may complete between refreshes.

When restoring an older server snapshot, its revision must never silently roll back a live client's cursor or base revisions. PySync pauses a project if the server is behind the client's known state. Preserve both roots and client state, then restore matching snapshots or attach a fresh empty root to the restored project and manually reconcile preserved local files.
