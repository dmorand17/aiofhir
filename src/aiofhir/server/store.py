"""Fernet-encrypted token storage, keyed by subject."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import MutableMapping

from cryptography.fernet import Fernet, InvalidToken

from aiofhir.smart.tokens import TokenSet


class EncryptedStore:
    """One encrypted `TokenSet` per subject, written into any bytes mapping.

    The default backend is an in-process dict. Pass a mapping backed by Redis or
    a database to survive restarts or share sessions between workers. Keys are
    an HMAC of the subject, so the backend never sees the subject either.
    """

    def __init__(self, key: bytes | str, backend: MutableMapping[str, bytes] | None = None) -> None:
        self._fernet = Fernet(key)
        raw = key.encode() if isinstance(key, str) else key
        self._key_hmac = hashlib.sha256(b"aiofhir-subject-key:" + raw).digest()
        self._backend: MutableMapping[str, bytes] = {} if backend is None else backend

    def get(self, subject: str) -> TokenSet | None:
        blob = self._backend.get(self._slot(subject))
        if blob is None:
            return None
        try:
            data = json.loads(self._fernet.decrypt(blob))
        except InvalidToken:
            return None
        return TokenSet.from_dict(data)

    def put(self, subject: str, tokens: TokenSet) -> None:
        self._backend[self._slot(subject)] = self._fernet.encrypt(
            json.dumps(tokens.to_dict()).encode()
        )

    def delete(self, subject: str) -> None:
        self._backend.pop(self._slot(subject), None)

    def _slot(self, subject: str) -> str:
        return hmac.new(self._key_hmac, subject.encode(), hashlib.sha256).hexdigest()
