"""Platform-safe names for crypto state files owned by a Matrix account."""

from __future__ import annotations

import hashlib
import json
import os

from .exceptions import LocalProtocolError

_LEGACY_ACCOUNT_SUFFIXES = (
    "",
    ".db",
    ".trusted_devices",
    ".blacklisted_devices",
    ".ignored_devices",
)


def _windows_account_stem(user_id: str, device_id: str | None = None) -> str:
    """Keep arbitrary IDs out of Windows paths, including reserved chars and lengths."""
    identity = [user_id] if device_id is None else [user_id, device_id]
    encoded = json.dumps(identity, ensure_ascii=False).encode("utf-8")
    return f"nio-{hashlib.sha256(encoded).hexdigest()}"


def account_filename_stem(user_id: str, device_id: str | None = None) -> str:
    """Preserve historical POSIX names; use stable Windows-safe account names."""
    if os.name == "nt":
        return _windows_account_stem(user_id, device_id)
    return user_id if device_id is None else f"{user_id}_{device_id}"


def refuse_stranded_windows_account_state(
    store_path: str,
    user_id: str,
    device_id: str | None = None,
    *,
    new_database_path: str | None = None,
) -> None:
    """Refuse to open a default-named account store beside legacy Windows state.

    Releases before the Windows-safe account names wrote default store
    databases and trust sidecars under stems embedding the Matrix user id.
    On NTFS the id's colon materializes as an alternate data stream, but the
    state is real: opening the ``nio-<sha256>`` names beside it would create a
    fresh Olm account and strand the existing Megolm sessions and trust state
    without any error. Called on Windows regardless of an explicit database
    name, because trust sidecars always derive from the default stem.

    Raises:
        LocalProtocolError: naming the legacy artifact, the expected new
            stem, and the manual rename that carries the state over.
    """
    if os.name != "nt":
        return
    stem = user_id if device_id is None else f"{user_id}_{device_id}"
    for suffix in _LEGACY_ACCOUNT_SUFFIXES:
        legacy = os.path.join(store_path, f"{stem}{suffix}")
        if os.path.exists(legacy):
            target = new_database_path or os.path.join(
                store_path, f"{account_filename_stem(user_id, device_id)}.db"
            )
            raise LocalProtocolError(
                f"legacy account state present: {legacy!r} predates the "
                f"Windows-safe account names; opening {target!r} would strand "
                "its crypto state behind a new Olm account. Rename the legacy "
                f"files to the expected stem {account_filename_stem(user_id, device_id)!r} "
                "or move them out of the store path, then start again."
            )
