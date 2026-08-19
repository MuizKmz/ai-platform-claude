"""ORM models. Importing this package registers every table on Base.metadata,
which is what Alembic autogenerate walks."""

from app.db.models.chunk import Chunk
from app.db.models.conversation import Conversation, ConversationMessage
from app.db.models.document import Document
from app.db.models.tenant import Tenant
from app.db.models.trace_span import TraceSpan
from app.db.models.user import User

__all__ = [
    "Chunk",
    "Conversation",
    "ConversationMessage",
    "Document",
    "Tenant",
    "TraceSpan",
    "User",
]
