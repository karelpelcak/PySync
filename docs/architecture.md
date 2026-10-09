# Protocol, persistence and recovery

## Revision semantics

A project is a UUID and an increasing integer revision counter. The counter advances once for each accepted content/tombstone mutation, including conflict copies. File state is keyed by `(project_id, normalized_relative_path)` and stores SHA-256, byte size, informational mtime, executable flag, revision, deletion flag, and originating device.

Each mutation contains an operation UUID and the base **file** revision on which the local edit was made. A new file uses base `0`. A tombstone still has a revision, so creating a replacement after learning about a deletion uses that tombstone revision. This avoids resurrection from stale offline state. Hash-equivalent content with the same executable flag is a no-op and can converge without making another revision.

The server commits a mutation only when its base matches the latest file state. A stale different put writes to a deterministic conflict filename based on the server's revision, the device ID, and operation ID. The accepted original stays available. A stale delete records a conflict and retains the accepted content. Both conflicts and operation responses commit in the same transaction as the change journal. Server clocks only timestamp audit records, and never determine the winner of a conflict.

## HTTP API

The FastAPI `/docs` endpoint describes validated parameters. All project/file endpoints require `Authorization: Bearer DEVICE_TOKEN`, except archival, which requires the owner token. Owner-issued devices are trusted to discover all active projects and explicitly join them. File APIs require a project membership, providing an authorization boundary that can later support stronger owner-specific ACLs.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Unauthenticated readiness/protocol version |
| POST | `/api/v1/devices/register` | Owner token → device UUID and one-time device token |
| GET | `/api/v1/devices` | Authenticated device list/presence |
| DELETE | `/api/v1/devices/{uuid}` | Owner-only token revocation; history retained |
| POST / GET | `/api/v1/projects` | Create / discover projects |
| POST | `/api/v1/projects/{uuid}/join` | Join as a trusted device |
| GET | `/api/v1/projects/{uuid}` | Project revision/name |
| DELETE | `/api/v1/projects/{uuid}` | Owner-only soft archive, without deleting client files |
| GET | `/api/v1/projects/{uuid}/manifest` | Consistent latest file state, including tombstones |
| GET | `/api/v1/projects/{uuid}/changes?after=REV&limit=500` | Ordered immutable journal page (maximum 1000) |
| POST | `/api/v1/projects/{uuid}/files/upload` | Raw streamed body and operation metadata header |
| GET | `/api/v1/projects/{uuid}/files/download?revision=REV` | Authenticated immutable revision download |
| GET | `/api/v1/projects/{uuid}/conflicts` | Unresolved server conflicts |
| POST | `/api/v1/projects/{uuid}/conflicts/{uuid}/resolve` | Mark a manually handled conflict resolved |
| WS | `/ws` | Authenticated notifications and control |

Upload metadata is compact JSON in `X-PySync-Operation` (maximum 8192 header characters):

```json
{
  "operation_id": "3b7d0dfa-95bd-4575-83f0-52f5a54695bf",
  "path": "src/main.py",
  "base_revision": 17,
  "operation": "put",
  "hash": "SHA256_HEX_DIGEST",
  "size": 512,
  "mtime": 1791561600.0,
  "executable": false,
  "moved_from": null
}
```

SHA-256 must be 64 lowercase hex characters. Non-ASCII metadata is JSON-escaped for safe HTTP headers. Deletes use the same endpoint with `operation=delete` and an empty body. Maximum file size defaults to 100 MiB; total immutable storage defaults to 20 GiB. An upload must complete within the configured `PYSYNC_SERVER_UPLOAD_TIMEOUT` (300 seconds by default), otherwise the uncommitted staging data is discarded and the same operation can be retried. Management JSON bodies are capped at 16 KiB before deserialization. Server metadata errors produce 4xx responses; filesystem failures produce a 507 response and structured server logs. Uploads and downloads use streamed content rather than whole-file memory buffers. The response contains the accepted file metadata, current project revision, and optional conflict metadata.

A completed operation UUID is globally unique. Reusing it with identical device, project and request metadata returns its stored response. Reusing it for different metadata returns 409. If a client loses the response to an accepted operation, it retries its durable snapshot with the same UUID. There is no second commit or duplicate conflict copy.

## WebSockets

Authenticate using the HTTP `Authorization` header during the handshake. Tokens are never URL query parameters. Shared Pydantic models define `sync_status`, `presence`, `file_changed`, `file_deleted`, `file_moved`, `conflict`, `heartbeat`, `sync_request`, `ack`, and `error`. Presence is advisory. Project notifications go only to members. A bounded queue disconnects slow receivers instead of consuming unbounded memory; reconnect restores state through the journal.

```json
{
  "type": "file_changed",
  "project_id": "project-uuid",
  "revision": 152,
  "path": "src/main.py",
  "operation": "put",
  "origin_device_id": "device-uuid",
  "moved_from": null
}
```

WebSocket events only wake the reconciler. It downloads HTTP journal records and immutable content by revision, then acknowledges its durable applied project cursor. HTTP polling continues if the WebSocket connection is unavailable. Both HTTP connectivity probes and WebSocket reconnection use bounded exponential backoff with jitter. Automatic retry needs no CLI intervention.

## Server persistence

SQLAlchemy async SQLite tables are devices, projects, memberships, file metadata, immutable file revisions/journal entries, conflicts, completed transfer operations, and schema migration records. SQLite has WAL, foreign keys, a busy timeout, and full synchronous commits. The single server process serializes writes and rejects a second process using the same data directory.

Uploads stream into unique staging files. Size and SHA-256 are checked; file data is flushed and fsynced. The accepted content is moved to a SHA-256-addressed immutable blob and the blob directory is fsynced **before** database commit. A failure before database commit leaves an unreferenced blob, never a database reference to an incomplete upload. Startup removes abandoned staging files, applies sequential migrations, and refuses to start if committed history refers to missing content. Hashes are verified by clients when downloaded; this does not replace disk-integrity monitoring.

Committed blob data is not edited in place. Old revisions are downloadable through the authenticated membership boundary. Archived projects, operations, revisions and tombstones are retained. No automatic history or orphan-blob garbage collection is implemented in this version.

## Client persistence and ordering

The local SQLite schema contains projects, per-file baselines, pending operations, journal cursors, conflicts, scan errors, and remote-application intents. Sequential `PRAGMA user_version` migrations are transactionally applied at startup.

A watchdog event wakes a debounced scanner. Watchers are attached to non-ignored directories without recursively subscribing to generated trees. A periodic full scan catches missed events, newly created directories, and changes while disconnected. Hashing reads regular files through descriptor-relative, no-follow operations and checks inode, mtime, ctime and size around reads. Unchanged files avoid writing snapshot data. A changed file is reread into a fsynced immutable spool snapshot before its operation row commits. Pending operations survive process restarts. Unreferenced spool files are removed at startup; referenced missing snapshots cause an explicit error.

One pending operation per path is retained with its original UUID and base. Later local edits remain in the working file and are captured after the previous queued snapshot completes. On conflict, their base stays the original base until the accepted remote file is applied, preventing a newer offline edit from accidentally overwriting the remote winner. Deletions are submitted before puts so case-only renames and file/directory transitions can make progress. Reliable watchdog moves attach `moved_from` metadata; moves always use a safe, independently revision-checked delete/create pair. Directory moves fall back to recursive comparison.

The reconciler verifies the identity of each configured root (device number and inode) before interpreting missing files as deletions. A disappeared, replaced or unmounted root pauses the project. Incomplete scans and any file access errors disable deletion inference for that pass. Ignored files are never inferred as deletions. Each startup, and each ignore-rule change, refreshes a manifest after local pending work is handled to recover changes that were skipped while ignored.

## Remote publication and crash recovery

The client verifies downloaded size/hash, writes a durable temporary source, and records a remote-application intent in SQLite before touching the working path. A destination-directory temporary file is fully written and fsynced. The previous regular file is atomically moved into `.pysync-recovery`; the new file is published with an exclusive hard link to the temporary inode. The directory is fsynced before recording file state and clearing the application intent.

Each published file is complete: a reader sees complete old or new data. There is a brief interval where the path is absent while the old inode is preserved. Exclusive publication refuses to overwrite an editor's intervening recreation of that path. Ordinary replacements and remote deletions retain old content in `.pysync-recovery`, and detected saves racing with an apply are recorded as local conflicts. Recovery data is never automatically deleted.

If the process stops after moving the old file but before publication/state update, startup replays the application intent **before** scanning for local deletions. If the destination already has the verified remote content, it records that content without another transfer. If the user wrote different data after the interruption, that local edit is left in place and reconciled through the normal optimistic queue. Unfinished operations retain their verified source until safely handled.

The CLI and background engine share one local daemon. Management commands use a mode-0600 Unix socket under a mode-0700 configuration directory, and an advisory process lock prevents competing daemons. Project attachment/removal and synchronization are serialized against the reconciler. Removing a project detaches its watchers and leaves all local files intact; pending work must finish unless the user explicitly selects `--discard-pending`.

## Desktop control plane

The optional PySide6 GUI uses the same private Unix-socket commands as the CLI, including server-project discovery, reviewed attachment, status, synchronization, removal, conflict resolution and shutdown. Shared `lifecycle.py` starts the single detached daemon and returns its actual status. The GUI never constructs an engine or file watcher. A stopped daemon's persisted projects, queue counts and conflicts are read through a read-only SQLite connection.

Qt worker threads run asynchronous network/IPC operations in their own event loops, then deliver results through signals to main-thread slots. Repeated submissions with the same key are suppressed, actions are disabled during submission, and closing waits for active requests. Periodic status refresh preserves unsaved form inputs and table selection. Owner-token registration is the only GUI HTTP management request made directly: it runs while holding the daemon's advisory lock so configuration cannot change underneath a running daemon. Only the returned device credential is persisted. Each project attachment requires a displayed plan and explicit acceptance; GUI detachment never sends the CLI's discard flag.
