"""initial

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-21 19:30:00.000000

Empty placeholder migration. Alembic needs at least one revision to
exist so it can create the ``alembic_version`` tracking table. Domain
tables will be introduced in later phases; this file exists so
``alembic upgrade head`` is a no-op today and produces a fully
versioned database.

Why both ``upgrade`` and ``downgrade`` are empty
------------------------------------------------
Alembic runs a small amount of bookkeeping *after* each migration's
body: it ``DELETE``s the row it just stamped (on downgrade) or
``INSERT``s the new row (on upgrade). If a migration drops the
``alembic_version`` table from its own ``downgrade`` body, the
post-migration cleanup fails because the table no longer exists.
The cleanest round-trip is therefore a true no-op body: the table
stays put, the row is removed by Alembic itself, and the database
ends up with an empty (but present) ``alembic_version`` table —
which is the standard state for a freshly-downgraded database.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stamp the database with the initial revision (no schema changes)."""
    # Alembic inserts the version row automatically after the body runs.
    pass


def downgrade() -> None:
    """Reverse the initial stamp (no schema changes to undo)."""
    # Alembic deletes the version row automatically after the body runs.
    pass
