"""Additive migration: statement file nullability, source, guarded downgrade.

Revision ID: 0002_statement_source
Revises: 0001_postgresql_baseline

Upgrade: adds non-null ``source`` (pdf/api, CHECK ``ck_statements_source``),
backfills historical rows as ``pdf``, keeps the server default so old PDF
writers keep working, and drops NOT NULL on file_path/file_hash. Existing
file values, statuses, errors and timestamps are untouched; per-card
non-null hash uniqueness is preserved.

Downgrade: guarded. Restoring NOT NULL file constraints is only safe while
every statement is ``source='pdf'`` with non-null file metadata. With any
incompatible row the downgrade fails before any DDL or stamp change; it
never deletes rows, fabricates file paths/hashes or discards provenance."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context as _migration_context
from alembic import op

revision: str = "0002_statement_source"
down_revision: str | Sequence[str] | None = "0001_postgresql_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INCOMPATIBLE_STATEMENTS_SQL = (
    "SELECT count(*) FROM statements "
    "WHERE source <> 'pdf' OR file_path IS NULL OR file_hash IS NULL"
)

# SQL form of the refusal guard. Used verbatim in offline mode, where no
# live connection exists, so the emitted script carries the same lock and
# guard as the online path instead of relying on Python-only inspection.
_OFFLINE_LOCK_SQL = "LOCK TABLE statements IN ACCESS EXCLUSIVE MODE"
_OFFLINE_GUARD_SQL = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM statements "
    "WHERE source <> 'pdf' OR file_path IS NULL OR file_hash IS NULL) THEN "
    "RAISE EXCEPTION 'Cannot downgrade: incompatible statements (source <> "
    "pdf or null file_path/file_hash) exist'; END IF; END $$;"
)


def _refuse_incompatible_statements(connection: sa.Connection) -> None:
    """Raise before any DDL when API/null-file statements exist."""
    incompatible = connection.execute(sa.text(_INCOMPATIBLE_STATEMENTS_SQL)).scalar_one()
    if incompatible:
        raise RuntimeError(
            f"Cannot downgrade: {incompatible} incompatible statements "
            "(source <> 'pdf' or null file_path/file_hash) exist. "
            "Downgrade is refused to avoid deleting data, fabricating file "
            "metadata or discarding provenance. Remove or migrate these rows "
            "via an explicitly authorized data-compatibility plan first."
        )


def upgrade() -> None:
    """Add source with pdf backfill/default and make file metadata nullable."""
    op.add_column(
        "statements",
        sa.Column(
            "source",
            sa.String(length=3),
            nullable=False,
            server_default=sa.text("'pdf'"),
        ),
    )
    # Backfill is a no-op for rows inserted after the ADD COLUMN (the server
    # default already stamped them) but is kept explicit for the DDL
    # transaction's documented contract.
    op.execute("UPDATE statements SET source = 'pdf' WHERE source IS NULL")
    op.create_check_constraint(
        "ck_statements_source",
        "statements",
        "source IN ('pdf', 'api')",
    )
    op.alter_column("statements", "file_path", existing_type=sa.String(512), nullable=True)
    op.alter_column("statements", "file_hash", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    """Restore file NOT NULL constraints only when no incompatible rows exist.

    Targets only ``0001_postgresql_baseline`` — never ``base`` (which
    would drop the financial tables). The refusal guard runs inside the
    same DDL transaction, so a refusal leaves schema, revision stamp and
    data unchanged.
    """
    connection = op.get_bind()
    if _migration_context.is_offline_mode():
        # Offline: emit the equivalent lock/guard SQL; execution is the
        # operator's responsibility when applying the script.
        op.execute(_OFFLINE_LOCK_SQL)
        op.execute(_OFFLINE_GUARD_SQL)
    else:
        # Lock the table against concurrent statement writes while the
        # guard and the constraint changes run, then guard.
        connection.execute(sa.text(_OFFLINE_LOCK_SQL))
        _refuse_incompatible_statements(connection)

    op.drop_constraint("ck_statements_source", "statements", type_="check")
    op.drop_column("statements", "source")
    op.alter_column("statements", "file_path", existing_type=sa.String(512), nullable=False)
    op.alter_column("statements", "file_hash", existing_type=sa.String(64), nullable=False)
