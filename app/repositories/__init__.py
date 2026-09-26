"""Repositories own every SQL statement in the application.

The service layer depends on the protocols in :mod:`app.domain.ports`; these
classes are the only place that knows SQLAlchemy exists. Statements are built
through the ORM or ``sqlalchemy`` expressions, never by string concatenation
(SR-10).
"""

from app.repositories.conversations import ConversationRepositoryImpl
from app.repositories.messages import MessageRepositoryImpl
from app.repositories.users import UserRepositoryImpl

__all__ = ["ConversationRepositoryImpl", "MessageRepositoryImpl", "UserRepositoryImpl"]
