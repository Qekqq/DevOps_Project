"""Encrypt a database dump for external storage; decrypt only to a new file."""

import argparse
import os
from getpass import getpass
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"DEVOPS-BACKUP-1\x00"
HEADER_SIZE = len(MAGIC) + 16 + 12
CHUNK = 1024 * 1024


def derive(password, salt):
    if not isinstance(password, str) or len(password) < 16:
        raise ValueError("Use a backup passphrase of at least 16 characters")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
        password.encode("utf-8")
    )


def transform(source, destination, password, *, decrypt=False):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Source must be a regular file")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Destination already exists; refusing overwrite")
    temporary = destination.with_name(
        "." + destination.name + "." + uuid4().hex + ".tmp"
    )
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            initial_stat = os.fstat(incoming.fileno())
            os.chmod(temporary, 0o600)
            if decrypt:
                size = os.fstat(incoming.fileno()).st_size
                header = incoming.read(HEADER_SIZE)
                if (
                    len(header) != HEADER_SIZE
                    or not header.startswith(MAGIC)
                    or size < HEADER_SIZE + 16
                ):
                    raise ValueError("Invalid encrypted backup format")
                incoming.seek(-16, os.SEEK_END)
                tag = incoming.read(16)
                incoming.seek(HEADER_SIZE)
                salt, nonce = header[len(MAGIC) : len(MAGIC) + 16], header[-12:]
                cipher = Cipher(
                    algorithms.AES(derive(password, salt)), modes.GCM(nonce, tag)
                ).decryptor()
                remaining = size - HEADER_SIZE - 16
            else:
                salt, nonce = os.urandom(16), os.urandom(12)
                header = MAGIC + salt + nonce
                outgoing.write(header)
                cipher = Cipher(
                    algorithms.AES(derive(password, salt)), modes.GCM(nonce)
                ).encryptor()
                remaining = os.fstat(incoming.fileno()).st_size
            cipher.authenticate_additional_data(header)
            while remaining:
                data = incoming.read(min(CHUNK, remaining))
                if not data:
                    raise ValueError("Backup source changed while reading")
                remaining -= len(data)
                outgoing.write(cipher.update(data))
            if not decrypt and incoming.read(1):
                raise ValueError("Backup source changed while reading")
            final_stat = os.fstat(incoming.fileno())
            if (initial_stat.st_size, initial_stat.st_mtime_ns) != (
                final_stat.st_size,
                final_stat.st_mtime_ns,
            ):
                raise ValueError("Backup source changed while reading")
            outgoing.write(cipher.finalize())
            if not decrypt:
                outgoing.write(cipher.tag)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        # Atomic no-clobber publication: unlike replace(), this cannot destroy
        # an existing destination created concurrently. Fails safely on a FS
        # without hard-link support; use a local NTFS/ext4 staging directory.
        os.link(temporary, destination)
        if os.name != "nt":
            fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--decrypt", action="store_true")
    args = parser.parse_args()
    password = getpass("Пароль внешней резервной копии (скрытый ввод): ")
    if not args.decrypt and password != getpass("Повторите пароль резервной копии: "):
        parser.error("Passphrases do not match")
    try:
        transform(args.source, args.destination, password, decrypt=args.decrypt)
    except Exception:
        raise SystemExit(
            "Backup operation failed; verify passphrase, file integrity and destination"
        ) from None
    print(f"Created: {args.destination}; database containers were not modified")


if __name__ == "__main__":
    main()
