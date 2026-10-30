import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.database import get_db, init_db
from app.models import Project, TestCase, TestSuite, ValidationCaseResult, ValidationRun
from app.schemas import (
    ProjectCreate,
    RuleUpdate,
    TestCaseItem,
    TestSuiteUpdate,
    ValidationCreateRequest,
    ValidationState,
    VerdictType,
)
from app.service import AIHelperService, CommonComparatorEngine, ContractExecutionAdapter

ACTIVE_VALIDATION_LOCK = {"is_running": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="OSSGuard - AI 기반 맞춤형 오픈소스 업데이트 검증 플랫폼",
    description="해커톤 MVP 검증 플랫폼 API (PostgreSQL DB 연동 버전)",
    version="1.0.0",
    lifespan=lifespan,
)

adapter = ContractExecutionAdapter()


@app.get("/health", tags=["0. 헬스체크"])
async def health_check():
    return {"status": "healthy"}


# 1. F01: 프로젝트·검증 대상·기존/후보 아티팩트 등록
@app.post("/api/projects", status_code=status.HTTP_201_CREATED, tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project_id = f"proj_{uuid.uuid4().hex[:8]}"

    new_project = Project(
        id=project_id,
        name=payload.name,
        target_type=payload.target_type,
        base_version=payload.base_version,
        candidate_version=payload.candidate_version,
        base_artifact_id=payload.base_artifact_id,
        candidate_artifact_id=payload.candidate_artifact_id,
        version_changed=(payload.base_version != payload.candidate_version),
        flow_changed=(payload.base_artifact_id != payload.candidate_artifact_id),
    )
    db.add(new_project)
    await db.commit()
    await db.refresh(new_project)

    return {
        "id": new_project.id,
        "name": new_project.name,
        "target_type": new_project.target_type,
        "version_changed": new_project.version_changed,
        "flow_changed": new_project.flow_changed,
        "created_at": new_project.created_at,
    }


# 2. F01: 저장된 설정과 테스트 확인
@app.get("/api/projects/{id}", tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
async def get_project(id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).filter(Project.id == id))
    project = result.scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    suite_data = None
    if project.latest_test_suite_id:
        suite_result = await db.execute(
            select(TestSuite)
            .options(selectinload(TestSuite.cases))
            .filter(TestSuite.id == project.latest_test_suite_id)
        )
        suite = suite_result.scalars().first()
        if suite:
            suite_data = {
                "id": suite.id,
                "confirmed": suite.confirmed,
                "revision": suite.revision,
                "cases": [
                    {
                        "case_id": c.case_id,
                        "description": c.description,
                        "inputs": c.inputs,
                        "expectations": c.expectations,
                    }
                    for c in suite.cases
                ],
            }

    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "target_type": project.target_type,
            "base_version": project.base_version,
            "candidate_version": project.candidate_version,
            "base_artifact_id": project.base_artifact_id,
            "candidate_artifact_id": project.candidate_artifact_id,
            "rules": project.rules,
            "latest_test_suite_id": project.latest_test_suite_id,
        },
        "test_suite": suite_data,
    }


# 3. F02: 업무 규칙·판정 필드·논리 단계 저장
@app.put("/api/projects/{id}/rules", tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
async def update_project_rules(id: str, payload: RuleUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).filter(Project.id == id))
    project = result.scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

    project.rules = payload.model_dump()
    await db.commit()
    await db.refresh(project)

    return {"message": "업무 규칙 및 논리 단계 메타데이터가 저장되었습니다.", "rules": project.rules}


# 4. F03: AI 테스트 초안 생성 (T01~T04)
@app.post("/api/projects/{id}/test-suites/generate", tags=["2. AI 테스트 생성 및 확정 (F03~F04)"])
async def generate_test_suite(id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).filter(Project.id == id))
    project = result.scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    if not project.rules:
        raise HTTPException(status_code=400, detail="먼저 PUT /api/projects/{id}/rules 로 업무 규칙을 등록해주세요.")

    suite_id = f"suite_{uuid.uuid4().hex[:8]}"
    draft_cases = AIHelperService.generate_default_test_cases()

    new_suite = TestSuite(id=suite_id, project_id=id, confirmed=False, revision=1)
    db.add(new_suite)
    await db.flush()

    cases_db = []
    for case_item in draft_cases:
        new_case = TestCase(
            id=f"case_{uuid.uuid4().hex[:8]}",
            test_suite_id=suite_id,
            case_id=case_item.case_id,
            description=case_item.description,
            inputs=case_item.inputs,
            expectations=[e.model_dump() for e in case_item.expectations],
        )
        db.add(new_case)
        cases_db.append(new_case)

    project.latest_test_suite_id = suite_id
    await db.commit()

    return {
        "id": suite_id,
        "project_id": id,
        "confirmed": False,
        "revision": 1,
        "cases": [
            {
                "case_id": c.case_id,
                "description": c.description,
                "inputs": c.inputs,
                "expectations": c.expectations,
            }
            for c in cases_db
        ],
    }


# 5. F04: 입력·기대 결과 수정 및 확정 (confirmed 저장)
@app.put("/api/test-suites/{id}", tags=["2. AI 테스트 생성 및 확정 (F03~F04)"])
async def confirm_or_update_test_suite(id: str, payload: TestSuiteUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TestSuite).options(selectinload(TestSuite.cases)).filter(TestSuite.id == id)
    )
    suite = result.scalars().first()
    if not suite:
        raise HTTPException(status_code=404, detail="테스트 세트를 찾을 수 없습니다.")

    for old_case in suite.cases:
        await db.delete(old_case)

    for case_item in payload.cases:
        new_case = TestCase(
            id=f"case_{uuid.uuid4().hex[:8]}",
            test_suite_id=suite.id,
            case_id=case_item.case_id,
            description=case_item.description,
            inputs=case_item.inputs,
            expectations=[e.model_dump() for e in case_item.expectations],
        )
        db.add(new_case)

    suite.confirmed = payload.confirmed
    suite.revision += 1
    suite.updated_at = datetime.utcnow()

    await db.commit()
    return {"message": "테스트 세트가 확정 저장되었습니다.", "confirmed": suite.confirmed}


# 6. F05: 확정된 스냅샷으로 검증 시작 (202 응답 + 실행 ID)
@app.post("/api/validations", status_code=status.HTTP_202_ACCEPTED, tags=["3. 검증 실행 및 결과 조회 (F05~F13)"])
async def start_validation(
    payload: ValidationCreateRequest,
    simulate_ai_error: bool = Query(default=False, description="V08 시험용: AI 분석 오류 주입 여부"),
    db: AsyncSession = Depends(get_db),
):
    if ACTIVE_VALIDATION_LOCK["is_running"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 실행 중인 검증 작업이 있습니다.")

    proj_result = await db.execute(select(Project).filter(Project.id == payload.project_id))
    project = proj_result.scalars().first()

    suite_result = await db.execute(
        select(TestSuite).options(selectinload(TestSuite.cases)).filter(TestSuite.id == payload.test_suite_id)
    )
    suite = suite_result.scalars().first()

    if not project or not suite:
        raise HTTPException(status_code=404, detail="프로젝트 또는 테스트 세트를 찾을 수 없습니다.")

    if not suite.confirmed:
        raise HTTPException(status_code=400, detail="확정(confirmed=true)되지 않은 테스트 세트는 검증할 수 없습니다.")

    ACTIVE_VALIDATION_LOCK["is_running"] = True
    run_id = f"val_{uuid.uuid4().hex[:8]}"

    cases_dump = [
        {"case_id": c.case_id, "description": c.description, "inputs": c.inputs, "expectations": c.expectations}
        for c in suite.cases
    ]
    snapshot = {
        "project_name": project.name,
        "target_type": project.target_type,
        "base_version": project.base_version,
        "candidate_version": project.candidate_version,
        "base_artifact_id": project.base_artifact_id,
        "candidate_artifact_id": project.candidate_artifact_id,
        "rules": project.rules,
        "cases": cases_dump,
    }
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]

    run_record = ValidationRun(
        id=run_id,
        project_id=project.id,
        test_suite_id=suite.id,
        state=ValidationState.PREPARING.value,
        snapshot_hash=snapshot_hash,
        snapshot=snapshot,
        overall_verdict=VerdictType.UNVERIFIED.value,
        cleanup_status="PENDING",
    )
    db.add(run_record)
    await db.commit()

    try:
        project_dict = {
            "id": project.id,
            "name": project.name,
            "target_type": project.target_type,
            "base_artifact_id": project.base_artifact_id,
            "candidate_artifact_id": project.candidate_artifact_id,
        }

        adapter.prepare(project_dict)
        adapter.start(snapshot)

        run_record.state = ValidationState.RUNNING_BASE.value
        await db.commit()

        parsed_cases = [TestCaseItem(**c) for c in cases_dump]
        case_comparisons = []

        for case_item in parsed_cases:
            base_res = adapter.execute_and_collect(run_id, "base", project.base_artifact_id, case_item)
            run_record.state = ValidationState.RUNNING_CANDIDATE.value
            await db.commit()

            cand_res = adapter.execute_and_collect(run_id, "candidate", project.candidate_artifact_id, case_item)

            run_record.state = ValidationState.COMPARING.value
            verdict, violations, diffs = CommonComparatorEngine.compare_case(
                base_res, cand_res, case_item.expectations
            )

            case_result_record = ValidationCaseResult(
                id=f"res_{uuid.uuid4().hex[:8]}",
                validation_run_id=run_id,
                case_id=case_item.case_id,
                description=case_item.description,
                inputs=case_item.inputs,
                verdict=verdict.value,
                violated_rules=violations,
                diffs=diffs,
                base_evidence=base_res.model_dump(),
                candidate_evidence=cand_res.model_dump(),
            )
            db.add(case_result_record)

            case_comparisons.append(
                {
                    "case_id": case_item.case_id,
                    "verdict": verdict,
                    "description": case_item.description,
                    "inputs": case_item.inputs,
                }
            )

        verdicts = [c["verdict"] for c in case_comparisons]
        if VerdictType.EXECUTION_ERROR in verdicts:
            run_record.overall_verdict = VerdictType.EXECUTION_ERROR.value
        elif VerdictType.BASE_CHECK_REQUIRED in verdicts:
            run_record.overall_verdict = VerdictType.BASE_CHECK_REQUIRED.value
        elif VerdictType.REGRESSION_SUSPECTED in verdicts:
            run_record.overall_verdict = VerdictType.REGRESSION_SUSPECTED.value
        elif all(v == VerdictType.PASS for v in verdicts):
            run_record.overall_verdict = VerdictType.PASS.value
        else:
            run_record.overall_verdict = VerdictType.UNVERIFIED.value

        run_record.state = ValidationState.ANALYZING.value
        await db.commit()

        ai_res = AIHelperService.analyze_failure(
            case_comparisons,
            project.base_artifact_id,
            project.candidate_artifact_id,
            simulate_ai_error=simulate_ai_error,
        )
        run_record.ai_analysis = ai_res.model_dump()
        run_record.state = ValidationState.COMPLETED.value

    finally:
        run_record.cleanup_status = adapter.cleanup(run_id)
        ACTIVE_VALIDATION_LOCK["is_running"] = False
        await db.commit()

    return {
        "validation_id": run_id,
        "state": run_record.state,
        "overall_verdict": run_record.overall_verdict,
        "message": "검증 실행이 완료되었습니다. GET /api/validations/{id} 로 상세 증거를 조회하세요.",
    }


# 7. F05, F10, F11, F12: 진행·결과·AI 분석·정리 상태 조회
@app.get("/api/validations/{id}", tags=["3. 검증 실행 및 결과 조회 (F05~F13)"])
async def get_validation_result(id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ValidationRun)
        .options(selectinload(ValidationRun.case_results))
        .filter(ValidationRun.id == id)
    )
    val = result.scalars().first()
    if not val:
        raise HTTPException(status_code=404, detail="검증 실행 기록을 찾을 수 없습니다.")

    return {
        "id": val.id,
        "project_id": val.project_id,
        "test_suite_id": val.test_suite_id,
        "state": val.state,
        "snapshot_hash": val.snapshot_hash,
        "overall_verdict": val.overall_verdict,
        "ai_analysis": val.ai_analysis,
        "cleanup_status": val.cleanup_status,
        "case_results": [
            {
                "case_id": r.case_id,
                "description": r.description,
                "verdict": r.verdict,
                "violated_rules": r.violated_rules,
                "diffs": r.diffs,
                "base_evidence": r.base_evidence,
                "candidate_evidence": r.candidate_evidence,
            }
            for r in val.case_results
        ],
    }