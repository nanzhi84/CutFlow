from apps.api.routers import secrets as secrets_router
from packages.core.storage.secret_store import LocalSecretStore


def test_local_secret_store_writes_0600_file_and_disables_secret(tmp_path):
    store = LocalSecretStore(root=tmp_path)

    secret_ref = store.put("plain-secret")

    secret_path = tmp_path / secret_ref
    assert secret_path.exists()
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert "plain-secret" not in secret_path.read_text(encoding="utf-8")
    assert store.get(secret_ref) == "plain-secret"

    store.disable(secret_ref)

    assert not secret_path.exists()
    assert store.get(secret_ref) is None


# The create/rotate/disable -> audit-event behaviour (Spec 11.3 / 32.9) is covered
# against a real database by
# tests/integration/test_sqlalchemy_secrets.py::
#   test_db_create_rotate_disable_write_audit_atomically_in_session
# which now runs in the default suite. The former in-memory variant of this test
# was removed together with the memory storage backend.


def test_router_passes_authenticated_actor_into_audit():
    # The router captures require_role's AuthUser and forwards user.id as actor,
    # so audit events attribute the real admin rather than the bare "system".
    import inspect

    source = inspect.getsource(secrets_router)
    assert "actor=user.id" in source
