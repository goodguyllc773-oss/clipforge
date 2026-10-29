"""OAuth tokens live in the OS credential store (Windows Credential Manager /
macOS Keychain / Secret Service on Linux via the ``keyring`` package) — never
in the database, a log, or plain text on disk. ``PublishAccount.credential_key``
is only a lookup key; losing the database loses no secrets.

Each account's token is stored as one JSON blob under service name
``clipforge-publish`` and username ``credential_key``.
"""

from __future__ import annotations

import json
from typing import Any

SERVICE = "clipforge-publish"


def _backend():
    import keyring
    return keyring


def save_token(credential_key: str, token_data: dict[str, Any]) -> None:
    _backend().set_password(SERVICE, credential_key, json.dumps(token_data))


def load_token(credential_key: str) -> dict[str, Any] | None:
    raw = _backend().get_password(SERVICE, credential_key)
    return json.loads(raw) if raw else None


def delete_token(credential_key: str) -> None:
    try:
        _backend().delete_password(SERVICE, credential_key)
    except Exception:
        pass  # already gone, or no backend available — disconnect must not fail on this
