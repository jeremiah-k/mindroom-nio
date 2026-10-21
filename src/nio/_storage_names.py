"""Platform-safe names for crypto state files owned by a Matrix account."""

from __future__ import annotations

import hashlib
import json
import os

from .exceptions import LocalProtocolError

_TRUST_SIDECAR_SUFFIXES = (
    ".trusted_devices",
    ".blacklisted_devices",
    ".ignored_devices",
)
CROSS_SIGNING_SIDECAR_SUFFIX = "_cross_signing.json"


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
    """Refuse to bypass legacy Windows crypto state when selecting new names.

    Releases before the Windows-safe account names wrote default store
    databases and trust sidecars under stems embedding the Matrix user id.
    On NTFS the id's colon materializes as an alternate data stream, but the
    state is real: opening the ``nio-<sha256>`` names beside it would create a
    fresh Olm account and strand the existing Megolm sessions and trust state.
    Check account-wide signing keys as well. Explicit database paths may select
    the legacy database itself; trust and signing sidecars still need migration.
    Omitting the device ID checks only the account-wide signing identity.

    Raises:
        LocalProtocolError: names the legacy artifact and its target path.
    """
    if os.name != "nt":
        return
    # Match the historical cross-signing separator sanitization.
    legacy_user = user_id.replace("/", "_").replace("\\", "_").replace("\0", "_")
    artifacts = [
        (
            os.path.join(store_path, f"{legacy_user}{CROSS_SIGNING_SIDECAR_SUFFIX}"),
            os.path.join(
                store_path,
                f"{account_filename_stem(user_id)}{CROSS_SIGNING_SIDECAR_SUFFIX}",
            ),
        )
    ]
    if device_id is not None:
        legacy_stem = os.path.join(store_path, f"{user_id}_{device_id}")
        target_stem = os.path.join(
            store_path, account_filename_stem(user_id, device_id)
        )
        database_target = new_database_path or f"{target_stem}.db"
        artifacts.extend(
            (f"{legacy_stem}.db{suffix}", f"{database_target}{suffix}")
            for suffix in ("", "-wal", "-shm", "-journal")
        )
        artifacts.extend(
            (f"{legacy_stem}{suffix}", f"{target_stem}{suffix}")
            for suffix in _TRUST_SIDECAR_SUFFIXES
        )
    for legacy, target in artifacts:
        if os.path.normcase(os.path.abspath(legacy)) == os.path.normcase(
            os.path.abspath(target)
        ):
            continue
        if os.path.exists(legacy):
            raise LocalProtocolError(
                f"legacy account state present: {legacy}; refusing to bypass it "
                f"with {target}. Close all clients and back up the complete "
                "store before migrating this artifact to the target path. "
                "Preserve SQLite journals, encryption keys, trust state, and "
                "cross-signing keys together before retrying."
            )
