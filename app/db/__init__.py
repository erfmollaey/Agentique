"""Data layer: declarative base, ORM models, engine and session ownership.

Phase 2 § 6 boundary. Nothing outside this package constructs an engine, and
nothing outside :mod:`app.repositories` writes SQL.
"""

from app.db.base import Base
from app.db.models import (
    CitationModel,
    ClaimModel,
    ConversationModel,
    MessageModel,
    ResearchRunModel,
    SourceChunkModel,
    SourceModel,
    UserModel,
    new_turn_id,
)
from app.db.session import Database, DatabaseMisconfiguredError, resolve_database_url

__all__ = [
    "Base",
    "CitationModel",
    "ClaimModel",
    "ConversationModel",
    "Database",
    "DatabaseMisconfiguredError",
    "MessageModel",
    "ResearchRunModel",
    "SourceChunkModel",
    "SourceModel",
    "UserModel",
    "new_turn_id",
    "resolve_database_url",
]
