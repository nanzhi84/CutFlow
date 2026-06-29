"""SqlAlchemy persistence for the publishing-center account foundation.

Covers clients, publish-account binding anchors, and case→account publish targets.
Platform login/session state is owned by 小V猫 and is never persisted here.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from packages.core.contracts import CasePublishTarget, Client, PublishAccount
from packages.core.storage.database import (
    CasePublishTargetRow,
    ClientRow,
    PublishAccountRow,
)
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.repository import new_id
from packages.publishing.accounts_mappers import (
    case_publish_target_row_to_contract,
    client_row_to_contract,
    publish_account_row_to_contract,
)


class SqlAlchemyAccountsRepository(BaseRepository):

    # --- clients ---
    def list_clients(self, *, include_archived: bool = False, limit: int = 50) -> list[Client]:
        with self.session_factory() as session:
            stmt = select(ClientRow).order_by(ClientRow.created_at.desc()).limit(limit)
            if not include_archived:
                stmt = stmt.where(ClientRow.status == "active")
            return [client_row_to_contract(row) for row in session.scalars(stmt)]

    def get_client(self, client_id: str) -> Client | None:
        with self.session_factory() as session:
            row = session.get(ClientRow, client_id)
            return client_row_to_contract(row) if row is not None else None

    def client_exists(self, client_id: str) -> bool:
        with self.session_factory() as session:
            return session.get(ClientRow, client_id) is not None

    def create_client(self, *, name: str, remark: str = "") -> Client:
        with self.session_factory() as session:
            row = ClientRow(id=new_id("client"), name=name, remark=remark, status="active")
            session.add(row)
            session.commit()
            session.refresh(row)
            return client_row_to_contract(row)

    def patch_client(
        self,
        client_id: str,
        *,
        name: str | None = None,
        remark: str | None = None,
        status: str | None = None,
    ) -> Client | None:
        with self.session_factory() as session:
            row = session.get(ClientRow, client_id)
            if row is None:
                return None
            if name is not None:
                row.name = name
            if remark is not None:
                row.remark = remark
            if status is not None:
                row.status = status
            session.commit()
            session.refresh(row)
            return client_row_to_contract(row)

    # --- accounts ---
    def list_accounts(
        self,
        *,
        client_id: str | None = None,
        platform: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> list[PublishAccount]:
        with self.session_factory() as session:
            stmt = select(PublishAccountRow).order_by(PublishAccountRow.created_at.desc()).limit(limit)
            if client_id is not None:
                stmt = stmt.where(PublishAccountRow.client_id == client_id)
            if platform is not None:
                stmt = stmt.where(PublishAccountRow.platform == platform)
            if not include_archived:
                stmt = stmt.where(PublishAccountRow.status == "active")
            return [publish_account_row_to_contract(row) for row in session.scalars(stmt)]

    def get_account(self, account_id: str) -> PublishAccount | None:
        with self.session_factory() as session:
            row = session.get(PublishAccountRow, account_id)
            return publish_account_row_to_contract(row) if row is not None else None

    def find_account_by_natural_key(
        self, *, client_id: str, platform: str, account_name: str
    ) -> PublishAccount | None:
        with self.session_factory() as session:
            stmt = select(PublishAccountRow).where(
                PublishAccountRow.client_id == client_id,
                PublishAccountRow.platform == platform,
                PublishAccountRow.account_name == account_name,
            )
            row = session.scalars(stmt).first()
            return publish_account_row_to_contract(row) if row is not None else None

    def create_account(
        self,
        *,
        client_id: str,
        platform: str,
        account_name: str,
        platform_uid: str | None = None,
        xiaovmao_uid: str | None = None,
    ) -> PublishAccount:
        with self.session_factory() as session:
            row = PublishAccountRow(
                id=new_id("pubacct"),
                client_id=client_id,
                platform=platform,
                account_name=account_name,
                platform_uid=platform_uid,
                xiaovmao_uid=xiaovmao_uid,
                status="active",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return publish_account_row_to_contract(row)

    def patch_account(
        self,
        account_id: str,
        *,
        account_name: str | None = None,
        platform_uid: str | None = None,
        platform_uid_set: bool = False,
        xiaovmao_uid: str | None = None,
        xiaovmao_uid_set: bool = False,
        status: str | None = None,
    ) -> PublishAccount | None:
        with self.session_factory() as session:
            row = session.get(PublishAccountRow, account_id)
            if row is None:
                return None
            if account_name is not None:
                row.account_name = account_name
            if platform_uid_set:
                row.platform_uid = platform_uid
            if xiaovmao_uid_set:
                row.xiaovmao_uid = xiaovmao_uid
            if status is not None:
                row.status = status
            session.commit()
            session.refresh(row)
            return publish_account_row_to_contract(row)

    def accounts_client_map(self, account_ids: list[str]) -> dict[str, str]:
        """Return ``{account_id: client_id}`` for the given accounts (same-client check)."""
        if not account_ids:
            return {}
        with self.session_factory() as session:
            stmt = select(PublishAccountRow.id, PublishAccountRow.client_id).where(
                PublishAccountRow.id.in_(account_ids),
                PublishAccountRow.status == "active",
            )
            return {account_id: client_id for account_id, client_id in session.execute(stmt)}

    # --- targets ---
    def list_targets(self, case_id: str) -> list[CasePublishTarget]:
        with self.session_factory() as session:
            return self._targets_for_case(session, case_id)

    def set_targets(self, case_id: str, account_ids: list[str]) -> list[CasePublishTarget]:
        """Idempotently replace the full target set for a case."""
        wanted = list(dict.fromkeys(account_ids))  # de-dupe, preserve order
        with self.session_factory() as session:
            existing = {
                row.account_id: row
                for row in session.scalars(
                    select(CasePublishTargetRow).where(CasePublishTargetRow.case_id == case_id)
                )
            }
            for account_id, row in existing.items():
                if account_id not in wanted:
                    session.delete(row)
            for account_id in wanted:
                if account_id not in existing:
                    session.add(
                        CasePublishTargetRow(
                            id=new_id("target"), case_id=case_id, account_id=account_id, enabled=True
                        )
                    )
            session.commit()
            return self._targets_for_case(session, case_id)

    def delete_targets_for_account(self, account_id: str) -> None:
        with self.session_factory() as session:
            session.execute(
                delete(CasePublishTargetRow).where(CasePublishTargetRow.account_id == account_id)
            )
            session.commit()

    def _targets_for_case(self, session: Session, case_id: str) -> list[CasePublishTarget]:
        rows = session.scalars(
            select(CasePublishTargetRow)
            .where(CasePublishTargetRow.case_id == case_id)
            .order_by(CasePublishTargetRow.created_at.asc())
        )
        out: list[CasePublishTarget] = []
        for row in rows:
            account = session.get(PublishAccountRow, row.account_id)
            out.append(case_publish_target_row_to_contract(row, account))
        return out
