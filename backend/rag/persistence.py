"""Durable chat history and user memory persistence.

PostgreSQL is the production system of record (``RAG_DATABASE_URL``).  The
repository also supports SQLite so the offline test suite and a zero-service
developer checkout remain usable.  Every read and mutation is scoped by both
tenant and user; caller-supplied conversation IDs are never sufficient on
their own.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import get_config
from .schemas import RagAnswer, UserContext


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def database_url() -> str:
    configured = os.environ.get("RAG_DATABASE_URL")
    if configured:
        return configured
    path = Path(get_config().paths.data_dir) / "chat_history.db"
    return "sqlite:///" + path.resolve().as_posix()


class Base(DeclarativeBase):
    pass


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New conversation")
    title_auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list["MessageRecord"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_conversations_owner_updated", "tenant_id", "user_id", "updated_at"),
        Index("ix_conversations_purge_after", "purge_after"),
    )


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="complete")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    abstained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    abstention_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback: Mapped[str | None] = mapped_column(String(8), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    conversation: Mapped[ConversationRecord] = relationship(back_populates="messages")
    sources: Mapped[list["MessageSourceRecord"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)


class MessageSourceRecord(Base):
    __tablename__ = "message_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    chunk_id: Mapped[str] = mapped_column(String(256), nullable=False)
    doc_id: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    section_path: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    message: Mapped[MessageRecord] = relationship(back_populates="sources")

    __table_args__ = (
        UniqueConstraint("message_id", "ordinal", name="uq_message_source_ordinal"),
        Index("ix_message_sources_message", "message_id"),
    )


class UserMemoryRecord(Base):
    __tablename__ = "user_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="preference")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_key: Mapped[str] = mapped_column(String(64), nullable=False)
    is_explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    source_conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    source_message_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "normalized_key", name="uq_user_memory_key"),
        Index("ix_user_memories_owner_updated", "tenant_id", "user_id", "updated_at"),
    )


def build_engine(url: str | None = None) -> Engine:
    resolved = url or database_url()
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if resolved.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(resolved, **kwargs)
    if resolved.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


def create_schema(engine: Engine | None = None) -> None:
    """Create tables for isolated repository tests.

    Application startup must use :func:`migrate_schema` so the schema is also
    recorded in Alembic's version table. Keeping this small helper is useful
    for unit tests that supply their own in-memory engine.
    """
    Base.metadata.create_all(engine or get_engine())


_migration_lock = threading.Lock()
_migration_complete = False


def _legacy_schema_matches_metadata(engine: Engine) -> bool:
    """Return whether an unversioned database contains this exact app schema.

    Older local builds called ``metadata.create_all`` directly, which created
    correct tables but no ``alembic_version`` row. Such databases can safely be
    stamped at the initial revision; incomplete/unknown schemas must fail
    instead of being silently marked current.
    """
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    if not expected.issubset(present):
        return False
    for table_name, table in Base.metadata.tables.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in table.columns}
        # The sole legacy schema predates automatic topic-title metadata. It is
        # safe to stamp at v1 and let the v2 migration add this column.
        if table_name == "conversations":
            expected_columns.discard("title_auto")
        if expected_columns != actual_columns:
            return False
    return True


def migrate_schema() -> None:
    """Upgrade the configured database to the latest Alembic revision.

    The operation is process-idempotent and also adopts databases produced by
    the previous ``create_all`` startup path after verifying their structure.
    """
    global _migration_complete
    if _migration_complete:
        return
    with _migration_lock:
        if _migration_complete:
            return

        from alembic import command
        from alembic.config import Config

        project_root = Path(__file__).resolve().parent.parent
        config = Config(str(project_root / "alembic.ini"))
        config.set_main_option("script_location", str(project_root / "migrations"))

        engine = get_engine()
        tables = set(inspect(engine).get_table_names())
        app_tables = set(Base.metadata.tables)
        if "alembic_version" not in tables and tables & app_tables:
            if not _legacy_schema_matches_metadata(engine):
                raise RuntimeError(
                    "The database has an incomplete unversioned chat schema; "
                    "back it up and run an explicit repair migration."
                )
            command.stamp(config, "20260727_0001")

        command.upgrade(config, "head")
        _migration_complete = True


class NotFoundError(LookupError):
    pass


class ValidationError(ValueError):
    pass


_DEFAULT_TITLES = {"New conversation", "Cuộc trò chuyện mới"}
_TITLE_STOPWORDS = {
    # English chat/request filler.
    "a", "about", "all", "an", "and", "are", "assistant", "can", "chat",
    "conversation", "could", "do", "does", "document", "documents", "entire",
    "everything", "explain",
    "for", "from", "give", "help", "how", "i", "in", "is", "it", "me",
    "my", "of", "on", "please", "say", "show", "summarize", "tell", "that",
    "the", "this", "to", "what", "when", "where", "which", "who", "whole", "why",
    "with", "would", "you", "hello", "hi", "hey", "read", "thanks",
    # Vietnamese chat/request filler.
    "ai", "bạn", "bao", "biết", "bố", "bộ", "các", "cả", "cần", "cho", "chuyện",
    "con", "có", "cuộc", "của", "document", "đi", "đọc", "giúp", "gì", "giờ",
    "hãy", "hết", "hiểu", "không", "là", "lòng", "mày", "mình", "mọi", "một",
    "muốn", "này", "như", "những", "nó", "nói", "ra", "sao", "sẽ", "tài", "tao",
    "tất", "liệu", "thế", "thể", "thứ", "tin", "toàn", "tóm", "tắt", "tôi",
    "trong", "trợ", "và", "về", "vui", "với", "xin", "đó", "được", "ơi", "chào",
    # Generic completion/request scaffolding. Compound forms are normalized
    # from tokenizer underscores before this set is checked.
    "bắt đầu", "hoàn thành", "khi", "làm việc", "phải", "tài liệu",
    "thế nào", "trước", "việc",
}
_TITLE_VAGUE_TERMS = {
    "alo", "hello", "hey", "hi", "help", "ok", "okay", "test", "thanks",
    "chào", "giúp", "xin chào",
}
_TITLE_REQUEST_MARKERS = (
    # English request/question framing.
    "can you ", "could you ", "create ", "explain ", "fix ", "help ",
    "how ", "list ", "make ", "plan ", "please ", "show ", "summarize ",
    "tell ", "what ", "when ", "where ", "which ", "who ", "why ",
    # Vietnamese request/question framing.
    "ai ", "cách ", "giải thích ", "hãy ", "khi nào", "làm sao",
    "làm thế nào", "lập kế hoạch", "liệt kê ", "ở đâu", "quy trình ",
    "tạo ", "tóm tắt ", "vui lòng ",
)
_TITLE_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
_TITLE_WORD_RE = re.compile(r"[^\W_]+(?:[_'-][^\W_]+)*", re.UNICODE)
_TITLE_VI_HINT_RE = re.compile(
    r"[ăâêôơưđáàảãạắằẳẵặấầẩẫậéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợ"
    r"úùủũụứừửữựýỳỷỹỵ]",
    re.I,
)
_title_vi_tokenizer: Any = None
_MEMORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction", re.compile(r"^(?:please\s+)?(?:remember that|remember)\s+(.+)$", re.I)),
    ("instruction", re.compile(r"^(?:hãy\s+)?nhớ(?:\s+rằng)?\s+(.+)$", re.I)),
)
_SENSITIVE_MEMORY = re.compile(
    r"\b(password|passcode|mật\s*khẩu|secret|api[ _-]?key|access[ _-]?token|"
    r"credit[ -]?card|cvv|private[ _-]?key)\b",
    re.I,
)


def extract_user_memories(text: str) -> list[tuple[str, str, bool, float]]:
    """Conservative deterministic extraction.

    It intentionally saves only explicit remember requests. Preferences or
    product/document names mentioned in ordinary chat are never promoted to
    cross-conversation memory. Credential-like content is never persisted.
    """
    value = " ".join((text or "").strip().split())
    if not value or len(value) > 1000 or _SENSITIVE_MEMORY.search(value):
        return []
    found: list[tuple[str, str, bool, float]] = []
    for kind, pattern in _MEMORY_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        content = match.group(1).strip(" .!?\t\r\n")
        if 2 <= len(content) <= 500 and not _SENSITIVE_MEMORY.search(content):
            explicit = kind == "instruction" and bool(re.match(r"^(please\s+)?remember|^(hãy\s+)?nhớ", value, re.I))
            found.append((kind, content, explicit, 1.0 if explicit else 0.9))
            break
    return found


def _memory_key(kind: str, content: str) -> str:
    normalized = " ".join(content.casefold().split())
    return hashlib.sha256(f"{kind}:{normalized}".encode("utf-8")).hexdigest()


def _normalize_title_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"\[Attached[^\]]*\]", " ", text, flags=re.I)
    return " ".join(text.strip().split())


def _title_tokens(text: str) -> list[str]:
    """Return surface-form English or Vietnamese words/compounds."""
    global _title_vi_tokenizer
    tokenized = text
    if _TITLE_VI_HINT_RE.search(text):
        if _title_vi_tokenizer is None:
            try:
                from pyvi import ViTokenizer  # type: ignore

                _title_vi_tokenizer = ViTokenizer.tokenize
            except Exception:
                _title_vi_tokenizer = False
        if _title_vi_tokenizer:
            try:
                tokenized = _title_vi_tokenizer(text)
            except Exception:
                tokenized = text
    return _TITLE_WORD_RE.findall(tokenized)


def _title_content(text: str) -> list[tuple[str, str]]:
    tokens = _title_tokens(text)
    normalized_tokens = [token.replace("_", " ").casefold() for token in tokens]
    content: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        display = token.replace("_", " ")
        normalized = normalized_tokens[index]
        # Drop generic instructions while preserving meaningful neighboring
        # phrases such as "xu hướng" and reducing "lập kế hoạch" to the noun.
        if (
            (normalized == "hướng" and index + 1 < len(tokens) and normalized_tokens[index + 1] == "dẫn")
            or (normalized == "dẫn" and index > 0 and normalized_tokens[index - 1] == "hướng")
            or (normalized == "lập" and index + 1 < len(tokens) and normalized_tokens[index + 1] == "kế hoạch")
        ):
            continue
        if normalized in _TITLE_STOPWORDS:
            continue
        if len(normalized) < 2 and not normalized.isdigit():
            continue
        content.append((display, normalized))
    return content


def _request_score(sentence: str, index: int) -> tuple[int, int]:
    folded = sentence.casefold().lstrip()
    marker_hits = sum(marker in folded for marker in _TITLE_REQUEST_MARKERS)
    return (marker_hits * 3 + int("?" in sentence), index)


def _truncate_title(title: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(title) <= max_length:
        return title
    if max_length == 1:
        return "…"
    shortened = title[: max_length - 1].rsplit(" ", 1)[0].rstrip(" ·,;:-")
    return (shortened or title[: max_length - 1].rstrip(" ·,;:-")) + "…"


def generate_topic_title(messages: Iterable[str], *, max_length: int = 80) -> str:
    """Summarize the first substantive user intent without an extra LLM call.

    The first useful request is deliberately authoritative: later, unrelated
    turns must not turn a stable chat-history label into a list of keywords. A
    greeting-only opener may fall through to the next substantive user turn.
    """
    for raw in messages:
        text = _normalize_title_text(raw)
        if not text:
            continue
        sentences = [part.strip() for part in _TITLE_SENTENCE_RE.split(text) if part.strip()]
        if not sentences:
            continue
        sentence_content = [_title_content(sentence) for sentence in sentences]
        all_content = [item for items in sentence_content for item in items]
        if not all_content or all(normalized in _TITLE_VAGUE_TERMS for _, normalized in all_content):
            continue

        request_index = max(
            range(len(sentences)),
            key=lambda index: _request_score(sentences[index], index),
        )
        selected = list(sentence_content[request_index])
        if not selected:
            selected = all_content

        # One compact phrase from the surrounding first-message context often
        # supplies the object of the request (for example, customer data). Keep
        # it secondary and never incorporate a later chat turn.
        surrounding = [
            item
            for index, items in enumerate(sentence_content)
            if index != request_index
            for item in items
        ]
        if surrounding and len(selected) < 8:
            selected.extend(surrounding[-2:])

        unique: list[str] = []
        seen: set[str] = set()
        for display, normalized in selected:
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(display)
            if len(unique) >= 8:
                break
        if not unique:
            continue
        title = " ".join(unique).strip()
        title = title[0].upper() + title[1:]
        return _truncate_title(title, max_length)

    return "Cuộc trò chuyện mới"


class ChatRepository:
    def __init__(self, sessions: sessionmaker[Session] | None = None) -> None:
        self._sessions = sessions or get_session_factory()

    def _owned_conversation(
        self, db: Session, user: UserContext, conversation_id: str, *, include_deleted: bool = False
    ) -> ConversationRecord:
        stmt = select(ConversationRecord).where(
            ConversationRecord.id == conversation_id,
            ConversationRecord.tenant_id == user.tenant_id,
            ConversationRecord.user_id == user.user_id,
        )
        if not include_deleted:
            stmt = stmt.where(ConversationRecord.deleted_at.is_(None))
        record = db.scalar(stmt)
        if record is None:
            raise NotFoundError("Conversation not found.")
        return record

    @staticmethod
    def _user_message_texts(db: Session, conversation_id: str) -> list[str]:
        return list(
            db.scalars(
                select(MessageRecord.content)
                .where(
                    MessageRecord.conversation_id == conversation_id,
                    MessageRecord.role == "user",
                )
                .order_by(MessageRecord.created_at.asc(), MessageRecord.id.asc())
            ).all()
        )

    def _refresh_topic_title(self, db: Session, conversation: ConversationRecord) -> None:
        """Name an untitled automatic conversation, then keep it stable."""
        if not conversation.title_auto or conversation.title not in _DEFAULT_TITLES:
            return
        messages = self._user_message_texts(db, conversation.id)
        if messages:
            conversation.title = generate_topic_title(messages)[:200]

    def backfill_auto_titles(self) -> int:
        """Regenerate automatic titles without touching manual names."""
        changed = 0
        with self._sessions.begin() as db:
            conversations = db.scalars(select(ConversationRecord)).all()
            for conversation in conversations:
                messages = self._user_message_texts(db, conversation.id)
                if not messages:
                    continue
                if not conversation.title_auto:
                    continue
                new_title = generate_topic_title(messages)[:200]
                if conversation.title != new_title or not conversation.title_auto:
                    conversation.title = new_title
                    conversation.title_auto = True
                    changed += 1
        return changed

    def create_conversation(self, user: UserContext, title: str | None = None) -> dict[str, Any]:
        clean_title = (title or "New conversation").strip()[:200] or "New conversation"
        with self._sessions.begin() as db:
            record = ConversationRecord(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                title=clean_title,
                title_auto=title is None or not title.strip(),
            )
            db.add(record)
            db.flush()
            return self._conversation_dict(record)

    def list_conversations(
        self, user: UserContext, *, limit: int = 100, offset: int = 0, deleted: bool = False
    ) -> list[dict[str, Any]]:
        with self._sessions() as db:
            filters = [
                ConversationRecord.tenant_id == user.tenant_id,
                ConversationRecord.user_id == user.user_id,
                ConversationRecord.deleted_at.is_not(None) if deleted else ConversationRecord.deleted_at.is_(None),
            ]
            if deleted:
                filters.append(
                    or_(
                        ConversationRecord.purge_after.is_(None),
                        ConversationRecord.purge_after > utcnow(),
                    )
                )
            stmt = select(ConversationRecord).where(*filters)
            stmt = stmt.order_by(ConversationRecord.pinned.desc(), ConversationRecord.updated_at.desc())
            records = db.scalars(stmt.limit(limit).offset(offset)).all()
            return [self._conversation_dict(r) for r in records]

    def get_conversation(self, user: UserContext, conversation_id: str) -> dict[str, Any]:
        with self._sessions() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            messages = db.scalars(
                select(MessageRecord)
                .where(MessageRecord.conversation_id == conversation.id)
                .order_by(MessageRecord.created_at.asc(), MessageRecord.id.asc())
            ).all()
            source_rows = db.scalars(
                select(MessageSourceRecord)
                .join(MessageRecord, MessageRecord.id == MessageSourceRecord.message_id)
                .where(MessageRecord.conversation_id == conversation.id)
                .order_by(MessageSourceRecord.ordinal.asc())
            ).all()
            by_message: dict[str, list[MessageSourceRecord]] = {}
            for source in source_rows:
                by_message.setdefault(source.message_id, []).append(source)
            result = self._conversation_dict(conversation)
            result["messages"] = [
                self._message_dict(message, by_message.get(message.id, [])) for message in messages
            ]
            return result

    def update_conversation(
        self,
        user: UserContext,
        conversation_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> dict[str, Any]:
        if title is not None and not title.strip():
            raise ValidationError("Conversation title cannot be empty.")
        with self._sessions.begin() as db:
            record = self._owned_conversation(db, user, conversation_id)
            if title is not None:
                record.title = title.strip()[:200]
                record.title_auto = False
            if pinned is not None:
                record.pinned = pinned
            record.updated_at = utcnow()
            db.flush()
            return self._conversation_dict(record)

    def soft_delete_conversation(
        self, user: UserContext, conversation_id: str, *, retention_days: int = 30
    ) -> None:
        if retention_days < 0:
            raise ValidationError("Retention days cannot be negative.")
        now = utcnow()
        with self._sessions.begin() as db:
            record = self._owned_conversation(db, user, conversation_id)
            record.deleted_at = now
            record.purge_after = now + timedelta(days=retention_days)
            record.pinned = False

    def restore_conversation(self, user: UserContext, conversation_id: str) -> dict[str, Any]:
        with self._sessions.begin() as db:
            record = self._owned_conversation(db, user, conversation_id, include_deleted=True)
            if record.deleted_at is None:
                raise ValidationError("Conversation is not deleted.")
            if record.purge_after is not None and _as_utc(record.purge_after) <= utcnow():
                raise NotFoundError("Conversation retention period has expired.")
            record.deleted_at = None
            record.purge_after = None
            record.updated_at = utcnow()
            db.flush()
            return self._conversation_dict(record)

    def purge_deleted(self, *, now: datetime | None = None) -> int:
        cutoff = now or utcnow()
        with self._sessions.begin() as db:
            result = db.execute(
                delete(ConversationRecord).where(
                    ConversationRecord.purge_after.is_not(None),
                    ConversationRecord.purge_after <= cutoff,
                )
            )
            return int(result.rowcount or 0)

    def start_turn(
        self, user: UserContext, conversation_id: str, content: str
    ) -> dict[str, dict[str, Any]]:
        """Persist a user message and its assistant placeholder atomically."""
        clean = content.strip()
        if not clean:
            raise ValidationError("Message cannot be empty.")
        with self._sessions.begin() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            created_at = utcnow()
            user_message = MessageRecord(
                conversation_id=conversation.id,
                role="user",
                content=clean,
                status="complete",
                created_at=created_at,
            )
            assistant_message = MessageRecord(
                conversation_id=conversation.id,
                role="assistant",
                content="",
                status="streaming",
                created_at=created_at + timedelta(microseconds=1),
            )
            db.add_all([user_message, assistant_message])
            db.flush()
            conversation.updated_at = utcnow()
            user_result = self._message_dict(user_message, [])
            assistant_result = self._message_dict(assistant_message, [])

        self._store_extracted_memories(user, conversation_id, user_message.id, clean)
        return {"user_message": user_result, "assistant_message": assistant_result}

    def complete_turn(
        self,
        user: UserContext,
        conversation_id: str,
        assistant_message_id: str,
        answer: RagAnswer,
    ) -> dict[str, Any]:
        """Replace a streaming placeholder with the durable final answer."""
        with self._sessions.begin() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            message = db.scalar(
                select(MessageRecord).where(
                    MessageRecord.id == assistant_message_id,
                    MessageRecord.conversation_id == conversation.id,
                    MessageRecord.role == "assistant",
                )
            )
            if message is None:
                raise NotFoundError("Assistant message not found.")

            message.content = answer.answer
            message.status = "complete"
            message.trace_id = answer.trace_id
            message.model = answer.model
            message.abstained = answer.abstained
            message.abstention_reason = answer.abstention_reason
            message.extra = {
                **dict(answer.metadata or {}),
                # Keep the exact evidence excerpt with the durable turn so a
                # reopened chat shows the same citation preview as the live
                # stream. Conversation ownership still scopes every read.
                "source_texts": {
                    source.chunk_id: source.text for source in answer.sources
                },
            }
            for source in list(message.sources):
                db.delete(source)
            db.flush()

            sources: list[MessageSourceRecord] = []
            for ordinal, source in enumerate(answer.sources, start=1):
                row = MessageSourceRecord(
                    message_id=message.id,
                    ordinal=ordinal,
                    label=source.label,
                    chunk_id=source.chunk_id,
                    doc_id=source.doc_id,
                    title=source.title,
                    source_uri=source.source_uri,
                    section_path=list(source.section_path),
                    page_number=source.page_number,
                )
                db.add(row)
                sources.append(row)
            conversation.updated_at = utcnow()
            self._refresh_topic_title(db, conversation)
            db.flush()
            return self._message_dict(message, sources)

    def regeneration_query(
        self, user: UserContext, conversation_id: str, assistant_message_id: str
    ) -> str:
        """Return the user prompt paired with an owned assistant message.

        This is deliberately read-only: the previous answer remains durable
        until a regenerated answer completes successfully.
        """
        with self._sessions() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            assistant = db.scalar(
                select(MessageRecord).where(
                    MessageRecord.id == assistant_message_id,
                    MessageRecord.conversation_id == conversation.id,
                    MessageRecord.role == "assistant",
                )
            )
            if assistant is None:
                raise NotFoundError("Assistant message not found.")
            user_message = db.scalar(
                select(MessageRecord)
                .where(
                    MessageRecord.conversation_id == conversation.id,
                    MessageRecord.role == "user",
                    MessageRecord.created_at < assistant.created_at,
                )
                .order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
                .limit(1)
            )
            if user_message is None or not user_message.content.strip():
                raise ValidationError("The assistant message has no preceding user prompt.")
            return user_message.content

    def fail_turn(
        self,
        user: UserContext,
        conversation_id: str,
        assistant_message_id: str,
        *,
        content: str = "Response generation was interrupted. Please try again.",
    ) -> None:
        """Finalize an unfinished assistant placeholder as an error."""
        with self._sessions.begin() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            message = db.scalar(
                select(MessageRecord).where(
                    MessageRecord.id == assistant_message_id,
                    MessageRecord.conversation_id == conversation.id,
                    MessageRecord.role == "assistant",
                )
            )
            if message is None:
                raise NotFoundError("Assistant message not found.")
            if message.status == "complete":
                return
            message.status = "error"
            message.content = content
            conversation.updated_at = utcnow()
            self._refresh_topic_title(db, conversation)

    def append_user_message(
        self, user: UserContext, conversation_id: str, content: str
    ) -> dict[str, Any]:
        clean = content.strip()
        if not clean:
            raise ValidationError("Message cannot be empty.")
        with self._sessions.begin() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            message = MessageRecord(conversation_id=conversation.id, role="user", content=clean)
            db.add(message)
            db.flush()
            conversation.updated_at = utcnow()
            message_id = message.id
        self._store_extracted_memories(user, conversation_id, message_id, clean)
        return self._message_dict(message, [])

    def append_assistant_message(
        self, user: UserContext, conversation_id: str, answer: RagAnswer
    ) -> dict[str, Any]:
        with self._sessions.begin() as db:
            conversation = self._owned_conversation(db, user, conversation_id)
            message = MessageRecord(
                conversation_id=conversation.id,
                role="assistant",
                content=answer.answer,
                trace_id=answer.trace_id,
                model=answer.model,
                abstained=answer.abstained,
                abstention_reason=answer.abstention_reason,
                extra=dict(answer.metadata or {}),
            )
            db.add(message)
            db.flush()
            sources: list[MessageSourceRecord] = []
            for ordinal, source in enumerate(answer.sources, start=1):
                row = MessageSourceRecord(
                    message_id=message.id,
                    ordinal=ordinal,
                    label=source.label,
                    chunk_id=source.chunk_id,
                    doc_id=source.doc_id,
                    title=source.title,
                    source_uri=source.source_uri,
                    section_path=list(source.section_path),
                    page_number=source.page_number,
                )
                db.add(row)
                sources.append(row)
            conversation.updated_at = utcnow()
            self._refresh_topic_title(db, conversation)
            db.flush()
            return self._message_dict(message, sources)

    def set_feedback(self, user: UserContext, trace_id: str | None, rating: str) -> None:
        if not trace_id:
            return
        with self._sessions.begin() as db:
            db.execute(
                update(MessageRecord)
                .where(
                    MessageRecord.trace_id == trace_id,
                    MessageRecord.conversation_id.in_(
                        select(ConversationRecord.id).where(
                            ConversationRecord.tenant_id == user.tenant_id,
                            ConversationRecord.user_id == user.user_id,
                        )
                    ),
                )
                .values(feedback=rating)
            )

    def list_memories(self, user: UserContext, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._sessions() as db:
            rows = db.scalars(
                select(UserMemoryRecord)
                .where(
                    UserMemoryRecord.tenant_id == user.tenant_id,
                    UserMemoryRecord.user_id == user.user_id,
                    UserMemoryRecord.deleted_at.is_(None),
                )
                .order_by(UserMemoryRecord.updated_at.desc())
                .limit(limit)
            ).all()
            return [self._memory_dict(row) for row in rows]

    def memory_context(self, user: UserContext, *, limit: int = 20) -> list[str]:
        # Only deliberate user memories are allowed to influence every chat.
        return [
            m["content"] for m in self.list_memories(user, limit=limit)
            if m["is_explicit"]
        ]

    def create_memory(
        self, user: UserContext, content: str, *, kind: str = "preference", explicit: bool = True
    ) -> dict[str, Any]:
        clean = " ".join(content.strip().split())
        if not 2 <= len(clean) <= 500:
            raise ValidationError("Memory must contain between 2 and 500 characters.")
        if _SENSITIVE_MEMORY.search(clean):
            raise ValidationError("Credential-like or secret content cannot be saved as memory.")
        if kind not in {"preference", "fact", "instruction"}:
            raise ValidationError("Invalid memory kind.")
        return self._upsert_memory(user, kind, clean, explicit, 1.0, None, None)

    def update_memory(
        self, user: UserContext, memory_id: str, *, content: str, kind: str
    ) -> dict[str, Any]:
        clean = " ".join(content.strip().split())
        if not 2 <= len(clean) <= 500 or _SENSITIVE_MEMORY.search(clean):
            raise ValidationError("Memory content is invalid or contains secret-like text.")
        if kind not in {"preference", "fact", "instruction"}:
            raise ValidationError("Invalid memory kind.")
        with self._sessions.begin() as db:
            row = db.scalar(
                select(UserMemoryRecord).where(
                    UserMemoryRecord.id == memory_id,
                    UserMemoryRecord.tenant_id == user.tenant_id,
                    UserMemoryRecord.user_id == user.user_id,
                    UserMemoryRecord.deleted_at.is_(None),
                )
            )
            if row is None:
                raise NotFoundError("Memory not found.")
            key = _memory_key(kind, clean)
            duplicate = db.scalar(
                select(UserMemoryRecord.id).where(
                    UserMemoryRecord.tenant_id == user.tenant_id,
                    UserMemoryRecord.user_id == user.user_id,
                    UserMemoryRecord.normalized_key == key,
                    UserMemoryRecord.id != memory_id,
                )
            )
            if duplicate is not None:
                raise ValidationError("An identical memory already exists.")
            row.content = clean
            row.kind = kind
            row.normalized_key = key
            row.updated_at = utcnow()
            db.flush()
            return self._memory_dict(row)

    def delete_memory(self, user: UserContext, memory_id: str) -> None:
        with self._sessions.begin() as db:
            row = db.scalar(
                select(UserMemoryRecord).where(
                    UserMemoryRecord.id == memory_id,
                    UserMemoryRecord.tenant_id == user.tenant_id,
                    UserMemoryRecord.user_id == user.user_id,
                    UserMemoryRecord.deleted_at.is_(None),
                )
            )
            if row is None:
                raise NotFoundError("Memory not found.")
            row.deleted_at = utcnow()
            row.updated_at = utcnow()

    def _store_extracted_memories(
        self, user: UserContext, conversation_id: str, message_id: str, text: str
    ) -> None:
        for kind, content, explicit, confidence in extract_user_memories(text):
            self._upsert_memory(
                user, kind, content, explicit, confidence, conversation_id, message_id
            )

    def _upsert_memory(
        self,
        user: UserContext,
        kind: str,
        content: str,
        explicit: bool,
        confidence: float,
        conversation_id: str | None,
        message_id: str | None,
    ) -> dict[str, Any]:
        key = _memory_key(kind, content)
        with self._sessions.begin() as db:
            row = db.scalar(
                select(UserMemoryRecord).where(
                    UserMemoryRecord.tenant_id == user.tenant_id,
                    UserMemoryRecord.user_id == user.user_id,
                    UserMemoryRecord.normalized_key == key,
                )
            )
            if row is None:
                try:
                    with db.begin_nested():
                        row = UserMemoryRecord(
                            tenant_id=user.tenant_id,
                            user_id=user.user_id,
                            kind=kind,
                            content=content,
                            normalized_key=key,
                            is_explicit=explicit,
                            confidence=confidence,
                            source_conversation_id=conversation_id,
                            source_message_id=message_id,
                        )
                        db.add(row)
                        db.flush()
                except IntegrityError:
                    # A concurrent request inserted the same owner/key after
                    # our first SELECT. The savepoint keeps this transaction
                    # usable so we can merge into the winner.
                    row = db.scalar(
                        select(UserMemoryRecord).where(
                            UserMemoryRecord.tenant_id == user.tenant_id,
                            UserMemoryRecord.user_id == user.user_id,
                            UserMemoryRecord.normalized_key == key,
                        )
                    )
                    if row is None:
                        raise

            row.content = content
            row.kind = kind
            row.is_explicit = row.is_explicit or explicit
            row.confidence = max(row.confidence, confidence)
            row.source_conversation_id = conversation_id or row.source_conversation_id
            row.source_message_id = message_id or row.source_message_id
            row.deleted_at = None
            row.updated_at = utcnow()
            db.flush()
            return self._memory_dict(row)

    @staticmethod
    def _conversation_dict(record: ConversationRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "title": record.title,
            "pinned": record.pinned,
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "deleted_at": _iso(record.deleted_at),
            "purge_after": _iso(record.purge_after),
        }

    @staticmethod
    def _message_dict(
        message: MessageRecord, sources: Iterable[MessageSourceRecord]
    ) -> dict[str, Any]:
        source_texts = (message.extra or {}).get("source_texts", {})
        source_items = [
            {
                "label": s.label,
                "chunk_id": s.chunk_id,
                "doc_id": s.doc_id,
                "title": s.title,
                "source_uri": s.source_uri,
                "text": source_texts.get(s.chunk_id, ""),
                "section_path": list(s.section_path or []),
                "page_number": s.page_number,
            }
            for s in sorted(sources, key=lambda item: item.ordinal)
        ]
        return {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "status": message.status,
            "created_at": _iso(message.created_at),
            "trace_id": message.trace_id,
            "model": message.model,
            "abstained": message.abstained,
            "abstention_reason": message.abstention_reason,
            "feedback": message.feedback,
            "sources": source_items,
        }

    @staticmethod
    def _memory_dict(row: UserMemoryRecord) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "content": row.content,
            "is_explicit": row.is_explicit,
            "confidence": row.confidence,
            "source_conversation_id": row.source_conversation_id,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }


_repository: ChatRepository | None = None


def get_chat_repository() -> ChatRepository:
    global _repository
    if _repository is None:
        # Direct module users and some TestClient patterns do not run FastAPI's
        # lifespan hook. Initialize here as well; migrate_schema is idempotent.
        auto_migrate = os.environ.get("RAG_AUTO_MIGRATE", "true").strip().lower()
        migrations_enabled = auto_migrate in ("1", "true", "yes", "on")
        if migrations_enabled:
            migrate_schema()
        _repository = ChatRepository()
        if migrations_enabled:
            _repository.backfill_auto_titles()
    return _repository
