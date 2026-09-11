"""Streaming authenticated encryption for backups leaving the host.

Backups carry a versioned magic header so the key can be rotated: v1 uses
EVE_BACKUP_KEY (or the SERVER_PASSWORD_KEY fallback), v2 uses the explicit
EVE_KEY_BACKUP_V2 key. New backups are written with the newest configured
version, and decrypt picks the key from the file's own header, so old archives
stay readable and rotation needs no downtime.
"""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC_V1 = b'EVE-BACKUP-AESGCM-v1\0'
MAGIC_V2 = b'EVE-BACKUP-AESGCM-v2\0'
MAGIC = MAGIC_V1  # historical export
_HEADER_BYTES = len(MAGIC_V1) + 12  # magic + nonce
_MAGICS = {MAGIC_V1: 1, MAGIC_V2: 2}


def current_backup_version() -> int:
    return 2 if (os.environ.get('EVE_KEY_BACKUP_V2') or '').strip() else 1


def _backup_key(version: int = 1) -> bytes:
    if version >= 2:
        encoded = (os.environ.get('EVE_KEY_BACKUP_V2') or '').strip()
        source = 'EVE_KEY_BACKUP_V2'
    else:
        encoded = (os.environ.get('EVE_BACKUP_KEY') or os.environ.get('SERVER_PASSWORD_KEY') or '').strip()
        source = 'EVE_BACKUP_KEY'
    if not encoded:
        raise RuntimeError(f'{source} is required to encrypt or decrypt backups')
    try:
        key = base64.urlsafe_b64decode(encoded.encode('ascii'))
    except Exception as exc:
        raise RuntimeError(f'{source} is not valid URL-safe base64') from exc
    if len(key) != 32:
        raise RuntimeError(f'{source} must decode to exactly 32 bytes')
    return key


def encrypt_backup_file(source: str, destination: str | None = None) -> str:
    """Encrypt a file with AES-256-GCM and return the output path."""
    source_path = Path(source)
    destination_path = Path(destination or f'{source}.eveenc')
    version = current_backup_version()
    magic = MAGIC_V2 if version == 2 else MAGIC_V1
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(_backup_key(version)), modes.GCM(nonce)).encryptor()
    with source_path.open('rb') as src, destination_path.open('wb') as dst:
        dst.write(magic)
        dst.write(nonce)
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)
    return str(destination_path)


def decrypt_backup_file(source: str, destination: str) -> str:
    """Decrypt a file produced by encrypt_backup_file.

    Plaintext is streamed to a temporary sibling and only moved into place after
    the AES-GCM tag authenticates. A truncated, tampered, or wrong-key archive
    therefore never leaves unauthenticated plaintext at the destination path.
    """
    source_path = Path(source)
    total_size = source_path.stat().st_size
    if total_size < _HEADER_BYTES + 16:
        raise ValueError('Encrypted backup is truncated')
    destination_path = Path(destination)
    temp_path = destination_path.with_name(
        f'.{destination_path.name}.part-{os.getpid()}'
    )
    try:
        with source_path.open('rb') as src:
            magic = src.read(len(MAGIC_V1))
            version = _MAGICS.get(magic)
            if version is None:
                raise ValueError('Not an Eve encrypted backup')
            nonce = src.read(12)
            src.seek(-16, os.SEEK_END)
            tag = src.read(16)
            ciphertext_size = total_size - _HEADER_BYTES - 16
            src.seek(_HEADER_BYTES)
            decryptor = Cipher(
                algorithms.AES(_backup_key(version)), modes.GCM(nonce, tag),
            ).decryptor()
            with open(temp_path, 'wb') as dst:
                remaining = ciphertext_size
                while remaining:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('Encrypted backup is truncated')
                    remaining -= len(chunk)
                    dst.write(decryptor.update(chunk))
                dst.write(decryptor.finalize())
        os.replace(temp_path, destination_path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return str(destination_path)
