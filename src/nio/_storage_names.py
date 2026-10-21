"""Platform-safe names for crypto state files owned by a Matrix account."""

from __future__ import annotations

import hashlib
import json
import os


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
