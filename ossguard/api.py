import hmac
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from ossguard.ai import generate_suite
from ossguard.config import Settings, get_settings
from ossguard.db import EmailNotification, Project, TestResult, TestSuite, Validation, database
from ossguard.errors import GuardError
from ossguard.repository import RepositoryService, allowed_repository
from ossguard.schemas import (
    EnvironmentUpdate,
    ExecutionProfile,
    NotificationUpdate,
    ProjectCreate,
    ProjectResponse,
    RulesUpdate,
    SuiteUpdate,
    ValidationCreate,
    ValidationResponse,
    ValidationSubmitted,
)


def check_environment(profile: dict, environment: dict, *, require_values=False):
    schema = ExecutionProfile.model_validate(profile).env_schema
    values = environment.get("values", {})
    refs = environment.get("secret_refs", {})
    for key in values.keys() | refs.keys():
        definition = schema.get(key)
        if not definition or definition.source == "runner_mock":
            raise GuardError("INVALID_ENVIRONMENT", f"Unsupported user variable: {key}")
        if (key in refs) != definition.secret:
            raise GuardError("INVALID_ENVIRONMENT", f"Wrong storage type for {key}")
    if require_values:
        missing = [
            key
            for key, definition in schema.items()
            if definition.source != "runner_mock"
            and definition.required
            and key not in values
            and key not in refs
        ]
        if missing:
            raise GuardError("MISSING_ENVIRONMENT", "Missing variables: " + ", ".join(missing))


def project_response(project):
    return {
        "id": project.id,
        "name": project.name,
        "repository_url": project.repository_url,
        "ref": project.ref,
        "commit_sha": project.commit_sha,
        "revision": project.revision,
        "execution_profile": project.execution_profile,
        "environment_profile": project.environment_profile,
        "rules": project.rules,
        "notification": project.notification,
        "inspection": project.inspection,
        "created_at": project.created_at,
    }


def validation_response(job):
    return {
        key: getattr(job, key)
        for key in (
            "id",
            "project_id",
            "package",
            "target_version",
            "execution_status",
            "phase",
            "verdict",
            "error_code",
            "error_message",
            "runner_id",
            "heartbeat_at",
            "created_at",
            "started_at",
            "finished_at",
            "cleanup_status",
            "analysis_status",
            "analysis",
        )
    } | {"snapshot": job.snapshot, "evidence": job.evidence}


class BodyLimitMiddleware:
    def __init__(self, app, limit=262144):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, length = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            length += len(message.get("body", b""))
            if length > self.limit:
                return await JSONResponse({"error": {"code": "BODY_TOO_LARGE"}}, status_code=413)(
                    scope, receive, send
                )
            chunks.append(message)
            if not message.get("more_body"):
                break

        async def replay():
            return chunks.pop(0) if chunks else await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None):
    settings = settings or get_settings()
    engine, sessions = database(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app):
        yield
        await engine.dispose()

    app = FastAPI(title="OSSGuard API", version="0.1.0", lifespan=lifespan)
    app.state.sessions = sessions
    app.state.engine = engine
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )
    security = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    ):
        token = settings.api_token.get_secret_value()
        if not token:
            raise GuardError("AUTH_NOT_CONFIGURED", "API token is not configured", 503)
        if not credentials or not hmac.compare_digest(credentials.credentials, token):
            raise GuardError("UNAUTHORIZED", "Bearer token required", 401)

    auth = [Depends(authorize)]

    @app.exception_handler(GuardError)
    async def guard_error(request: Request, exc: GuardError):
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo accidentally submitted credentials in validation responses.
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_INPUT",
                    "details": [
                        {"location": error["loc"], "type": error["type"], "message": error["msg"]}
                        for error in exc.errors()
                    ],
                }
            },
            status_code=422,
        )

    @app.get("/health")
    async def health():
        try:
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception:
            return JSONResponse({"status": "database_unavailable"}, status_code=503)

    async def get_project(session, project_id, lock=False):
        statement = select(Project).where(Project.id == project_id)
        project = await session.scalar(statement.with_for_update() if lock else statement)
        if not project:
            raise GuardError("PROJECT_NOT_FOUND", "Project not found", 404)
        return project

    @app.post("/api/projects", status_code=201, dependencies=auth, response_model=ProjectResponse)
    async def create_project(body: ProjectCreate):
        allowed_repository(body.repository_url, settings)
        async with sessions.begin() as session:
            project = Project(**body.model_dump())
            session.add(project)
            await session.flush()
            return project_response(project)

    @app.get("/api/projects", dependencies=auth, response_model=list[ProjectResponse])
    async def list_projects(limit: int = Query(30, ge=1, le=100)):
        async with sessions() as session:
            projects = await session.scalars(
                select(Project).order_by(Project.created_at.desc()).limit(limit)
            )
            return [project_response(project) for project in projects]

    @app.get("/api/projects/{project_id}", dependencies=auth, response_model=ProjectResponse)
    async def read_project(project_id: str):
        async with sessions() as session:
            project = await get_project(session, project_id)
            suites = await session.scalars(
                select(TestSuite).where(TestSuite.project_id == project_id)
            )
            return project_response(project) | {
                "test_suites": [
                    {
                        "id": suite.id,
                        "revision": suite.revision,
                        "source": suite.source,
                        **suite.content,
                    }
                    for suite in suites
                ]
            }

    @app.post("/api/projects/{project_id}/inspect", dependencies=auth)
    async def inspect_project(project_id: str):
        async with sessions() as session:
            project = await get_project(session, project_id)
            url, ref, cache, revision = (
                project.repository_url,
                project.ref,
                project.inspection,
                project.revision,
            )
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            result = await RepositoryService(settings, client).inspect(url, ref, cache)
        async with sessions.begin() as session:
            project = await get_project(session, project_id, lock=True)
            if project.revision != revision:
                raise GuardError("PROJECT_CHANGED", "Project changed during inspection; retry", 409)
            unchanged = project.commit_sha == result["sha"]
            project.commit_sha, project.inspection = result["sha"], result
            suite = None
            if not unchanged or not project.execution_profile:
                project.execution_profile = result["profile"]
                project.environment_profile = {}
                suite = TestSuite(
                    project_id=project.id, content=result["suite"], source="repository"
                )
                session.add(suite)
            project.revision += 1
            await session.flush()
            return {
                "commit_sha": result["sha"],
                "dependencies": result["dependencies"],
                "suite_id": suite.id if suite else None,
                "cached": unchanged,
            }

    @app.get("/api/projects/{project_id}/dependencies", dependencies=auth)
    async def dependencies(project_id: str):
        async with sessions() as session:
            project = await get_project(session, project_id)
            return project.inspection.get("dependencies", [])

    @app.get("/api/projects/{project_id}/dependencies/{name}/versions", dependencies=auth)
    async def versions(project_id: str, name: str):
        async with sessions() as session:
            project = await get_project(session, project_id)
            if name not in {dep["name"] for dep in project.inspection.get("dependencies", [])}:
                raise GuardError(
                    "DEPENDENCY_NOT_FOUND", "Select an inspected direct dependency", 404
                )
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            return {
                "package": name,
                "versions": await RepositoryService(settings, client).versions(name),
            }

    async def update_field(project_id, name, value):
        async with sessions.begin() as session:
            project = await get_project(session, project_id, lock=True)
            if name == "execution_profile":
                check_environment(value, project.environment_profile)
            elif name == "environment_profile":
                if not project.execution_profile:
                    raise GuardError(
                        "PROFILE_REQUIRED", "Inspect or configure execution profile first"
                    )
                check_environment(project.execution_profile, value)
                for ref in value.get("secret_refs", {}).values():
                    if not settings.secret_project_id or not ref.startswith(
                        f"projects/{settings.secret_project_id}/secrets/"
                    ):
                        raise GuardError(
                            "SECRET_PROJECT_DENIED", "Dedicated target secret project required"
                        )
            setattr(project, name, value)
            project.revision += 1
            return {"revision": project.revision, name: value}

    @app.put("/api/projects/{project_id}/execution-profile", dependencies=auth)
    async def execution_profile(project_id: str, body: ExecutionProfile):
        return await update_field(project_id, "execution_profile", body.model_dump())

    @app.get("/api/projects/{project_id}/environment-profile", dependencies=auth)
    async def environment_profile(project_id: str):
        async with sessions() as session:
            project = await get_project(session, project_id)
            return {
                "env_schema": project.execution_profile.get("env_schema", {}),
                **project.environment_profile,
            }

    @app.put("/api/projects/{project_id}/environment-profile", dependencies=auth)
    async def put_environment(project_id: str, body: EnvironmentUpdate):
        return await update_field(project_id, "environment_profile", body.model_dump())

    @app.put("/api/projects/{project_id}/rules", dependencies=auth)
    async def put_rules(project_id: str, body: RulesUpdate):
        return await update_field(project_id, "rules", body.model_dump())

    @app.put("/api/projects/{project_id}/notification-settings", dependencies=auth)
    async def put_notification(project_id: str, body: NotificationUpdate):
        return await update_field(project_id, "notification", body.model_dump())

    @app.post("/api/projects/{project_id}/test-suites", status_code=201, dependencies=auth)
    async def create_suite(project_id: str, body: SuiteUpdate):
        async with sessions.begin() as session:
            await get_project(session, project_id)
            suite = TestSuite(project_id=project_id, content=body.model_dump(), source="manual")
            session.add(suite)
            await session.flush()
            return {"id": suite.id, "revision": suite.revision, **suite.content}

    @app.post("/api/projects/{project_id}/test-suites/generate", status_code=201, dependencies=auth)
    async def generate(project_id: str):
        async with sessions() as session:
            project = await get_project(session, project_id)
            rules, existing = project.rules, project.inspection.get("suite", {})
        async with httpx.AsyncClient(trust_env=False) as client:
            suite = await generate_suite(settings, client, rules, existing)
        async with sessions.begin() as session:
            row = TestSuite(project_id=project_id, content=suite.model_dump(), source="ai")
            session.add(row)
            await session.flush()
            return {"id": row.id, "revision": row.revision, **row.content}

    @app.put("/api/test-suites/{suite_id}", dependencies=auth)
    async def update_suite(suite_id: str, body: SuiteUpdate):
        async with sessions.begin() as session:
            suite = await session.scalar(
                select(TestSuite).where(TestSuite.id == suite_id).with_for_update()
            )
            if not suite:
                raise GuardError("SUITE_NOT_FOUND", "Test suite not found", 404)
            suite.content = body.model_dump()
            suite.revision += 1
            return {"id": suite.id, "revision": suite.revision, **suite.content}

    @app.post(
        "/api/validations", status_code=202, dependencies=auth, response_model=ValidationSubmitted
    )
    async def start_validation(body: ValidationCreate):
        async def existing_job():
            async with sessions() as session:
                row = await session.scalar(
                    select(Validation).where(Validation.idempotency_key == body.idempotency_key)
                )
                if row and row.request != body.model_dump():
                    raise GuardError(
                        "IDEMPOTENCY_CONFLICT", "Key was already used for different inputs", 409
                    )
                return row

        if row := await existing_job():
            return {"id": row.id, "execution_status": row.execution_status}
        async with sessions() as session:
            project = await get_project(session, body.project_id)
            dep = next(
                (
                    d
                    for d in project.inspection.get("dependencies", [])
                    if d["name"] == body.package
                ),
                None,
            )
            if not project.commit_sha or not dep:
                raise GuardError(
                    "INSPECTION_REQUIRED", "Inspect and select a direct dependency first"
                )
            allowed_repository(project.repository_url, settings)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            await RepositoryService(settings, client).check_version(
                body.package, body.target_version
            )
        try:
            async with sessions.begin() as session:
                project = await get_project(session, body.project_id, lock=True)
                suite = await session.scalar(
                    select(TestSuite)
                    .where(TestSuite.id == body.suite_id, TestSuite.project_id == project.id)
                    .with_for_update()
                )
                if not suite or not suite.content.get("approved"):
                    raise GuardError("TESTS_NOT_APPROVED", "Approve a nonempty test suite first")
                check_environment(
                    project.execution_profile, project.environment_profile, require_values=True
                )
                dep = next(
                    d for d in project.inspection["dependencies"] if d["name"] == body.package
                )
                snapshot = {
                    "repository_url": project.repository_url,
                    "commit_sha": project.commit_sha,
                    "project_revision": project.revision,
                    "suite_revision": suite.revision,
                    "suite": suite.content,
                    "execution_profile": project.execution_profile,
                    "environment_profile": project.environment_profile,
                    "notification": project.notification,
                    "rules": project.rules,
                    "file_hashes": project.inspection["file_hashes"],
                    "baseline_version": dep["locked_version"],
                }
                job = Validation(
                    project_id=project.id,
                    idempotency_key=body.idempotency_key,
                    request=body.model_dump(),
                    package=body.package,
                    target_version=body.target_version,
                    snapshot=snapshot,
                )
                session.add(job)
                await session.flush()
                return {"id": job.id, "execution_status": job.execution_status}
        except IntegrityError:
            if row := await existing_job():
                return {"id": row.id, "execution_status": row.execution_status}
            raise

    @app.get("/api/validations", dependencies=auth, response_model=list[ValidationResponse])
    async def list_validations(project_id: str | None = None, limit: int = Query(30, ge=1, le=100)):
        async with sessions() as session:
            query = select(Validation).order_by(Validation.created_at.desc()).limit(limit)
            if project_id:
                query = query.where(Validation.project_id == project_id)
            return [validation_response(job) for job in await session.scalars(query)]

    @app.get("/api/validations/{run_id}", dependencies=auth, response_model=ValidationResponse)
    async def read_validation(run_id: str):
        async with sessions() as session:
            job = await session.get(Validation, run_id)
            if not job:
                raise GuardError("VALIDATION_NOT_FOUND", "Validation not found", 404)
            notifications = list(
                await session.scalars(
                    select(EmailNotification.status).where(
                        EmailNotification.validation_id == run_id
                    )
                )
            )
            return validation_response(job) | {"notification_statuses": notifications}

    @app.get("/api/validations/{run_id}/evidence", dependencies=auth)
    async def evidence(run_id: str):
        async with sessions() as session:
            job = await session.get(Validation, run_id)
            if not job:
                raise GuardError("VALIDATION_NOT_FOUND", "Validation not found", 404)
            tests = await session.scalars(
                select(TestResult).where(TestResult.validation_id == run_id)
            )
            return {
                "evidence": job.evidence,
                "test_results": [
                    {"case_id": t.case_id, "environment": t.environment, **t.result} for t in tests
                ],
            }

    @app.get("/api/validations/{run_id}/notifications", dependencies=auth)
    async def notifications(run_id: str):
        async with sessions() as session:
            if not await session.get(Validation, run_id):
                raise GuardError("VALIDATION_NOT_FOUND", "Validation not found", 404)
            rows = await session.scalars(
                select(EmailNotification).where(EmailNotification.validation_id == run_id)
            )
            return [
                {
                    "id": row.id,
                    "status": row.status,
                    "attempts": row.attempts,
                    "provider_id": row.provider_id,
                    "error": row.error,
                }
                for row in rows
            ]

    return app


app = create_app()
