"""SQLAlchemy ORM models.

Re-exports the project's declarative :class:`Base`, the reusable
mixins, and every domain model so callers can import everything from
a single namespace::

    from app.models import Base, UUIDMixin, TimestampMixin
    from app.models import Bank, CreditCard, Statement, Transaction

Importing this package has the side effect of registering every
model with :data:`Base.metadata`, which is what Alembic reads to
generate migrations. Anything that uses the ORM (services,
repositories, route handlers) should import the model classes
through this module rather than reaching into the per-file modules
directly.
"""

from app.models.bank import Bank
from app.models.base import Base
from app.models.credit_card import CreditCard
from app.models.mixins import TimestampMixin, UUIDMixin
from app.models.statement import Statement, StatementStatus
from app.models.transaction import Transaction

__all__ = [
    "Bank",
    "Base",
    "CreditCard",
    "Statement",
    "StatementStatus",
    "TimestampMixin",
    "Transaction",
    "UUIDMixin",
]
