# import hashlib
# import json
# import uuid
# from contextlib import asynccontextmanager
# from datetime import datetime
# from typing import Any, Dict

# from fastapi import Depends, FastAPI, HTTPException, Query, status
# from sqlalchemy.ext.asyncio import AsyncSession
# from sqlalchemy.future import select

# from app.database import get_db, init_db
# from app.models import ProjectModel, TestResultModel, TestSuiteModel, ValidationModel
# from app.schemas import (
#     ProjectCreate,
#     RuleUpdate,
#     TestCaseItem,
#     TestSuiteUpdate,
#     ValidationCreateRequest,
#     ValidationState,
#     VerdictType,
# )
# from app.service import AIHelperService, CommonComparatorEngine, ContractExecutionAdapter

# ACTIVE_VALIDATION_LOCK: Dict[str, bool] = {"is_running": False}


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # 1. 서버 시작 시 DB 테이블 자동 생성
#     await init_db()
    
#     # 2. 서버 재시작 시 미완료 상태 작업을 중단(ABORTED) 상태로 변경
#     # (실제 DB 세션을 사용하는 비동기 처리 영역)
#     yield


# app = FastAPI(
#     title="OSSGuard - AI 기반 맞춤형 오픈소스 업데이트 검증 플랫폼",
#     description="PostgreSQL + SQLAlchemy 연동 버전 (MVP P0)",
#     version="1.0.0",
#     lifespan=lifespan,
# )

# adapter = ContractExecutionAdapter()


# @app.get("/health", tags=["0. 헬스체크"])
# async def health_check():
#     return {"status": "healthy"}


# # 1. F01: 프로젝트 등록
# @app.post("/api/projects", status_code=status.HTTP_201_CREATED, tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
# async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db)):
#     project_id = f"proj_{uuid.uuid4().hex[:8]}"
#     db_project = ProjectModel(
#         id=project_id,
#         name=payload.name,
#         target_type=payload.target_type,
#         base_version=payload.base_version,
#         candidate_version=payload.candidate_version,
#         base_artifact_id=payload.base_artifact_id,
#         candidate_artifact_id=payload.candidate_artifact_id,
#     )
#     db.add(db_project)
#     await db.commit()
#     await db.refresh(db_project)
#     return {
#         "id": db_project.id,
#         "name": db_project.name,
#         "target_type": db_project.target_type,
#         "base_version": db_project.base_version,
#         "candidate_version": db_project.candidate_version,
#         "base_artifact_id": db_project.base_artifact_id,
#         "candidate_artifact_id": db_project.candidate_artifact_id,
#         "created_at": db_project.created_at.isoformat(),
#     }


# # 2. F01: 프로젝트 조회
# @app.get("/api/projects/{id}", tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
# async def get_project(id: str, db: AsyncSession = Depends(get_db)):
#     result = await db.execute(select(ProjectModel).where(ProjectModel.id == id))
#     project = result.scalars().first()
#     if not project:
#         raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    
#     return {
#         "project": {
#             "id": project.id,
#             "name": project.name,
#             "target_type": project.target_type,
#             "base_version": project.base_version,
#             "candidate_version": project.candidate_version,
#             "rules": {"natural_language_rule": project.natural_language_rule, "input_schema": project.input_schema, "logical_steps": project.logical_steps},
#         }
#     }


# # 3. F02: 업무 규칙 저장
# @app.put("/api/projects/{id}/rules", tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
# async def update_project_rules(id: str, payload: RuleUpdate, db: AsyncSession = Depends(get_db)):
#     result = await db.execute(select(ProjectModel).where(ProjectModel.id == id))
#     project = result.scalars().first()
#     if not project:
#         raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")

#     project.natural_language_rule = payload.natural_language_rule
#     project.input_schema = payload.input_schema
#     project.logical_steps = [step.model_dump() for step in payload.logical_steps]
#     await db.commit()
    
#     return {"message": "업무 규칙 및 논리 단계 메타데이터가 DB에 저장되었습니다."}


# # 4. F03: AI 테스트 초안 생성
# @app.post("/api/projects/{id}/test-suites/generate", tags=["2. AI 테스트 생성 및 확정 (F03~F04)"])
# async def generate_test_suite(id: str, db: AsyncSession = Depends(get_db)):
#     result = await db.execute(select(ProjectModel).where(ProjectModel.id == id))
#     project = result.scalars().first()
#     if not project:
#         raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
#     if not project.natural_language_rule:
#         raise HTTPException(status_code=400, detail="먼저 업무 규칙을 등록해주세요.")

#     suite_id = f"suite_{uuid.uuid4().hex[:8]}"
#     draft_cases = [c.model_dump() for c in AIHelperService.generate_default_test_cases()]

#     db_suite = TestSuiteModel(
#         id=suite_id,
#         project_id=id,
#         cases_json=draft_cases,
#         confirmed=False,
#         revision=1,
#     )
#     db.add(db_suite)
#     await db.commit()

#     return {"id": suite_id, "project_id": id, "cases": draft_cases, "confirmed": False}


# # 5. F04: 테스트 세트 확정
# @app.put("/api/test-suites/{id}", tags=["2. AI 테스트 생성 및 확정 (F03~F04)"])
# async def confirm_or_update_test_suite(id: str, payload: TestSuiteUpdate, db: AsyncSession = Depends(get_db)):
#     result = await db.execute(select(TestSuiteModel).where(TestSuiteModel.id == id))
#     suite = result.scalars().first()
#     if not suite:
#         raise HTTPException(status_code=404, detail="테스트 세트를 찾을 수 없습니다.")

#     suite.cases_json = [c.model_dump() for c in payload.cases]
#     suite.confirmed = payload.confirmed
#     suite.revision += 1
#     await db.commit()

#     return {"message": "테스트 세트가 확정 저장되었습니다.", "test_suite_id": suite.id, "confirmed": suite.confirmed}


# # 6. F05: 검증 시작 및 결과 저장
# @app.post("/api/validations", status_code=status.HTTP_202_ACCEPTED, tags=["3. 검증 실행 및 결과 조회 (F05~F13)"])
# async def start_validation(
#     payload: ValidationCreateRequest,
#     simulate_ai_error: bool = Query(default=False),
#     db: AsyncSession = Depends(get_db),
# ):
#     if ACTIVE_VALIDATION_LOCK["is_running"]:
#         raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 실행 중인 검증 작업이 있습니다.")

#     # 프로젝트 및 테스트 세트 조회
#     proj_res = await db.execute(select(ProjectModel).where(ProjectModel.id == payload.project_id))
#     project = proj_res.scalars().first()
#     suite_res = await db.execute(select(TestSuiteModel).where(TestSuiteModel.id == payload.test_suite_id))
#     suite = suite_res.scalars().first()

#     if not project or not suite:
#         raise HTTPException(status_code=404, detail="프로젝트 또는 테스트 세트를 찾을 수 없습니다.")
#     if not suite.confirmed:
#         raise HTTPException(status_code=400, detail="확정되지 않은 테스트 세트는 실행할 수 없습니다.")

#     ACTIVE_VALIDATION_LOCK["is_running"] = True
#     run_id = f"val_{uuid.uuid4().hex[:8]}"

#     snapshot = {
#         "project_name": project.name,
#         "base_version": project.base_version,
#         "candidate_version": project.candidate_version,
#         "base_artifact_id": project.base_artifact_id,
#         "candidate_artifact_id": project.candidate_artifact_id,
#         "rules": project.natural_language_rule,
#         "cases": suite.cases_json,
#     }
#     snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]

#     # Validation 레코드 생성
#     db_val = ValidationModel(
#         id=run_id,
#         project_id=project.id,
#         state=ValidationState.PREPARING,
#         overall_verdict=VerdictType.UNVERIFIED,
#         snapshot_json={"hash": snapshot_hash, **snapshot},
#         cleanup_status="PENDING",
#     )
#     db.add(db_val)
#     await db.commit()

#     try:
#         adapter.prepare({"target_type": project.target_type})
#         adapter.start(snapshot)

#         parsed_cases = [TestCaseItem(**c) for c in suite.cases_json]
#         case_comparisons = []

#         for case_item in parsed_cases:
#             base_res = adapter.execute_and_collect(run_id, "base", project.base_artifact_id, case_item)
#             cand_res = adapter.execute_and_collect(run_id, "candidate", project.candidate_artifact_id, case_item)

#             verdict, violations, diffs = CommonComparatorEngine.compare_case(
#                 base_res, cand_res, case_item.expectations
#             )

#             case_comparisons.append({
#                 "case_id": case_item.case_id,
#                 "verdict": verdict,
#                 "violations": violations,
#                 "diffs": diffs,
#             })

#             # 개별 테스트 결과 저장 (test_results 테이블)
#             db_res_item = TestResultModel(
#                 validation_id=run_id,
#                 case_id=case_item.case_id,
#                 base_result_json=base_res.model_dump(),
#                 candidate_result_json=cand_res.model_dump(),
#                 verdict=verdict,
#                 violated_rules=violations,
#                 diff_fields=diffs,
#             )
#             db.add(db_res_item)

#         # 전체 판정 집계
#         verdicts = [c["verdict"] for c in case_comparisons]
#         if VerdictType.REGRESSION_SUSPECTED in verdicts:
#             overall = VerdictType.REGRESSION_SUSPECTED
#         elif all(v == VerdictType.PASS for v in verdicts):
#             overall = VerdictType.PASS
#         else:
#             overall = VerdictType.UNVERIFIED

#         ai_res = AIHelperService.analyze_failure(
#             case_comparisons, project.base_artifact_id, project.candidate_artifact_id, simulate_ai_error
#         )

#         db_val.overall_verdict = overall
#         db_val.ai_analysis_json = ai_res.model_dump()
#         db_val.state = ValidationState.COMPLETED
#         await db.commit()

#     finally:
#         db_val.cleanup_status = adapter.cleanup(run_id)
#         await db.commit()
#         ACTIVE_VALIDATION_LOCK["is_running"] = False

#     return {"validation_id": run_id, "overall_verdict": overall, "message": "검증 완료 및 PostgreSQL 저장 완료"}


# # 7. F05, F10: 결과 조회
# @app.get("/api/validations/{id}", tags=["3. 검증 실행 및 결과 조회 (F05~F13)"])
# async def get_validation_result(id: str, db: AsyncSession = Depends(get_db)):
#     result = await db.execute(select(ValidationModel).where(ValidationModel.id == id))
#     val = result.scalars().first()
#     if not val:
#         raise HTTPException(status_code=404, detail="검증 결과를 찾을 수 없습니다.")
#     return {
#         "validation_id": val.id,
#         "state": val.state,
#         "overall_verdict": val.overall_verdict,
#         "snapshot": val.snapshot_json,
#         "ai_analysis": val.ai_analysis_json,
#         "cleanup_status": val.cleanup_status,
#     }
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Query, status
from pydantic import ValidationError
from runner.judge import JUDGE_VERSION

from app.runner_bridge import judge_results, overall_verdict, to_runner_case
from app.schemas import (
    ProjectCreate,
    RuleUpdate,
    TestCaseItem,
    TestSuiteUpdate,
    ValidationCreateRequest,
    ValidationState,
    VerdictType,
)
from app.service import AIHelperService, ContractExecutionAdapter

# 해커톤 시연용 고속 메모리 저장소
PROJECTS_DB: Dict[str, Dict[str, Any]] = {}
TEST_SUITES_DB: Dict[str, Dict[str, Any]] = {}
VALIDATIONS_DB: Dict[str, Dict[str, Any]] = {}
ACTIVE_VALIDATION_LOCK: Dict[str, bool] = {"is_running": False}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 재시작 시 미완료 작업 정리
    for val in VALIDATIONS_DB.values():
        if val["state"] not in (ValidationState.COMPLETED, ValidationState.ABORTED):
            val["state"] = ValidationState.ABORTED
            val["cleanup_status"] = "CLEANED_ON_RESTART"
    yield


app = FastAPI(
    title="OSSGuard - AI 기반 맞춤형 오픈소스 업데이트 검증 플랫폼",
    description="해커톤 MVP 검증 플랫폼 API (메모리 고속 구동 버전)",
    version="1.0.0",
    lifespan=lifespan,
)

adapter = ContractExecutionAdapter()


@app.get("/health", tags=["0. 헬스체크"])
async def health_check():
    return {"status": "healthy"}


# 1. F01: 프로젝트·검증 대상·기존/후보 아티팩트 등록
@app.post("/api/projects", status_code=status.HTTP_201_CREATED, tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
async def create_project(payload: ProjectCreate):
    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    project_data = {
        "id": project_id,
        **payload.model_dump(),
        "version_changed": payload.base_version != payload.candidate_version,
        "flow_changed": payload.base_artifact_id != payload.candidate_artifact_id,
        "rules": None,
        "latest_test_suite_id": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    PROJECTS_DB[project_id] = project_data
    return project_data


# 2. F01: 저장된 설정과 테스트 확인
@app.get("/api/projects/{id}", tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
async def get_project(id: str):
    project = PROJECTS_DB.get(id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    suite_id = project.get("latest_test_suite_id")
    test_suite = TEST_SUITES_DB.get(suite_id) if suite_id else None
    return {"project": project, "test_suite": test_suite}


# 3. F02: 업무 규칙·판정 필드·논리 단계 저장
@app.put("/api/projects/{id}/rules", tags=["1. 프로젝트 및 규칙 설정 (F01~F02)"])
async def update_project_rules(id: str, payload: RuleUpdate):
    project = PROJECTS_DB.get(id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    project["rules"] = payload.model_dump()
    return {"message": "업무 규칙 및 논리 단계 메타데이터가 저장되었습니다.", "project": project}


# 4. F03: AI 테스트 초안 생성 (T01~T04)
@app.post("/api/projects/{id}/test-suites/generate", tags=["2. AI 테스트 생성 및 확정 (F03~F04)"])
async def generate_test_suite(id: str):
    project = PROJECTS_DB.get(id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    if not project.get("rules"):
        raise HTTPException(status_code=400, detail="먼저 PUT /api/projects/{id}/rules 로 업무 규칙을 등록해주세요.")

    suite_id = f"suite_{uuid.uuid4().hex[:8]}"
    draft_cases = [c.model_dump() for c in AIHelperService.generate_default_test_cases()]

    suite_data = {
        "id": suite_id,
        "project_id": id,
        "cases": draft_cases,
        "confirmed": False,
        "revision": 1,
        "updated_at": datetime.utcnow().isoformat(),
    }
    TEST_SUITES_DB[suite_id] = suite_data
    project["latest_test_suite_id"] = suite_id
    return suite_data


# 5. F04: 입력·기대 결과 수정 및 확정 (confirmed 저장)
@app.put("/api/test-suites/{id}", tags=["2. AI 테스트 생성 및 확정 (F03~F04)"])
async def confirm_or_update_test_suite(id: str, payload: TestSuiteUpdate):
    suite = TEST_SUITES_DB.get(id)
    if not suite:
        raise HTTPException(status_code=404, detail="테스트 세트를 찾을 수 없습니다.")

    suite["cases"] = [c.model_dump() for c in payload.cases]
    suite["confirmed"] = payload.confirmed
    suite["revision"] += 1
    suite["updated_at"] = datetime.utcnow().isoformat()
    return {"message": "테스트 세트가 확정 저장되었습니다.", "test_suite": suite}


# 6. F05: 확정된 스냅샷으로 검증 시작 (202 응답 + 실행 ID)
@app.post("/api/validations", status_code=status.HTTP_202_ACCEPTED, tags=["3. 검증 실행 및 결과 조회 (F05~F13)"])
async def start_validation(
    payload: ValidationCreateRequest,
    simulate_ai_error: bool = Query(default=False, description="V08 시험용: AI 분석 오류 주입 여부"),
):
    if ACTIVE_VALIDATION_LOCK["is_running"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 실행 중인 검증 작업이 있습니다.")

    project = PROJECTS_DB.get(payload.project_id)
    suite = TEST_SUITES_DB.get(payload.test_suite_id)
    if not project or not suite:
        raise HTTPException(status_code=404, detail="프로젝트 또는 테스트 세트를 찾을 수 없습니다.")

    if not suite.get("confirmed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="확정(confirmed=true)되지 않은 테스트 세트는 검증을 실행할 수 없습니다.",
        )

    # 판정 기준을 실행 전에 검증한다 (허용되지 않는 비교식이면 실행하지 않음)
    try:
        parsed_cases = [TestCaseItem(**c) for c in suite["cases"]]
        runner_cases = [to_runner_case(c) for c in parsed_cases]
    except ValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"테스트 세트의 기대 조건 형식이 올바르지 않습니다: {e}",
        )

    ACTIVE_VALIDATION_LOCK["is_running"] = True
    run_id = f"val_{uuid.uuid4().hex[:8]}"

    snapshot = {
        "project_name": project["name"],
        "target_type": project["target_type"],
        "base_version": project["base_version"],
        "candidate_version": project["candidate_version"],
        "base_artifact_id": project["base_artifact_id"],
        "candidate_artifact_id": project["candidate_artifact_id"],
        "rules": project["rules"],
        "cases": suite["cases"],
    }
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()[:16]

    validation_record = {
        "id": run_id,
        "project_id": project["id"],
        "test_suite_id": suite["id"],
        "state": ValidationState.PREPARING,
        "snapshot_hash": snapshot_hash,
        "snapshot": snapshot,
        "adapter_version": "node-red-adapter-0.1.0",
        "comparator_version": f"runner-judge-{JUDGE_VERSION}",
        "overall_verdict": VerdictType.UNVERIFIED,
        "case_results": [],
        "ai_analysis": None,
        "cleanup_status": "PENDING",
        "created_at": datetime.utcnow().isoformat(),
    }
    VALIDATIONS_DB[run_id] = validation_record

    try:
        adapter.prepare(project)
        adapter.start(snapshot)

        validation_record["state"] = ValidationState.RUNNING_BASE

        case_comparisons = []
        for case_item, runner_case in zip(parsed_cases, runner_cases):
            base_res = adapter.execute_and_collect(run_id, "base", project["base_artifact_id"], case_item)
            validation_record["state"] = ValidationState.RUNNING_CANDIDATE
            cand_res = adapter.execute_and_collect(run_id, "candidate", project["candidate_artifact_id"], case_item)

            validation_record["state"] = ValidationState.COMPARING
            verdict, violations, diffs, judgement = judge_results(runner_case, base_res, cand_res)

            case_comparisons.append({
                "case_id": case_item.case_id,
                "description": case_item.description,
                "inputs": case_item.inputs,
                "verdict": verdict,
                "violated_rules": violations,
                "diffs": diffs,
                "judgement": judgement,
                "base_evidence": base_res.model_dump(),
                "candidate_evidence": cand_res.model_dump(),
            })

        validation_record["case_results"] = case_comparisons

        validation_record["overall_verdict"] = overall_verdict([c["verdict"] for c in case_comparisons])

        validation_record["state"] = ValidationState.ANALYZING
        ai_res = AIHelperService.analyze_failure(
            case_comparisons,
            project["base_artifact_id"],
            project["candidate_artifact_id"],
            simulate_ai_error=simulate_ai_error,
        )
        validation_record["ai_analysis"] = ai_res.model_dump()
        validation_record["state"] = ValidationState.COMPLETED

    finally:
        validation_record["cleanup_status"] = adapter.cleanup(run_id)
        ACTIVE_VALIDATION_LOCK["is_running"] = False

    return {
        "validation_id": run_id,
        "state": validation_record["state"],
        "overall_verdict": validation_record["overall_verdict"],
        "message": "검증 실행이 완료되었습니다. GET /api/validations/{id} 로 상세 증거를 조회하세요.",
    }


# 7. F05, F10, F11, F12: 진행·결과·AI 분석·정리 상태 조회
@app.get("/api/validations/{id}", tags=["3. 검증 실행 및 결과 조회 (F05~F13)"])
async def get_validation_result(id: str):
    val = VALIDATIONS_DB.get(id)
    if not val:
        raise HTTPException(status_code=404, detail="검증 실행 기록을 찾을 수 없습니다.")
    return val