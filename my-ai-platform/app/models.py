from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ProjectModel(Base):
    """projects: 대상 종류, 어댑터 설정, 버전·아티팩트 참조, 규칙·입력 스키마·단계 메타데이터"""
    __tablename__ = "projects"

    id = Column(String(64), primary_key=True)
    name = Column(String(200), nullable=False)
    target_type = Column(String(50), nullable=False, default="node-red")
    base_version = Column(String(100), nullable=False)
    candidate_version = Column(String(100), nullable=False)
    base_artifact_id = Column(String(100), nullable=False)
    candidate_artifact_id = Column(String(100), nullable=False)
    natural_language_rule = Column(Text, nullable=True)
    input_schema = Column(JSON, nullable=True)
    logical_steps = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TestSuiteModel(Base):
    """test_suites: 프로젝트 ID, 테스트 입력·기대 조건 JSON, 확정 여부·수정 버전"""
    __tablename__ = "test_suites"

    id = Column(String(64), primary_key=True)
    project_id = Column(String(64), ForeignKey("projects.id"), nullable=False)
    cases_json = Column(JSON, nullable=False)
    confirmed = Column(Boolean, default=False)
    revision = Column(Integer, default=1)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ValidationModel(Base):
    """validations: 실행 상태, 규칙·테스트·아티팩트 스냅샷, 이미지 식별자, 어댑터·판정 버전, 분석·정리 상태"""
    __tablename__ = "validations"

    id = Column(String(64), primary_key=True)
    project_id = Column(String(64), ForeignKey("projects.id"), nullable=False)
    state = Column(String(50), nullable=False)
    overall_verdict = Column(String(50), nullable=True)
    snapshot_json = Column(JSON, nullable=False)
    adapter_version = Column(String(30), default="node-red-adapter-0.1.0")
    rule_engine_version = Column(String(30), default="comparator-0.1.0")
    ai_analysis_json = Column(JSON, nullable=True)
    cleanup_status = Column(String(30), default="PENDING")
    created_at = Column(DateTime, default=datetime.utcnow)


class TestResultModel(Base):
    """test_results: 실행·케이스·환경 ID, 응답, 정규화 이벤트, 판정, 원본 증거 참조"""
    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    validation_id = Column(String(64), ForeignKey("validations.id"), nullable=False)
    case_id = Column(String(32), nullable=False)
    base_result_json = Column(JSON, nullable=False)
    candidate_result_json = Column(JSON, nullable=False)
    verdict = Column(String(50), nullable=False)
    violated_rules = Column(JSON, nullable=False)
    diff_fields = Column(JSON, nullable=False)