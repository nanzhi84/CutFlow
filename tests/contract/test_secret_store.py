from pathlib import Path

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
