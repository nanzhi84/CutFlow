from __future__ import annotations

from argon2 import PasswordHasher, Type

from packages.core.contracts import UserRole
from packages.core.registration_codes import hash_registration_code


ROLE_RANK = {
    UserRole.viewer: 10,
    UserRole.operator: 20,
    UserRole.admin: 30,
}


def create_password_hasher() -> PasswordHasher:
    return PasswordHasher(type=Type.ID, time_cost=1, memory_cost=1024, parallelism=1)


__all__ = ["ROLE_RANK", "create_password_hasher", "hash_registration_code"]
