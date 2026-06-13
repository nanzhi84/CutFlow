from __future__ import annotations

import base64
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from packages.core.config import build_settings


class SecretStore(Protocol):
    def put(self, plaintext: str, *, secret_ref: str | None = None) -> str:
        ...

    def get(self, secret_ref: str) -> str | None:
        ...

    def disable(self, secret_ref: str) -> None:
        ...


def local_dev_secret_envelope(value: str) -> str:
    return "dev+base64:" + base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def open_local_dev_secret_envelope(value: str) -> str:
    prefix = "dev+base64:"
    if not value.startswith(prefix):
        raise ValueError("Unsupported local secret envelope.")
    return base64.urlsafe_b64decode(value[len(prefix) :].encode("ascii")).decode("utf-8")


class LocalSecretStore:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root or build_settings().secret_store.dir)

    def put(self, plaintext: str, *, secret_ref: str | None = None) -> str:
        ref = secret_ref or f"sec_{uuid4().hex}.secret"
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(local_dev_secret_envelope(plaintext), encoding="utf-8")
        path.chmod(0o600)
        return ref

    def get(self, secret_ref: str) -> str | None:
        path = self._path(secret_ref)
        if not path.exists():
            return None
        return open_local_dev_secret_envelope(path.read_text(encoding="utf-8"))

    def disable(self, secret_ref: str) -> None:
        path = self._path(secret_ref)
        if path.exists():
            path.unlink()

    def _path(self, secret_ref: str) -> Path:
        if "/" in secret_ref or "\\" in secret_ref:
            raise ValueError("secret_ref must be a file name, not a path.")
        return self.root / secret_ref
