from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004_artifacts_run_index"
down_revision = "0003_selection_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001_initial_schema bootstraps the whole current schema via
    # Base.metadata.create_all(), which already creates these indexes on fresh
    # databases (both are declared on ArtifactRow). Guard each so this migration is
    # a no-op there, while still applying on databases provisioned before the
    # indexes were added to the model.
    bind = op.get_bind()
    existing_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("artifacts")}
    if "idx_artifacts_run" not in existing_indexes:
        op.create_index("idx_artifacts_run", "artifacts", ["run_id"])
    if "idx_artifacts_run_kind" not in existing_indexes:
        op.create_index("idx_artifacts_run_kind", "artifacts", ["run_id", "kind"])


def downgrade() -> None:
    op.drop_index("idx_artifacts_run_kind", table_name="artifacts")
    op.drop_index("idx_artifacts_run", table_name="artifacts")
