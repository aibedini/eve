"""Streaming authenticated encryption for backups leaving the host."""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC = b'EVE-BACKUP-AESGCM-v1\0'


def _backup_key() -> bytes:
    encoded = (os.environ.get('EVE_BACKUP_KEY') or os.environ.get('SERVER_PASSWORD_KEY') or '').strip()
    if not encoded:
        raise RuntimeError('EVE_BACKUP_KEY (or SERVER_PASSWORD_KEY fallback) is required')
    try:
        key = base64.urlsafe_b64decode(encoded.encode('ascii'))
    except Exception as exc:
        raise RuntimeError('EVE_BACKUP_KEY is not valid URL-safe base64') from exc
    if len(key) != 32:
        raise RuntimeError('EVE_BACKUP_KEY must decode to exactly 32 bytes')
    return key


def encrypt_backup_file(source: str, destination: str | None = None) -> str:
    """Encrypt a file with AES-256-GCM and return the output path."""
    source_path = Path(source)
    destination_path = Path(destination or f'{source}.eveenc')
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(_backup_key()), modes.GCM(nonce)).encryptor()
    with source_path.open('rb') as src, destination_path.open('wb') as dst:
        dst.write(MAGIC)
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
    header_size = len(MAGIC) + 12
    if total_size < header_size + 16:
        raise ValueError('Encrypted backup is truncated')
    destination_path = Path(destination)
    temp_path = destination_path.with_name(
        f'.{destination_path.name}.part-{os.getpid()}'
    )
    try:
        with source_path.open('rb') as src:
            if src.read(len(MAGIC)) != MAGIC:
                raise ValueError('Not an Eve encrypted backup')
            nonce = src.read(12)
            src.seek(-16, os.SEEK_END)
            tag = src.read(16)
            ciphertext_size = total_size - header_size - 16
            src.seek(header_size)
            decryptor = Cipher(algorithms.AES(_backup_key()), modes.GCM(nonce, tag)).decryptor()
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
