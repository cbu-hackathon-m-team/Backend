from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Validation(Base):
    __tablename__ = "validations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[dict] = mapped_column(JSON)
    report: Mapped[dict] = mapped_column(JSON)


class TestResult(Base):
    __tablename__ = "test_results"
    run_id: Mapped[str] = mapped_column(ForeignKey("validations.id"), primary_key=True)
    env_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    evidence: Mapped[dict] = mapped_column(JSON)


class ExecutionSlot(Base):
    __tablename__ = "execution_slot"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("validations.id"), unique=True)


class BusyError(Exception):
    pass


class Repository:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.owner_connection = None

    async def initialize_for_tests(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def own_worker(self):
        """One API worker owns recovery; an accidentally started second worker fails."""
        if self.engine.dialect.name == "postgresql":
            self.owner_connection = await self.engine.connect()
            acquired = await self.owner_connection.scalar(text("SELECT pg_try_advisory_lock(64821901)"))
            if not acquired:
                await self.owner_connection.close()
                self.owner_connection = None
                raise BusyError("Another execution worker owns this database")

    async def close(self):
        if self.owner_connection is not None:
            await self.owner_connection.execute(text("SELECT pg_advisory_unlock(64821901)"))
            await self.owner_connection.close()
        await self.engine.dispose()

    async def create(self, snapshot: dict):
        run_id = str(uuid4())
        try:
            async with self.sessions.begin() as session:
                session.add(
                    Validation(
                        id=run_id,
                        created_at=datetime.now(timezone.utc),
                        status="QUEUED",
                        snapshot=snapshot,
                        report={"analysis_status": "NOT_CONFIGURED", "cleanup_status": "NOT_STARTED"},
                    )
                )
                await session.flush()
                session.add(ExecutionSlot(id=1, run_id=run_id))
        except IntegrityError as error:
            raise BusyError("A validation is running or waiting for cleanup") from error
        return run_id

    async def get(self, run_id: str):
        async with self.sessions() as session:
            row = await session.get(Validation, run_id)
            if row is None:
                return None
            results = (await session.scalars(select(TestResult).where(TestResult.run_id == run_id))).all()
            return {
                "id": row.id,
                "created_at": row.created_at.isoformat(),
                "status": row.status,
                "snapshot": row.snapshot,
                "report": row.report,
                "results": [r.evidence for r in results],
            }

    async def update(self, run_id: str, *, status=None, report=None):
        async with self.sessions.begin() as session:
            row = await session.get(Validation, run_id)
            if row is None:
                raise KeyError(run_id)
            if status:
                row.status = status
            if report is not None:
                row.report = report

    async def save_evidence(self, evidence):
        async with self.sessions.begin() as session:
            await session.merge(
                TestResult(
                    run_id=evidence.run_id,
                    env_id=evidence.env_id,
                    case_id=evidence.case_id,
                    evidence=evidence.model_dump(mode="json"),
                )
            )

    async def release(self, run_id):
        async with self.sessions.begin() as session:
            await session.execute(delete(ExecutionSlot).where(ExecutionSlot.run_id == run_id))

    async def unfinished(self):
        async with self.sessions() as session:
            rows = (await session.scalars(select(Validation))).all()
            occupied = set((await session.scalars(select(ExecutionSlot.run_id))).all())
            return [
                row.id
                for row in rows
                if row.status not in {"COMPLETED", "FAILED", "INTERRUPTED"}
                or row.report.get("cleanup_status") == "PENDING"
                or row.id in occupied
            ]
