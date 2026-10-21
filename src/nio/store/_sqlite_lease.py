"""Opening-time coordination for ordinary and durable SQLite stores."""

from __future__ import annotations

import os
import sqlite3
import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from peewee import SqliteDatabase

from ..exceptions import LocalProtocolError


def _windows_lease(fd: int, *, exclusive: bool, unlock: bool = False) -> None:
    """Hold or release one sidecar byte using native shared/exclusive locks."""
    if sys.platform != "win32":
        raise OSError("Windows store leases require native Windows")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = msvcrt.get_osfhandle(fd)
    overlapped = ctypes.byref(Overlapped())
    if unlock:
        unlock_file = kernel32.UnlockFileEx
        unlock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        unlock_file.restype = wintypes.BOOL
        success = unlock_file(handle, 0, 1, 0, overlapped)
    else:
        lock_file = kernel32.LockFileEx
        lock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        lock_file.restype = wintypes.BOOL
        # msvcrt.locking has no shared-lock equivalent to LockFileEx.
        flags = 0x01 | (0x02 if exclusive else 0)  # FAIL_IMMEDIATELY | EXCLUSIVE
        success = lock_file(handle, flags, 0, 1, 0, overlapped)

    if not success:
        code = ctypes.get_last_error()
        if not unlock and code in (32, 33):  # SHARING_VIOLATION, LOCK_VIOLATION
            raise BlockingIOError("store lifetime lease is already held")
        raise OSError(code, ctypes.FormatError(code))


class FileLease:
    """Lifetime sidecar lock; live database path replacement is unsupported."""

    def __init__(self, database_path: Path, *, exclusive: bool = True) -> None:
        if os.name != "nt":
            try:
                import fcntl
            except ImportError as error:
                raise LocalProtocolError(
                    "store filesystem ownership requires fcntl support"
                ) from error
        self.pid = os.getpid()
        self._locked = False
        self.fd = os.open(
            f"{database_path.resolve()}.ingest.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            if os.name == "nt":
                _windows_lease(self.fd, exclusive=exclusive)
            else:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(self.fd, mode | fcntl.LOCK_NB)
            self._locked = True
        except BaseException as error:
            self.close()
            if isinstance(error, BlockingIOError):
                raise LocalProtocolError(
                    "store lifetime lease is already held"
                ) from error
            raise

    def assert_open(self) -> None:
        if self.fd < 0 or self.pid != os.getpid():
            raise LocalProtocolError(
                "store lease is closed or belongs to another process"
            )

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            try:
                if os.name == "nt" and self._locked:
                    _windows_lease(fd, exclusive=False, unlock=True)
            finally:
                self._locked = False
                os.close(fd)


def check_ordinary_database(connection: sqlite3.Connection) -> None:
    marker = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name COLLATE NOCASE IN ('NioIngestMeta', 'NioDurableMeta')"
    ).fetchone()
    if marker is not None:
        if marker[0].lower() == "niodurablemeta":
            raise LocalProtocolError("this database requires a durable sync session")
        raise LocalProtocolError(
            "unmerged ingestion format requires explicit replacement"
        )


# A failed final close (for example GC on another thread) must retain exclusion
# until process exit. Supported explicit closes normally release immediately.
_DEFERRED_LEASES: list[FileLease] = []


class _LeasedConnection(sqlite3.Connection):
    def __init__(self, *args: Any, lease: FileLease, **kwargs: Any) -> None:
        self._lease: FileLease | None = lease
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        super().close()
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def __del__(self) -> None:
        try:
            self.close()
        except sqlite3.Error:
            lease = getattr(self, "_lease", None)
            if lease is not None:
                _DEFERRED_LEASES.append(lease)
                self._lease = None


class LeasedSqliteDatabase(SqliteDatabase):
    """One shared lease per physical connection, checked again on reconnect."""

    if TYPE_CHECKING:
        _timeout: float

        def _add_conn_hooks(self, connection: sqlite3.Connection) -> None: ...

    def _connect(self) -> sqlite3.Connection:
        lease = FileLease(Path(self.database), exclusive=False)
        try:
            connection = sqlite3.connect(
                self.database,
                timeout=self._timeout,
                isolation_level=None,
                **{
                    **self.connect_params,
                    "factory": partial(_LeasedConnection, lease=lease),
                },
            )
        except BaseException:
            lease.close()
            raise
        try:
            # Check before hooks: even PRAGMA writes must not touch an adopted DB.
            check_ordinary_database(connection)
            self._add_conn_hooks(connection)
        except BaseException:
            connection.close()
            raise
        return connection
