"""Accounts repository CRUD against real Postgres (no HTTP)."""

from __future__ import annotations

from packages.publishing import SqlAlchemyAccountsRepository

# ``case_publish_targets.case_id`` has a NOT NULL FK to ``cases.id``; the seeded
# baseline ships exactly one case (``case_demo``), so target tests anchor to it.
CASE_ID = "case_demo"


def test_client_account_crud_roundtrip_with_xiaovmao_anchor(db_session_factory):
    repo = SqlAlchemyAccountsRepository(db_session_factory)
    client = repo.create_client(name="ACME", remark="vip")
    assert client.status == "active"
    assert repo.client_exists(client.id)

    account = repo.create_account(
        client_id=client.id,
        platform="douyin",
        account_name="acme-dy",
        platform_uid="dy-uid",
        xiaovmao_uid="xvm-uid",
    )
    assert account.client_id == client.id
    assert account.platform_uid == "dy-uid"
    assert account.xiaovmao_uid == "xvm-uid"
    assert account.login_state == "unknown"

    assert [a.id for a in repo.list_accounts(client_id=client.id)] == [account.id]
    assert repo.list_accounts(client_id=client.id, platform="kuaishou") == []


def test_natural_key_lookup(db_session_factory):
    repo = SqlAlchemyAccountsRepository(db_session_factory)
    client = repo.create_client(name="ACME")
    repo.create_account(client_id=client.id, platform="douyin", account_name="a")
    assert (
        repo.find_account_by_natural_key(client_id=client.id, platform="douyin", account_name="a")
        is not None
    )
    assert (
        repo.find_account_by_natural_key(client_id=client.id, platform="douyin", account_name="b")
        is None
    )


def test_patch_account_can_clear_platform_and_xiaovmao_uids(db_session_factory):
    repo = SqlAlchemyAccountsRepository(db_session_factory)
    client = repo.create_client(name="ACME")
    account = repo.create_account(
        client_id=client.id,
        platform="douyin",
        account_name="a",
        platform_uid="platform-uid",
        xiaovmao_uid="xvm-uid",
    )

    patched = repo.patch_account(
        account.id,
        platform_uid=None,
        platform_uid_set=True,
        xiaovmao_uid=None,
        xiaovmao_uid_set=True,
    )

    assert patched is not None
    assert patched.platform_uid is None
    assert patched.xiaovmao_uid is None


def test_targets_replace_is_idempotent_and_hydrated(db_session_factory):
    repo = SqlAlchemyAccountsRepository(db_session_factory)
    client = repo.create_client(name="ACME")
    a1 = repo.create_account(client_id=client.id, platform="douyin", account_name="a1")
    a2 = repo.create_account(client_id=client.id, platform="kuaishou", account_name="a2")

    repo.set_targets(CASE_ID, [a1.id, a2.id])
    targets = {t.account_id: t for t in repo.list_targets(CASE_ID)}
    assert set(targets) == {a1.id, a2.id}
    assert targets[a1.id].platform == "douyin"
    assert targets[a1.id].client_id == client.id

    repo.set_targets(CASE_ID, [a1.id])  # idempotent replace to subset
    assert {t.account_id for t in repo.list_targets(CASE_ID)} == {a1.id}


def test_archived_account_excluded_from_client_map_and_targets_cleaned(db_session_factory):
    repo = SqlAlchemyAccountsRepository(db_session_factory)
    client = repo.create_client(name="ACME")
    a1 = repo.create_account(client_id=client.id, platform="douyin", account_name="a1")
    repo.set_targets(CASE_ID, [a1.id])
    assert {t.account_id for t in repo.list_targets(CASE_ID)} == {a1.id}

    repo.patch_account(a1.id, status="archived")
    repo.delete_targets_for_account(a1.id)
    assert repo.list_targets(CASE_ID) == []
    assert repo.accounts_client_map([a1.id]) == {}  # archived account can't be re-bound
