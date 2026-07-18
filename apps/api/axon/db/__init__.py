"""Database layer: declarative base, engine/session management, migrations.

Importing this package registers every model on ``Base.metadata`` — services,
Alembic, and ``create_all`` can rely on ``import axon.db`` for full schema
discovery.
"""

from axon.db.base import Base
from axon.db import models

__all__ = ["Base", "models"]
