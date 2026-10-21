# SQLite crypto stores on Windows

Native Windows stores use shared `LockFileEx` leases for ordinary connections
and an exclusive lease for durable-store ownership. Close every ordinary
connection before adopting its database as a durable store.

Default Windows database and trust-sidecar filenames use a stable `nio-` prefix
and SHA-256 hash of the user/device identity. Cross-signing sidecars use the
account identity alone, so devices on the same account share their signing keys.
POSIX filenames keep their existing form. An explicit `database_name` continues
to select that path, including an existing legacy database.

## Existing Windows state

Older stores used raw user/device IDs in filenames. A colon can address an
[NTFS named data stream](https://learn.microsoft.com/en-us/windows/win32/fileio/file-streams),
so an invalid-looking legacy path can still hold encryption keys and sessions.
These streams do not appear as separate files in a normal directory listing.

Before opening a store or selecting a cross-signing identity, nio checks the
corresponding legacy paths. If opening a new name would bypass existing state,
it raises `LocalProtocolError` naming the artifact and its expected target.
This also applies when both old and new names already exist. The check does
not rename, remove, or rewrite existing state.

Before migrating:

1. Stop every client using the store and back up the complete store, including
   NTFS named streams. Confirm that the backup preserves their contents.
2. Use the previously working client or SQLite tooling to checkpoint and close
   the old database. A database with outstanding `-wal` or `-journal` state
   must be recovered with its companion files before copying it on its own.
3. Transfer the database and any trust/signing sidecars to the target paths
   reported by the errors. Preserve their contents and restrict access to
   these private keys. Named-stream contents must be copied into regular
   files; renaming the stream's host file does not perform this conversion.
4. Verify the migrated account keys, sessions, and trust state before resuming
   service. Keep the verified backup until recovery is complete. If both old
   and new stores contain state, reconcile them before retiring either copy.

An explicit database path that still selects the legacy database does not
bypass it and can continue to open it. Legacy trust and cross-signing sidecars
still need migration because their names are derived independently.
