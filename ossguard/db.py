from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


def new_id():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    repository_url: Mapped[str] = mapped_column(String(400))
    ref: Mapped[str] = mapped_column(String(200))
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    inspection: Mapped[dict] = mapped_column(JSONB, default=dict)
    execution_profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    environment_profile: Mapped[dict] = mapped_column(JSONB, default=dict)
    rules: Mapped[dict] = mapped_column(JSONB, default=dict)
    notification: Mapped[dict] = mapped_column(JSONB, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TestSuite(Base):
    __tablename__ = "test_suites"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    content: Mapped[dict] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(20), default="manual")


class Validation(Base):
    __tablename__ = "validations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    request: Mapped[dict] = mapped_column(JSONB)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    package: Mapped[str] = mapped_column(String(120))
    target_version: Mapped[str] = mapped_column(String(100))
    execution_status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    phase: Mapped[str] = mapped_column(String(40), default="queued")
    verdict: Mapped[str] = mapped_column(String(30), default="NOT_EVALUATED")
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    runner_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    cleanup_status: Mapped[str] = mapped_column(String(20), default="PENDING")
    analysis_status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    analysis: Mapped[dict] = mapped_column(JSONB, default=dict)


# QUEUED jobs are durable; only one may be RUNNING, even across competing workers.
Index(
    "uq_one_running_validation",
    text("(1)"),
    unique=True,
    postgresql_where=Validation.execution_status == "RUNNING",
    _table=Validation.__table__,
)


class TestResult(Base):
    __tablename__ = "test_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    validation_id: Mapped[str] = mapped_column(ForeignKey("validations.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict] = mapped_column(JSONB)


class EmailNotification(Base):
    __tablename__ = "email_notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    validation_id: Mapped[str] = mapped_column(ForeignKey("validations.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    recipient: Mapped[str] = mapped_column(String(254))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_id: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def database(url: str):
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
