from __future__ import annotations

import hashlib
import os


def registration_code_salt() -> str:
    return os.getenv("CUTAGENT_REGISTRATION_CODE_SALT", "local-dev-registration-code-salt")


def hash_registration_code(code: str) -> str:
    return hashlib.sha256(f"{code}{registration_code_salt()}".encode("utf-8")).hexdigest()
