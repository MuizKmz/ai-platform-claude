"""Connector credential storage.

Credentials are encrypted by the application before they reach the database, with
a key that lives outside it. That ordering is the point: a database backup, a
replica, or a stolen dump contains ciphertext and nothing else. Encryption at rest
provided by the storage layer protects the disk, not the dump.

`SecretStr` is used throughout because it prevents the ordinary accidents —
`repr()`, f-strings, log lines, exception messages, and Pydantic serialisation all
render it as `**********`. The value comes out only when someone writes
`.get_secret_value()`, which is easy to grep for in review.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.core.config import settings


class CredentialError(Exception):
    """Encryption or decryption failed. Never carries the value or the key."""


def _fernet() -> Fernet:
    """Build the cipher from the configured key.

    The key is derived rather than used raw so any sufficiently long secret works
    as configuration, while Fernet still receives the exact 32 bytes it requires.
    A real deployment sources this from a secret manager, and rotating it means
    re-encrypting stored credentials — which is why the key is separate from the
    database rather than a column in it.
    """
    secret = settings.credential_encryption_key
    if not secret:
        raise CredentialError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Connector credentials cannot be "
            "stored or read without it."
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: SecretStr) -> str:
    """Encrypt a credential for storage. Returns base64 ciphertext."""
    try:
        return _fernet().encrypt(value.get_secret_value().encode("utf-8")).decode("ascii")
    except CredentialError:
        raise
    except Exception as exc:  # noqa: BLE001 — never surface cipher internals
        raise CredentialError("credential encryption failed") from exc


def decrypt(ciphertext: str) -> SecretStr:
    """Decrypt a stored credential.

    Returns SecretStr so the plaintext cannot reach a log line by accident on its
    way to the caller that needs it.
    """
    try:
        return SecretStr(_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8"))
    except InvalidToken as exc:
        # Wrong key, tampered ciphertext, or a value encrypted before a rotation.
        # All three are indistinguishable here and none should say more.
        raise CredentialError("credential could not be decrypted") from exc
    except CredentialError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CredentialError("credential decryption failed") from exc
