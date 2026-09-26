"""Data layer: declarative base, ORM models, engine and session ownership.

Phase 2 § 6 boundary. Nothing outside this package constructs an engine, and
nothing outside :mod:`app.repositories` writes SQL.
"""

from app.db.base import Base
from app.db.models import ConversationModel, MessageModel, UserModel, new_turn_id
from app.db.session import Database, DatabaseMisconfiguredError, resolve_database_url

__all__ = [
    "Base",
    "ConversationModel",
    "Database",
    "DatabaseMisconfiguredError",
    "MessageModel",
    "UserModel",
    "new_turn_id",
    "resolve_database_url",
]
