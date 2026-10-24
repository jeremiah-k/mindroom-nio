"""Atomic sidecar publication ordering and POSIX directory durability."""

import os
import stat

import pytest

from nio import _atomic_write


def _record_fsync_types(monkeypatch):
    calls = []
    original = os.fsync

    def recording(fd):
        mode = os.fstat(fd).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        original(fd)

    monkeypatch.setattr(_atomic_write.os, "fsync", recording)
    return calls


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
@pytest.mark.parametrize("overwrite", [True, False])
def test_atomic_write_syncs_directory(tmp_path, monkeypatch, overwrite):
    target = tmp_path / "identity.json"
    if overwrite:
        target.write_text("old")
    calls = _record_fsync_types(monkeypatch)
    with _atomic_write.atomic_write(target, overwrite=overwrite) as handle:
        handle.write("new complete identity")
    assert target.read_text() == "new complete identity"
    assert calls == ["file", "directory"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["identity.json"]


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
def test_failed_replace_does_not_publish_or_sync_directory(tmp_path, monkeypatch):
    target = tmp_path / "identity.json"
    target.write_text("previous identity")
    calls = _record_fsync_types(monkeypatch)

    def fail_replace(*args):
        raise OSError("publication refused")

    monkeypatch.setattr(_atomic_write.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication refused"):
        with _atomic_write.atomic_write(target, overwrite=True) as handle:
            handle.write("replacement")
    assert target.read_text() == "previous identity"
    assert calls == ["file"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["identity.json"]


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
def test_directory_fsync_failure_reports_published_state(tmp_path, monkeypatch):
    target = tmp_path / "identity.json"
    target.write_text("previous identity")
    original = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        original(fd)

    monkeypatch.setattr(_atomic_write.os, "fsync", fail_directory_sync)
    with pytest.raises(OSError, match="directory sync failed"):
        with _atomic_write.atomic_write(target, overwrite=True) as handle:
            handle.write("new complete identity")
    assert target.read_text() == "new complete identity"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["identity.json"]
