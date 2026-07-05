from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0037_clip_emb_vector_hnsw"
down_revision = "0036_clip_embedding_index"
branch_labels = None
depends_on = None

_TABLE = "clip_embedding_index"
_COLUMN = "embedding"
_HNSW_INDEX = "idx_clip_embedding_embedding_hnsw"


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _column_type(table: str, column: str) -> str | None:
    return op.get_bind().execute(
        sa.text(
            """
            select format_type(a.atttypid, a.atttypmod)
            from pg_attribute a
            join pg_class c on c.oid = a.attrelid
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = current_schema()
              and c.relname = :table
              and a.attname = :column
              and not a.attisdropped
            """
        ),
        {"table": table, "column": column},
    ).scalar_one_or_none()


def _bad_jsonb_vector_count() -> int:
    return int(
        op.get_bind().execute(
            sa.text(
                """
                select count(*)
                from clip_embedding_index
                where embedding_dimension <> 1024
                   or jsonb_typeof(embedding) <> 'array'
                   or jsonb_array_length(embedding) <> 1024
                """
            )
        ).scalar_one()
    )


def upgrade() -> None:
    if not _has_table(_TABLE):
        return
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    current_type = _column_type(_TABLE, _COLUMN)
    if current_type != "vector(1024)":
        if current_type != "jsonb":
            raise RuntimeError(f"unsupported {_TABLE}.{_COLUMN} type for migration: {current_type}")
        bad_rows = _bad_jsonb_vector_count()
        if bad_rows:
            raise RuntimeError(
                f"cannot migrate {_TABLE}.{_COLUMN} to vector(1024): {bad_rows} invalid rows"
            )
        op.execute(
            sa.text(
                """
                alter table clip_embedding_index
                alter column embedding type vector(1024)
                using embedding::text::vector(1024)
                """
            )
        )
    op.execute(
        sa.text(
            """
            create index if not exists idx_clip_embedding_embedding_hnsw
            on clip_embedding_index
            using hnsw (embedding vector_cosine_ops)
            with (m = 16, ef_construction = 64)
            """
        )
    )


def downgrade() -> None:
    if not _has_table(_TABLE):
        return
    op.execute(sa.text(f"drop index if exists {_HNSW_INDEX}"))
    current_type = _column_type(_TABLE, _COLUMN)
    if current_type == "jsonb":
        return
    if current_type != "vector(1024)":
        raise RuntimeError(f"unsupported {_TABLE}.{_COLUMN} type for downgrade: {current_type}")
    op.execute(
        sa.text(
            """
            alter table clip_embedding_index
            alter column embedding type jsonb
            using to_jsonb(embedding::real[])
            """
        )
    )
