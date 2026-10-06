"""API keys (M6 D8): only the sha256 is stored, a revoked key stops working."""

from pathlib import Path

import pytest

from app.api.keys import KeyStore
from app.brief.errors import NotFound
from app.store.db import Database


@pytest.fixture
def keys(tmp_path: Path) -> KeyStore:
    return KeyStore(Database(tmp_path / "udr.sqlite"))


def test_a_created_key_verifies_and_carries_its_flag(keys: KeyStore) -> None:
    key, text = keys.create("gui", self_approve=True)
    assert text.startswith("udr_")
    verified = keys.verify(text)
    assert verified is not None
    assert (verified.key_id, verified.name, verified.self_approve) == (key.key_id, "gui", True)
    other, other_text = keys.create("agent", self_approve=False)
    assert other.key_id != key.key_id
    assert other_text != text
    again = keys.verify(other_text)
    assert again is not None
    assert not again.self_approve


@pytest.mark.parametrize("text", ["", "udr_wrong", "Bearer x", "udr_" + "A" * 43])
def test_an_unknown_key_does_not_verify(keys: KeyStore, text: str) -> None:
    keys.create("gui", self_approve=True)
    assert keys.verify(text) is None


def test_a_revoked_key_does_not_verify_and_is_listed_as_revoked(keys: KeyStore) -> None:
    key, text = keys.create("gui", self_approve=True)
    keys.revoke(key.key_id)
    assert keys.verify(text) is None
    (listed,) = keys.list()
    assert listed.revoked_at is not None


def test_revoking_an_unknown_key_is_not_found(keys: KeyStore) -> None:
    with pytest.raises(NotFound):
        keys.revoke("k-nope")


def test_the_key_text_is_never_stored(tmp_path: Path) -> None:
    db = Database(tmp_path / "udr.sqlite")
    _, text = KeyStore(db).create("gui", self_approve=False)
    db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for path in tmp_path.iterdir():
        assert text.encode() not in path.read_bytes(), path.name


def test_the_listing_has_no_key_material(keys: KeyStore) -> None:
    _, text = keys.create("gui", self_approve=False)
    (listed,) = keys.list()
    assert text not in repr(listed)
    assert not hasattr(listed, "key_sha256")
