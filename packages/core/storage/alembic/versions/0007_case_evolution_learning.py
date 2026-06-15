from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0007_case_evolution_learning"
down_revision = "0006_media_thumbnail_duration"
branch_labels = None
depends_on = None


# Cluster PR3: Case 自进化闭环 (PerformanceScore + feature extraction + memory
# recall/proposal + reflection + §25.4 metrics 回流). Adds:
#   - recall/validity/scope columns to case_memories + memory_proposals
#   - lineage columns to performance_observations (§25.1) + reflection_runs (§8.3)
#   - canonical metric columns to performance_observations (§8.3)
#   - new tables: creative_feature_vectors (§25.5), performance_scores (§25.6),
#     case_knowledge_items (§25.8)
#
# Idempotent: 0001_initial_schema bootstraps via Base.metadata.create_all(), so
# fresh DBs already have the new columns/tables. Inspect first so this migration
# is a no-op there but still applies on pre-existing databases.

_MEMORY_COLUMNS = (
    ("memory_type", sa.String(), False, "'script_pattern'"),
    ("scope_key", sa.String(), True, None),
    ("sample_size", sa.Integer(), False, "0"),
    ("supersedes_memory_id", sa.String(), True, None),
)
_CASE_MEMORY_COLUMNS = _MEMORY_COLUMNS + (
    ("valid_from", sa.DateTime(timezone=True), True, None),
    ("valid_until", sa.DateTime(timezone=True), True, None),
)
_PROPOSAL_COLUMNS = _MEMORY_COLUMNS

_OBSERVATION_COLUMNS = (
    ("video_version_id", sa.String(), True, None),
    ("platform", sa.String(), True, None),
    ("account_id", sa.String(), True, None),
    ("window", sa.String(), True, None),
    ("impressions", sa.Integer(), True, None),
    ("views", sa.Integer(), True, None),
    ("avg_watch_sec", sa.Float(), True, None),
    ("completion_rate", sa.Float(), True, None),
    ("like_rate", sa.Float(), True, None),
    ("comment_rate", sa.Float(), True, None),
    ("share_rate", sa.Float(), True, None),
    ("follow_rate", sa.Float(), True, None),
    ("conversion_count", sa.Integer(), True, None),
    ("conversion_rate", sa.Float(), True, None),
    ("raw_metrics", JSONB(), False, "'{}'::jsonb"),
)

_REFLECTION_COLUMNS = (
    ("input_observation_ids", ARRAY(sa.String()), False, "{}"),
    ("input_feature_vector_ids", ARRAY(sa.String()), False, "{}"),
    ("memory_proposal_ids", ARRAY(sa.String()), False, "{}"),
    ("sample_size", sa.Integer(), False, "0"),
)


def _add_missing_columns(bind, table: str, columns) -> None:
    existing = {col["name"] for col in sa.inspect(bind).get_columns(table)}
    for name, type_, nullable, server_default in columns:
        if name in existing:
            continue
        op.add_column(
            table,
            sa.Column(name, type_, nullable=nullable, server_default=server_default),
        )


def _drop_columns(bind, table: str, columns) -> None:
    existing = {col["name"] for col in sa.inspect(bind).get_columns(table)}
    for name, *_ in columns:
        if name in existing:
            op.drop_column(table, name)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    _add_missing_columns(bind, "case_memories", _CASE_MEMORY_COLUMNS)
    _add_missing_columns(bind, "memory_proposals", _PROPOSAL_COLUMNS)
    _add_missing_columns(bind, "performance_observations", _OBSERVATION_COLUMNS)
    _add_missing_columns(bind, "reflection_runs", _REFLECTION_COLUMNS)

    if "creative_feature_vectors" not in existing_tables:
        op.create_table(
            "creative_feature_vectors",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "case_id",
                sa.String(),
                sa.ForeignKey("cases.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("script_version_id", sa.String(), nullable=True),
            sa.Column("video_version_id", sa.String(), nullable=True),
            sa.Column("hook_type", sa.String(), nullable=True),
            sa.Column("script_structure", sa.String(), nullable=True),
            sa.Column("topic_tags", ARRAY(sa.String()), nullable=False, server_default="{}"),
            sa.Column("cta_type", sa.String(), nullable=True),
            sa.Column("angle", sa.String(), nullable=True),
            sa.Column("duration_sec", sa.Float(), nullable=True),
            sa.Column("broll_density", sa.Float(), nullable=True),
            sa.Column("cut_density", sa.Float(), nullable=True),
            sa.Column("subtitle_style_id", sa.String(), nullable=True),
            sa.Column("bgm_id", sa.String(), nullable=True),
            sa.Column("cover_style", sa.String(), nullable=True),
            sa.Column("material_ids", ARRAY(sa.String()), nullable=False, server_default="{}"),
            sa.Column("broll_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("title_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("idx_feature_vectors_case", "creative_feature_vectors", ["case_id"])
        op.create_index("idx_feature_vectors_video", "creative_feature_vectors", ["video_version_id"])

    if "performance_scores" not in existing_tables:
        op.create_table(
            "performance_scores",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("observation_id", sa.String(), nullable=False),
            sa.Column(
                "case_id",
                sa.String(),
                sa.ForeignKey("cases.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("video_version_id", sa.String(), nullable=True),
            sa.Column("platform", sa.String(), nullable=True),
            sa.Column("account_id", sa.String(), nullable=True),
            sa.Column("window", sa.String(), nullable=False),
            sa.Column("primary_metric", sa.String(), nullable=False),
            sa.Column("normalized_score", sa.Float(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("excluded_reason", sa.String(), nullable=True),
            sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("idx_performance_scores_case", "performance_scores", ["case_id", "window"])
        op.create_index("idx_performance_scores_observation", "performance_scores", ["observation_id"])

    if "case_knowledge_items" not in existing_tables:
        op.create_table(
            "case_knowledge_items",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column(
                "case_id",
                sa.String(),
                sa.ForeignKey("cases.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("ref_id", sa.String(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("tags", ARRAY(sa.String()), nullable=False, server_default="{}"),
            sa.Column("embedding_ref", sa.String(), nullable=True),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("schema_version", sa.String(length=16), nullable=False, server_default="v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("idx_knowledge_items_case_kind", "case_knowledge_items", ["case_id", "kind"])

    _create_index_if_missing(bind, "idx_case_memories_case_type", "case_memories", ["case_id", "memory_type"])
    _create_index_if_missing(bind, "idx_performance_video", "performance_observations", ["video_version_id"])


def _create_index_if_missing(bind, name: str, table: str, columns) -> None:
    existing = {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    for table in ("case_knowledge_items", "performance_scores", "creative_feature_vectors"):
        if table in existing_tables:
            op.drop_table(table)
    _drop_columns(bind, "reflection_runs", _REFLECTION_COLUMNS)
    _drop_columns(bind, "performance_observations", _OBSERVATION_COLUMNS)
    _drop_columns(bind, "memory_proposals", _PROPOSAL_COLUMNS)
    _drop_columns(bind, "case_memories", _CASE_MEMORY_COLUMNS)
