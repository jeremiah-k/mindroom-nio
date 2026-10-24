"""Failure injection at the private cross-signing identity publication boundary."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from nio.crypto import cross_signing
from nio.crypto.cross_signing import CrossSigningIdentity, cross_signing_sidecar_path


@pytest.fixture
def saved_identity(tmp_path):
    """Keep an existing account identity whose private seeds must survive."""
    identity = CrossSigningIdentity.generate("@relay:example.org")
    path = cross_signing_sidecar_path(str(tmp_path), identity.user_id)
    identity.save(path)
    return identity, path, path.read_bytes()


def test_partial_write_preserves_existing_identity(saved_identity, monkeypatch):
    """A disk write failing after partial progress cannot destroy saved keys."""
    identity, path, original = saved_identity
    original_fdopen = cross_signing.os.fdopen
    before = set(path.parent.iterdir())

    @contextmanager
    def broken_fdopen(*args, **kwargs):
        with original_fdopen(*args, **kwargs) as stream:

            def partial_write(payload):
                stream.write(payload[:5])
                stream.flush()
                raise OSError("injected partial write failure")

            yield SimpleNamespace(write=partial_write)

    monkeypatch.setattr(cross_signing.os, "fdopen", broken_fdopen)
    identity.signed_devices.append("ADDITIONAL_DEVICE")
    with pytest.raises(OSError, match="partial write failure"):
        identity.save(path)

    assert path.read_bytes() == original
    assert CrossSigningIdentity.load(path).master_seed == identity.master_seed
    assert set(path.parent.iterdir()) == before


def test_failed_replace_preserves_existing_identity(saved_identity, monkeypatch):
    """Publication failure leaves the complete previous identity available."""
    identity, path, original = saved_identity
    before = set(path.parent.iterdir())

    def broken_replace(*args, **kwargs):
        raise OSError("injected publication failure")

    monkeypatch.setattr(cross_signing.os, "replace", broken_replace)
    with pytest.raises(OSError, match="publication failure"):
        identity.save(path)
    assert path.read_bytes() == original
    assert set(path.parent.iterdir()) == before


def test_replacement_restores_private_permissions(saved_identity):
    """Replacement files retain owner-only permissions even after an unsafe chmod."""
    identity, path, _ = saved_identity
    path.chmod(0o644)
    identity.signed_devices.append("ADDITIONAL_DEVICE")
    identity.save(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert CrossSigningIdentity.load(path).signed_devices == ["ADDITIONAL_DEVICE"]
