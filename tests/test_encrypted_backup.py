import os

import pytest
from cryptography.exceptions import InvalidTag

from scripts.encrypted_backup import HEADER_SIZE, transform

PASSWORD = "synthetic test passphrase only"


def test_large_backup_roundtrip_and_randomized_ciphertext(tmp_path):
    source = tmp_path / "source.dump"
    source.write_bytes(os.urandom(2 * 1024 * 1024 + 17))
    first, second, restored = (
        tmp_path / name for name in ("a.enc", "b.enc", "restored.dump")
    )
    transform(source, first, PASSWORD)
    transform(source, second, PASSWORD)
    assert first.read_bytes() != second.read_bytes()
    transform(first, restored, PASSWORD, decrypt=True)
    assert restored.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("damage", ["password", "header", "payload", "tag", "truncate"])
def test_corruption_never_publishes_plaintext(tmp_path, damage):
    source, encrypted, restored = (
        tmp_path / name for name in ("source", "encrypted", "restored")
    )
    source.write_bytes(b"private synthetic data" * 100)
    transform(source, encrypted, PASSWORD)
    data = bytearray(encrypted.read_bytes())
    if damage in ("header", "payload", "tag"):
        index = {
            "header": len(b"DEVOPS-BACKUP-1\x00"),
            "payload": HEADER_SIZE + 3,
            "tag": len(data) - 1,
        }[damage]
        data[index] ^= 1
    elif damage == "truncate":
        data = data[:-30]
    encrypted.write_bytes(data)
    with pytest.raises((InvalidTag, ValueError)):
        transform(
            encrypted,
            restored,
            "wrong passphrase for this backup" if damage == "password" else PASSWORD,
            decrypt=True,
        )
    assert not restored.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_refuses_overwriting_destination(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"data")
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        transform(source, target, PASSWORD)
    assert target.read_bytes() == b"original"
