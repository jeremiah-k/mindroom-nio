"""Ordinary SQLite connections exclude adoption for their physical lifetime."""

import gc
import os
import sqlite3
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

from nio._storage_names import _windows_account_stem, account_filename_stem
from nio.crypto import OlmAccount, OlmDevice
from nio.exceptions import LocalProtocolError
from nio.store import DefaultStore, SqliteMemoryStore, SqliteStore
from nio.store._sqlite_lease import FileLease, LeasedSqliteDatabase

from .store_test import CONSUMER, DEVICE, USER, open_store


def test_two_ordinary_connections_exclude_durable_open_in_another_process(tmp_path):
    first = SqliteStore(USER, DEVICE, str(tmp_path))
    second = SqliteStore(USER, DEVICE, str(tmp_path))
    program = """
import sys
from pathlib import Path
from uuid import UUID
from nio.durable.store import DurableStore
from nio.exceptions import LocalProtocolError
try:
    store = DurableStore(Path(sys.argv[1]), user_id=sys.argv[2], device_id=sys.argv[3], consumer_id=UUID(sys.argv[4]))
except LocalProtocolError as error:
    assert 'lease' in str(error)
    sys.exit(3)
store.close()
"""

    def attempt():
        return subprocess.run(
            [sys.executable, "-c", program, str(tmp_path), USER, DEVICE, str(CONSUMER)],
            capture_output=True,
            text=True,
            timeout=20,
        )

    try:
        result = attempt()
        assert result.returncode == 3, result.stderr
        first.database.close()
        second.save_account(OlmAccount())
        result = attempt()
        assert result.returncode == 3, result.stderr
        second.database.close()
        result = attempt()
        assert result.returncode == 0, result.stderr
    finally:
        first.database.close()
        second.database.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses LockFileEx")
def test_package_import_without_fcntl_and_ownership_failure(tmp_path):
    program = """
import sys
sys.modules['fcntl'] = None
from pathlib import Path
from uuid import UUID
import nio
from nio.durable.store import DurableStore
from nio.exceptions import LocalProtocolError
from nio.store import SqliteStore
for factory in (
    lambda: SqliteStore('@alice:example.org', 'ALICE', sys.argv[1]),
    lambda: DurableStore(Path(sys.argv[1]), user_id='@alice:example.org', device_id='ALICE', consumer_id=UUID(int=1)),
):
    try:
        factory()
    except LocalProtocolError as error:
        assert 'requires fcntl' in str(error)
    else:
        raise AssertionError('ownership succeeded without filesystem locking')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_shared_leases_exclude_exclusive_adoption(tmp_path):
    path = tmp_path / "store.db"
    first = FileLease(path, exclusive=False)
    second = FileLease(path, exclusive=False)
    try:
        assert Path(f"{path}.ingest.lock").stat().st_size == 0
        with pytest.raises(LocalProtocolError, match="lease"):
            FileLease(path)
        first.close()
        with pytest.raises(LocalProtocolError, match="lease"):
            FileLease(path)
    finally:
        first.close()
        second.close()

    exclusive = FileLease(path)
    try:
        with pytest.raises(LocalProtocolError, match="lease"):
            FileLease(path)
        with pytest.raises(LocalProtocolError, match="lease"):
            FileLease(path, exclusive=False)
    finally:
        exclusive.close()
    shared = FileLease(path, exclusive=False)
    shared.close()


@pytest.mark.parametrize("held_exclusive", [False, True])
@pytest.mark.parametrize("requested_exclusive", [False, True])
def test_lease_modes_across_processes(tmp_path, held_exclusive, requested_exclusive):
    path = tmp_path / "store.db"
    program = """
import sys
from pathlib import Path
from nio.exceptions import LocalProtocolError
from nio.store._sqlite_lease import FileLease
try:
    lease = FileLease(Path(sys.argv[1]), exclusive=sys.argv[2] == 'True')
except LocalProtocolError as error:
    assert 'lease' in str(error)
    sys.exit(3)
lease.close()
"""

    def attempt():
        return subprocess.run(
            [sys.executable, "-c", program, str(path), str(requested_exclusive)],
            capture_output=True,
            text=True,
            timeout=20,
        )

    held = FileLease(path, exclusive=held_exclusive)
    try:
        result = attempt()
        expected = 3 if held_exclusive or requested_exclusive else 0
        assert result.returncode == expected, result.stderr
    finally:
        held.close()
    result = attempt()
    assert result.returncode == 0, result.stderr


def test_failed_connection_initialization_releases_lease(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("connection setup interrupted")

    monkeypatch.setattr(LeasedSqliteDatabase, "_add_conn_hooks", fail)
    with pytest.raises(RuntimeError, match="setup interrupted"):
        SqliteStore(USER, DEVICE, str(tmp_path))
    store = open_store(tmp_path)
    store.close()


@pytest.mark.parametrize("store_class", [SqliteStore, DefaultStore])
def test_finalization_releases_lease_only_after_last_connection_user(
    tmp_path, store_class
):
    store = store_class(USER, DEVICE, str(tmp_path))
    cursor = store.database.execute_sql("SELECT 42")
    reference = weakref.ref(store)
    del store
    gc.collect()
    assert reference() is None
    with pytest.raises(LocalProtocolError, match="lease"):
        open_store(tmp_path)
    assert cursor.fetchone() == (42,)
    del cursor
    gc.collect()
    adopted = open_store(tmp_path, source_store_class=store_class)
    adopted.close()


def test_failed_physical_close_retains_connection_lease(tmp_path):
    store = SqliteStore(USER, DEVICE, str(tmp_path))
    connection = store.database.connection()
    failures = []

    def close():
        try:
            connection.close()
        except sqlite3.ProgrammingError as error:
            failures.append(error)

    thread = threading.Thread(target=close)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(failures) == 1
    with pytest.raises(LocalProtocolError, match="lease"):
        open_store(tmp_path)
    assert store.load_account() is None
    store.database.close()
    adopted = open_store(tmp_path)
    adopted.close()


@pytest.mark.parametrize("marker", ["niodurablemeta", "nioingestmeta"])
def test_closed_ordinary_reconnect_rejects_case_insensitive_markers(tmp_path, marker):
    store = SqliteStore(USER, DEVICE, str(tmp_path))
    store.database.close()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(f"CREATE TABLE {marker} (id INTEGER)")
    with pytest.raises(LocalProtocolError, match="durable|unmerged ingestion"):
        store.save_account(OlmAccount())


def test_default_sidecar_operation_holds_lease_through_connection_close(
    tmp_path, monkeypatch
):
    store = DefaultStore(USER, DEVICE, str(tmp_path))
    device = OlmDevice("@bob:example.org", "BOB", OlmAccount().identity_keys)
    remove = store.blacklist_db.remove

    def close_during_remove(key):
        result = remove(key)
        store.database.close()
        with pytest.raises(LocalProtocolError, match="lease"):
            open_store(tmp_path, source_store_class=DefaultStore)
        return result

    monkeypatch.setattr(store.blacklist_db, "remove", close_during_remove)
    assert store.verify_device(device)
    adopted = open_store(tmp_path, source_store_class=DefaultStore)
    adopted.close()
    with pytest.raises(LocalProtocolError, match="adoption"):
        store.is_device_verified(device)


@pytest.mark.parametrize("marker", ["NioIngestMeta", "nioingestmeta"])
def test_durable_rejects_prototype_marker_before_schema_writes(tmp_path, marker):
    path = tmp_path / f"{account_filename_stem(USER, DEVICE)}.db"
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE {marker} (id INTEGER)")
    with pytest.raises(LocalProtocolError, match="unmerged ingestion"):
        open_store(tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [(marker,)]


def test_windows_account_filename_stems_are_safe_and_unambiguous():
    a = _windows_account_stem("@alice:example.org", "ALICE")
    assert a == _windows_account_stem("@alice:example.org", "ALICE")
    assert a != _windows_account_stem("@alice:example.org", "OTHER")
    assert a != _windows_account_stem("@alice/example.org", "ALICE")
    assert a != _windows_account_stem("@alice:example.org_ALICE")
    assert len(a) == 68
    assert a.startswith("nio-")
    assert set(a[4:]) <= set("0123456789abcdef")


def test_default_filenames_agree_across_ordinary_durable_and_sidecars(tmp_path):
    expected = f"{account_filename_stem(USER, DEVICE)}.db"
    ordinary = DefaultStore(USER, DEVICE, str(tmp_path))
    try:
        assert ordinary.database_name == expected
        assert ordinary.database_path == str(tmp_path / expected)
        stem = account_filename_stem(USER, DEVICE)
        for name, suffix in (
            (ordinary.trust_db, "trusted_devices"),
            (ordinary.blacklist_db, "blacklisted_devices"),
            (ordinary.ignore_db, "ignored_devices"),
        ):
            assert name._filename == str(tmp_path / f"{stem}.{suffix}")
        if os.name == "nt":
            assert ":" not in expected
            assert ":" not in Path(ordinary.trust_db._filename).name
    finally:
        ordinary.database.close()
    durable = open_store(tmp_path, source_store_class=DefaultStore)
    try:
        assert durable.path == tmp_path / expected
    finally:
        durable.close()


def test_explicit_database_name_is_not_rewritten(tmp_path):
    ordinary = SqliteStore(USER, DEVICE, str(tmp_path), database_name="state.db")
    try:
        assert ordinary.database_name == "state.db"
        assert ordinary.database_path == str(tmp_path / "state.db")
    finally:
        ordinary.database.close()
    durable = open_store(tmp_path / "explicit", database_name="state.db")
    try:
        assert durable.path.name == "state.db"
    finally:
        durable.close()


def test_cross_signing_sidecar_filename_is_windows_safe(tmp_path):
    from nio.crypto.cross_signing import cross_signing_sidecar_path

    path = cross_signing_sidecar_path(str(tmp_path), USER)
    assert path.parent == tmp_path
    if os.name == "nt":
        assert path.name == f"{account_filename_stem(USER)}_cross_signing.json"
        assert ":" not in path.name
    else:
        assert path.name == f"{USER}_cross_signing.json"


@pytest.fixture
def windows_storage_names(monkeypatch):
    """Exercise Windows names without changing pathlib's platform globally."""
    from nio import _storage_names
    from nio.crypto import cross_signing

    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})
    monkeypatch.setattr(_storage_names, "os", windows_os)
    monkeypatch.setattr(cross_signing, "os", windows_os)


@pytest.mark.parametrize("store_class", [SqliteStore, DefaultStore])
@pytest.mark.parametrize(
    "suffix",
    [
        ".db",
        ".db-wal",
        ".db-shm",
        ".db-journal",
        ".trusted_devices",
        ".blacklisted_devices",
        ".ignored_devices",
    ],
)
def test_windows_store_refuses_to_strand_legacy_account_state(
    tmp_path, windows_storage_names, store_class, suffix
):
    legacy = tmp_path / f"{USER}_{DEVICE}{suffix}"
    legacy.write_bytes(b"legacy account database")

    with pytest.raises(
        LocalProtocolError, match="legacy account state present"
    ) as info:
        store_class(USER, DEVICE, str(tmp_path))

    message = str(info.value)
    assert str(legacy) in message
    assert account_filename_stem(USER, DEVICE) in message
    assert legacy.read_bytes() == b"legacy account database"
    assert not (tmp_path / f"{account_filename_stem(USER, DEVICE)}.db").exists()


@pytest.mark.parametrize("database_name", [None, "state.db"])
@pytest.mark.parametrize(
    "suffix",
    [
        ".db",
        ".db-wal",
        ".db-shm",
        ".db-journal",
        ".trusted_devices",
        ".blacklisted_devices",
        ".ignored_devices",
    ],
)
def test_windows_durable_store_refuses_to_strand_legacy_account_state(
    tmp_path, windows_storage_names, database_name, suffix
):
    from nio.durable.store import DurableStore

    legacy = tmp_path / f"{USER}_{DEVICE}{suffix}"
    legacy.write_bytes(b"legacy trust sidecar")

    with pytest.raises(LocalProtocolError, match="legacy account state present"):
        DurableStore(
            tmp_path,
            user_id=USER,
            device_id=DEVICE,
            consumer_id=CONSUMER,
            database_name=database_name,
        )
    assert legacy.read_bytes() == b"legacy trust sidecar"
    target = database_name or f"{account_filename_stem(USER, DEVICE)}.db"
    assert not (tmp_path / target).exists()


def test_windows_probe_allows_store_directory_without_legacy_artifacts(
    tmp_path, windows_storage_names
):
    from nio._storage_names import refuse_stranded_windows_account_state

    (tmp_path / f"{_windows_account_stem(USER, DEVICE)}.db").write_bytes(b"new")

    refuse_stranded_windows_account_state(str(tmp_path), USER, DEVICE)


@pytest.mark.parametrize("store_class", [SqliteStore, DefaultStore])
@pytest.mark.parametrize("durable", [False, True])
def test_explicit_legacy_database_can_be_reopened(
    tmp_path, windows_storage_names, durable, store_class
):
    """An explicit legacy path continues to select the existing crypto account."""
    # A safe legacy ID is readable by SQLite on every platform, so this also
    # checks preservation of real encrypted state in the native Windows job.
    user = "legacy"
    filename = f"{user}_{DEVICE}.db"
    account = OlmAccount()
    ordinary = store_class(user, DEVICE, str(tmp_path), database_name=filename)
    try:
        ordinary.save_account(account)
    finally:
        ordinary.database.close()
    if durable:
        from nio.durable.store import DurableStore

        store = DurableStore(
            tmp_path,
            user_id=user,
            device_id=DEVICE,
            consumer_id=CONSUMER,
            database_name=filename,
            source_store_class=store_class,
        )
        try:
            assert store.matrix.load_account().identity_keys == account.identity_keys
        finally:
            store.close()
    else:
        reopened = store_class(user, DEVICE, str(tmp_path), database_name=filename)
        try:
            assert reopened.load_account().identity_keys == account.identity_keys
        finally:
            reopened.database.close()


def _write_legacy_signing_identity(identity, path):
    """Model the old in-place writer, including NTFS alternate data streams.

    The current atomic publisher intentionally cannot replace a colon-named
    stream. Serialize through a safe staging name, then create the historical
    artifact with the legacy write semantics the migration guard must detect.
    """
    staging = path.with_name("legacy-signing-seed.json")
    try:
        identity.save(staging)
        path.write_bytes(staging.read_bytes())
    finally:
        staging.unlink(missing_ok=True)


@pytest.mark.parametrize("consumer", ["ordinary", "durable", "cross_signing"])
@pytest.mark.parametrize("user", [USER, "@al/ice\\evil:example.org"])
def test_legacy_cross_signing_identity_is_not_bypassed(
    tmp_path, windows_storage_names, consumer, user
):
    """Legacy signing keys must not be replaced by an empty hashed identity."""
    from nio.crypto.cross_signing import (
        CrossSigningIdentity,
        cross_signing_sidecar_path,
    )

    sanitized = user.replace("/", "_").replace("\\", "_")
    legacy = tmp_path / f"{sanitized}_cross_signing.json"
    identity = CrossSigningIdentity.generate(user)
    _write_legacy_signing_identity(identity, legacy)
    before = legacy.read_bytes()
    with pytest.raises(LocalProtocolError, match="legacy account state present"):
        if consumer == "ordinary":
            SqliteStore(user, DEVICE, str(tmp_path))
        elif consumer == "durable":
            from nio.durable.store import DurableStore

            DurableStore(tmp_path, user_id=user, device_id=DEVICE, consumer_id=CONSUMER)
        else:
            cross_signing_sidecar_path(str(tmp_path), user)
    assert legacy.read_bytes() == before
    assert not (tmp_path / f"{_windows_account_stem(user, DEVICE)}.db").exists()
    assert not (tmp_path / f"{_windows_account_stem(user)}_cross_signing.json").exists()


def test_migrated_cross_signing_identity_keeps_its_keys(
    tmp_path, windows_storage_names
):
    """Both-name conflicts fail closed; a completed migration reuses the keys."""
    from nio.crypto.cross_signing import (
        CrossSigningIdentity,
        cross_signing_sidecar_path,
    )

    legacy = tmp_path / f"{USER}_cross_signing.json"
    target = tmp_path / f"{_windows_account_stem(USER)}_cross_signing.json"
    identity = CrossSigningIdentity.generate(USER)
    _write_legacy_signing_identity(identity, legacy)
    target.write_bytes(legacy.read_bytes())
    with pytest.raises(LocalProtocolError, match="legacy account state present"):
        cross_signing_sidecar_path(str(tmp_path), USER)
    legacy.unlink()
    assert cross_signing_sidecar_path(str(tmp_path), USER) == target
    loaded = CrossSigningIdentity.load(target)
    assert loaded.master_seed == identity.master_seed
    assert loaded.self_signing_seed == identity.self_signing_seed


def test_memory_store_does_not_probe_legacy_files(
    tmp_path, windows_storage_names, monkeypatch
):
    """An in-memory account must not depend on files in the working directory."""
    legacy = tmp_path / f"{USER}_{DEVICE}.db"
    legacy.write_bytes(b"unrelated disk state")
    monkeypatch.chdir(tmp_path)
    store = SqliteMemoryStore(USER, DEVICE)
    try:
        assert store.load_account() is None
        assert legacy.read_bytes() == b"unrelated disk state"
    finally:
        store.database.close()
