from __future__ import annotations

import hashlib

from packages.core.config import build_settings


def registration_code_salt() -> str:
    return build_settings().auth.registration_code_salt


def hash_registration_code(code: str) -> str:
    return hashlib.sha256(f"{code}{registration_code_salt()}".encode("utf-8")).hexdigest()
