"""Declarative base shared by all SQLAlchemy models.

Models (added in T0.4, all in ``axon/db/models.py``) must inherit from
:class:`Base` so that ``Base.metadata`` is the single metadata registry —
Alembic autogenerate and any bootstrap ``create_all`` both key off it.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint/index names. Without this, Postgres auto-generates
# names and Alembic migrations can't reliably drop or alter constraints later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root declarative base for every Axon model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
