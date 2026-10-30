import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from ossguard.api import create_app
from ossguard.config import Settings
from ossguard.db import EmailNotification, Project, Validation, now
from ossguard.docker_runner import DockerRunner
from ossguard.repository import RepositoryService, parse_project
from ossguard.result_worker import ResultWorker
from ossguard.runner import ExecutionWorker
from ossguard.schemas import ExecutionProfile, SuiteUpdate
from ossguard.updater import PythonUvUpdater

pytestmark = pytest.mark.integration
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "purchase-app"
REPO = "https://github.com/ossguard-test/purchase-app"


def sample_inspection():
    profile = ExecutionProfile.model_validate_json((SAMPLE / "ossguard.json").read_text())
    suite = SuiteUpdate(
        cases=json.loads((SAMPLE / profile.test_cases).read_text()),
        stubs=json.loads((SAMPLE / profile.mock_stubs / "stubs.json").read_text()),
    )
    return {
        "sha": "f" * 40,
        "dependencies": parse_project(
            (SAMPLE / "pyproject.toml").read_text(), (SAMPLE / "uv.lock").read_text()
        ),
        "profile": profile.model_dump(),
        "suite": suite.model_dump(),
        "file_hashes": {
            name: hashlib.sha256((SAMPLE / name).read_bytes()).hexdigest()
            for name in [
                "pyproject.toml",
                "uv.lock",
                "ossguard.json",
                "Dockerfile.ossguard",
                "app.py",
            ]
        },
    }


async def setup_api(postgres_url, monkeypatch, tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=postgres_url,
        api_token="test-team-token",
        allowed_repos=[REPO],
        work_dir=tmp_path / "runs",
        ai_key="",
        email_key="",
    )

    async def inspect(*args, **kwargs):
        return sample_inspection()

    async def check_version(*args, **kwargs):
        return None

    monkeypatch.setattr(RepositoryService, "inspect", inspect)
    monkeypatch.setattr(RepositoryService, "check_version", check_version)
    app = create_app(settings)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-team-token"},
    )
    project_response = await client.post(
        "/api/projects", json={"name": "Purchase demo", "repository_url": REPO}
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]
    inspected = await client.post(f"/api/projects/{project_id}/inspect")
    assert inspected.status_code == 200, inspected.text
    suite_id = inspected.json()["suite_id"]
    suite = sample_inspection()["suite"] | {"approved": True}
    response = await client.put(f"/api/test-suites/{suite_id}", json=suite)
    assert response.status_code == 200, response.text
    return settings, app, client, project_id, suite_id


async def test_api_snapshots_idempotency_and_auth(db, postgres_url, monkeypatch, tmp_path):
    _, app, client, project_id, suite_id = await setup_api(postgres_url, monkeypatch, tmp_path)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as anonymous:
            assert (await anonymous.get("/api/projects")).status_code == 401
        payload = {
            "project_id": project_id,
            "package": "pydantic",
            "target_version": "2.12.5",
            "suite_id": suite_id,
            "idempotency_key": "test-idempotent-1",
        }
        first = await client.post("/api/validations", json=payload)
        assert first.status_code == 202, first.text
        second = await client.post("/api/validations", json=payload)
        assert first.json()["id"] == second.json()["id"]
        conflict = await client.post(
            "/api/validations", json=payload | {"target_version": "1.10.24"}
        )
        assert conflict.status_code == 409
        await client.put(
            f"/api/projects/{project_id}/rules", json={"description": "changed after enqueue"}
        )
        job = (await client.get(f"/api/validations/{first.json()['id']}")).json()
        assert job["snapshot"]["rules"] == {}
        assert job["execution_status"] == "QUEUED"
        too_large = await client.post("/api/projects", content=b"x" * 270000)
        assert too_large.status_code == 413
    finally:
        await client.aclose()
        await app.state.engine.dispose()


@pytest.mark.parametrize(
    "target,baseline_invalid,expected",
    [
        ("2.12.5", False, "REGRESSION_SUSPECTED"),
        ("1.10.24", False, "PASS"),
        ("2.12.5", True, "BASELINE_INVALID"),
    ],
)
async def test_real_docker_update_and_business_comparison(
    db, postgres_url, monkeypatch, tmp_path, target, baseline_invalid, expected
):
    settings, app, client, project_id, suite_id = await setup_api(
        postgres_url, monkeypatch, tmp_path
    )
    _, sessions = db

    # Only source transport/preflight is substituted; uv, Docker, HTTP and WireMock are real.
    async def collect(self, url, sha, root):
        shutil.copytree(SAMPLE, root)

    monkeypatch.setattr(PythonUvUpdater, "collect", collect)
    try:
        if baseline_invalid:
            suite = sample_inspection()["suite"] | {"approved": True}
            suite["cases"][0]["assertions"][0]["value"] = 99
            await client.put(f"/api/test-suites/{suite_id}", json=suite)
        await client.put(
            f"/api/projects/{project_id}/notification-settings",
            json={"enabled": True, "email": "demo@example.com"},
        )
        response = await client.post(
            "/api/validations",
            json={
                "project_id": project_id,
                "package": "pydantic",
                "target_version": target,
                "suite_id": suite_id,
                "idempotency_key": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        worker = ExecutionWorker(settings, sessions)
        job = await worker.claim()
        assert job
        assert await worker.claim() is None
        await worker.execute(job)
        result = (await client.get(f"/api/validations/{job.id}")).json()
        assert result["execution_status"] == "COMPLETED", json.dumps(
            {key: result[key] for key in ("error_code", "error_message", "evidence")}, indent=2
        )
        assert result["verdict"] == expected, result
        assert result["cleanup_status"] == "SUCCEEDED", result
        assert not (settings.work_dir / job.id).exists()
        evidence = (await client.get(f"/api/validations/{job.id}/evidence")).json()
        assert len(evidence["test_results"]) == (4 if baseline_invalid else 8)
        if not baseline_invalid:
            assert evidence["evidence"]["candidate"]["installed_packages"]["pydantic"] == target
            assert evidence["evidence"]["update"]["source_unchanged"]
            if target == "2.12.5":
                t01 = next(
                    r
                    for r in evidence["test_results"]
                    if r["environment"] == "candidate" and r["case_id"] == "T01"
                )
                assert t01["status_code"] == 200
                assert t01["events"] == []
                assert t01["passed"] is False
        result_worker = ResultWorker(settings, sessions)
        async with httpx.AsyncClient() as external:
            assert await result_worker.process_result(external)
            assert await result_worker.process_result(external) is False
            assert await result_worker.process_email(external)
        after = (await client.get(f"/api/validations/{job.id}")).json()
        assert after["analysis_status"] == "SKIPPED"
        assert after["notification_statuses"] == ["FAILED"]  # no real key in tests
    finally:
        await client.aclose()
        await app.state.engine.dispose()


async def test_expired_foreign_runner_is_not_reclaimed(db, tmp_path):
    _, sessions = db
    async with sessions.begin() as session:
        project = Project(name="x", repository_url=REPO, ref="main")
        session.add(project)
        await session.flush()
        session.add(
            Validation(
                project_id=project.id,
                idempotency_key="foreign-run",
                request={},
                snapshot={},
                package="x",
                target_version="1",
                execution_status="RUNNING",
                runner_id="other-vm",
                heartbeat_at=now(),
            )
        )
    worker = ExecutionWorker(Settings(_env_file=None, work_dir=tmp_path), sessions)
    assert await worker.claim() is None


async def test_email_timeout_retry_uses_same_idempotency_key(db):
    _, sessions = db
    async with sessions.begin() as session:
        project = Project(name="x", repository_url=REPO, ref="main")
        session.add(project)
        await session.flush()
        job = Validation(
            project_id=project.id,
            idempotency_key="email-run",
            request={},
            snapshot={},
            package="x",
            target_version="1",
        )
        session.add(job)
        await session.flush()
        session.add(
            EmailNotification(
                validation_id=job.id,
                event_key="event-unique",
                recipient="demo@example.com",
                payload={"text": "stable"},
            )
        )
    worker = ResultWorker(
        Settings(_env_file=None, email_key="fake-provider-key", email_from="test@example.com"),
        sessions,
    )
    requests = []

    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={"id": "provider-id"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        await worker.process_email(client)
        async with sessions.begin() as session:
            row = await session.scalar(select(EmailNotification))
            assert row.status == "PENDING"
            row.next_attempt_at = now()
        await worker.process_email(client)
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert requests[0].content == requests[1].content
    async with sessions() as session:
        row = await session.scalar(select(EmailNotification))
        assert row.status == "ACCEPTED" and row.attempts == 2


async def test_atomic_claim_and_cleanup_recovery(db, tmp_path, monkeypatch):
    _, sessions = db
    async with sessions.begin() as session:
        project = Project(name="x", repository_url=REPO, ref="main")
        session.add(project)
        await session.flush()
        for index in range(2):
            session.add(
                Validation(
                    project_id=project.id,
                    idempotency_key=f"queue-{index}",
                    request={},
                    snapshot={},
                    package="x",
                    target_version="1",
                )
            )
    settings = Settings(_env_file=None, work_dir=tmp_path, runner_id="same-host")
    worker = ExecutionWorker(settings, sessions)
    claims = await asyncio.gather(worker.claim(), worker.claim())
    assert sum(job is not None for job in claims) == 1
    job = next(job for job in claims if job)
    work = tmp_path / job.id
    work.mkdir()
    (work / "intermediate").write_text("owned data")
    cleaned = []

    async def cleanup(docker):
        cleaned.append(docker.run_id)
        return {"status": "SUCCEEDED", "errors": []}

    monkeypatch.setattr(DockerRunner, "cleanup", cleanup)
    await worker.recover()
    async with sessions() as session:
        recovered = await session.get(Validation, job.id)
        assert recovered.execution_status == "INTERRUPTED"
        assert recovered.cleanup_status == "SUCCEEDED"
    assert cleaned == [job.id] and not work.exists()
    next_job = await worker.claim()
    assert next_job
    await worker.save(
        next_job.id, execution_status="COMPLETED", verdict="PASS", cleanup_status="FAILED"
    )
    assert await worker.claim() is None
    await worker.recover()
    async with sessions() as session:
        recovered = await session.get(Validation, next_job.id)
        assert recovered.verdict == "PASS" and recovered.cleanup_status == "SUCCEEDED"


async def test_startup_failure_keeps_logs_and_cleans_containers(
    db, postgres_url, monkeypatch, tmp_path
):
    settings, app, client, project_id, suite_id = await setup_api(
        postgres_url, monkeypatch, tmp_path
    )
    settings.startup_timeout = 2

    async def collect(self, url, sha, root):
        shutil.copytree(SAMPLE, root)

    monkeypatch.setattr(PythonUvUpdater, "collect", collect)
    try:
        profile = sample_inspection()["profile"] | {"health_path": "/missing-health"}
        response = await client.put(f"/api/projects/{project_id}/execution-profile", json=profile)
        assert response.status_code == 200
        response = await client.post(
            "/api/validations",
            json={
                "project_id": project_id,
                "package": "pydantic",
                "target_version": "2.12.5",
                "suite_id": suite_id,
                "idempotency_key": "startup-failure",
            },
        )
        assert response.status_code == 202
        worker = ExecutionWorker(settings, db[1])
        job = await worker.claim()
        await worker.execute(job)
        result = (await client.get(f"/api/validations/{job.id}")).json()
        assert result["error_code"] == "STARTUP_TIMEOUT", result
        assert result["execution_status"] == "FAILED"
        assert result["verdict"] == "NOT_EVALUATED"
        assert result["cleanup_status"] == "SUCCEEDED"
        assert result["evidence"]["container_logs"]
        assert len(result["evidence"]["not_evaluated_case_ids"]) == 4
    finally:
        await client.aclose()
        await app.state.engine.dispose()
