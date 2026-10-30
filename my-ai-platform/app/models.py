from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    target_type = Column(String, nullable=False)
    base_version = Column(String, nullable=False)
    candidate_version = Column(String, nullable=False)
    base_artifact_id = Column(String, nullable=False)
    candidate_artifact_id = Column(String, nullable=False)
    version_changed = Column(Boolean, default=False)
    flow_changed = Column(Boolean, default=False)
    rules = Column(JSON, nullable=True)
    latest_test_suite_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TestSuite(Base):
    __tablename__ = "test_suites"

    id = Column(String, primary_key=True, index=True)
    project_id = Column(String, ForeignKey("projects.id"))
    confirmed = Column(Boolean, default=False)
    revision = Column(Integer, default=1)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cases = relationship("TestCase", back_populates="test_suite", cascade="all, delete-orphan")


class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(String, primary_key=True, index=True)
    test_suite_id = Column(String, ForeignKey("test_suites.id"))
    case_id = Column(String, nullable=False)
    description = Column(String, nullable=True)
    inputs = Column(JSON, nullable=False)
    expectations = Column(JSON, nullable=False)

    test_suite = relationship("TestSuite", back_populates="cases")


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id = Column(String, primary_key=True, index=True)
    project_id = Column(String, nullable=False)
    test_suite_id = Column(String, nullable=False)
    state = Column(String, nullable=False)
    snapshot_hash = Column(String, nullable=False)
    snapshot = Column(JSON, nullable=False)
    overall_verdict = Column(String, nullable=False)
    ai_analysis = Column(JSON, nullable=True)
    cleanup_status = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    case_results = relationship(
        "ValidationCaseResult", back_populates="validation_run", cascade="all, delete-orphan"
    )


class ValidationCaseResult(Base):
    __tablename__ = "validation_case_results"

    id = Column(String, primary_key=True, index=True)
    validation_run_id = Column(String, ForeignKey("validation_runs.id"))
    case_id = Column(String, nullable=False)
    description = Column(String, nullable=True)
    inputs = Column(JSON, nullable=False)
    verdict = Column(String, nullable=False)
    violated_rules = Column(JSON, nullable=True)
    diffs = Column(JSON, nullable=True)
    base_evidence = Column(JSON, nullable=False)
    candidate_evidence = Column(JSON, nullable=False)

    validation_run = relationship("ValidationRun", back_populates="case_results")