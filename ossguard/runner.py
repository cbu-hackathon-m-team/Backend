import asyncio
import contextlib
import logging
import os
import shutil
import stat
import tomllib

import httpx
from packaging.utils import canonicalize_name
from sqlalchemy import or_, select, text, update

from ossguard.config import Settings, get_settings
from ossguard.db import TestResult, Validation, database, now
from ossguard.docker_runner import DockerRunner
from ossguard.errors import GuardError
from ossguard.evidence import Redactor, bounded_text, bounded_value
from ossguard.locking import process_lock
from ossguard.schemas import HttpCase
from ossguard.secrets import resolve_environment
from ossguard.updater import PythonUvUpdater, package_diff, source_hashes
from ossguard.validator import compare_results, execute_case

logger = logging.getLogger(__name__)
RUNNER_LOCK = 73501001


def lock_versions(path):
    packages = tomllib.loads(path.read_text())["package"]
    return {
        f"{canonicalize_name(p['name'])}": p["version"]
        for p in packages
        if p.get("source", {}).get("registry")
    }


class ExecutionWorker:
    def __init__(self, settings: Settings, sessions):
        self.settings = settings
        self.sessions = sessions

    async def claim(self):
        async with self.sessions.begin() as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(73501002)"))
            if await session.scalar(
                select(Validation.id)
                .where(
                    or_(
                        Validation.execution_status == "RUNNING",
                        Validation.cleanup_status == "FAILED",
                    )
                )
                .limit(1)
            ):
                # A stale heartbeat cannot prove that another VM stopped executing.
                return None
            job = await session.scalar(
                select(Validation)
                .where(Validation.execution_status == "QUEUED")
                .order_by(Validation.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not job:
                return None
            job.execution_status = "RUNNING"
            job.runner_id = self.settings.runner_id
            job.started_at = job.heartbeat_at = now()
            return job

    async def heartbeat(self, run_id):
        while True:
            async with self.sessions.begin() as session:
                await session.execute(
                    update(Validation)
                    .where(
                        Validation.id == run_id,
                        Validation.execution_status == "RUNNING",
                        Validation.runner_id == self.settings.runner_id,
                    )
                    .values(heartbeat_at=now())
                )
            await asyncio.sleep(self.settings.heartbeat_seconds)

    async def save(self, run_id, **values):
        async with self.sessions.begin() as session:
            await session.execute(
                update(Validation).where(Validation.id == run_id).values(**values)
            )

    async def recover(self):
        async with self.sessions() as session:
            jobs = (
                await session.scalars(
                    select(Validation).where(
                        or_(
                            Validation.execution_status == "RUNNING",
                            Validation.cleanup_status == "FAILED",
                        ),
                        Validation.runner_id == self.settings.runner_id,
                    )
                )
            ).all()
        for job in jobs:
            work = self.settings.work_dir.resolve() / job.id
            docker = DockerRunner(self.settings, job.id, work)
            cleanup = await docker.cleanup()
            if cleanup["status"] == "SUCCEEDED":
                self.remove_work(work)
            values = {
                "cleanup_status": cleanup["status"],
                "evidence": {**job.evidence, "recovery_cleanup": cleanup},
            }
            if job.execution_status == "RUNNING":
                values.update(
                    execution_status="INTERRUPTED",
                    verdict="NOT_EVALUATED",
                    error_code="RUNNER_INTERRUPTED",
                    error_message="Previous runner process ended; inspect saved partial evidence",
                    finished_at=now(),
                )
            await self.save(job.id, **values)

    def remove_work(self, path):
        base = self.settings.work_dir.resolve()
        target = path.resolve()
        if target.parent != base or target == base:
            raise GuardError("INVALID_WORK_PATH", "Cleanup path is outside the run workspace")
        if target.exists():

            def retry_readonly(function, failed_path, error):
                if os.name != "nt" or not isinstance(error, PermissionError):
                    raise error
                # Git marks packed objects read-only on Windows.
                os.chmod(failed_path, os.stat(failed_path).st_mode | stat.S_IWRITE)
                function(failed_path)

            shutil.rmtree(target, onexc=retry_readonly)

    async def execute(self, job: Validation):
        run_id = job.id
        work = self.settings.work_dir.resolve() / run_id
        docker = DockerRunner(self.settings, run_id, work)
        evidence = {
            "phase_times": {},
            "not_evaluated_case_ids": [c["id"] for c in job.snapshot["suite"]["cases"]],
        }
        redactor = Redactor()
        status, verdict, error_code, error_message = "COMPLETED", "NOT_EVALUATED", None, None
        phase = "source"
        heartbeat = asyncio.create_task(self.heartbeat(run_id))

        async def checkpoint(next_phase=None):
            nonlocal phase
            if next_phase:
                phase = next_phase
                evidence["phase_times"][phase] = now().isoformat()
            await self.save(
                run_id,
                phase=phase,
                evidence=bounded_value(evidence, self.settings.evidence_bytes, redactor),
            )

        async def cases_for(client, environment, app_url, mock_url):
            results = []
            for case_data in job.snapshot["suite"]["cases"]:
                case = HttpCase.model_validate(case_data)
                result = await execute_case(
                    client, app_url, mock_url, case, self.settings.response_bytes
                )
                retained = bounded_value(result, 200_000, redactor)
                async with self.sessions.begin() as session:
                    session.add(
                        TestResult(
                            validation_id=run_id,
                            case_id=case.id,
                            environment=environment,
                            result=retained,
                        )
                    )
                results.append(result)
                evidence.setdefault("tests", {}).setdefault(environment, []).append(
                    redactor.clean({k: v for k, v in result.items() if k not in {"events", "body"}})
                )
                await checkpoint()
            return results

        try:
            work.mkdir(parents=True, exist_ok=False)
            values, secret_values = await resolve_environment(self.settings, job.snapshot)
            redactor = Redactor(
                [
                    *secret_values,
                    self.settings.ai_key.get_secret_value(),
                    self.settings.email_key.get_secret_value(),
                    self.settings.github_token.get_secret_value(),
                    self.settings.api_token.get_secret_value(),
                ]
            )
            await checkpoint("source")
            updater = PythonUvUpdater(self.settings)
            baseline_source, candidate_source = work / "a", work / "b"
            await asyncio.wait_for(
                updater.collect(
                    job.snapshot["repository_url"], job.snapshot["commit_sha"], baseline_source
                ),
                self.settings.source_timeout,
            )
            hashes = source_hashes(baseline_source)
            for name, expected in job.snapshot["file_hashes"].items():
                if hashes.get(name) != expected:
                    raise GuardError(
                        "SOURCE_MISMATCH", f"Source file differs from inspection: {name}"
                    )
            evidence["source"] = {"commit_sha": job.snapshot["commit_sha"], "hashes": hashes}
            await docker.create_network()
            profile = job.snapshot["execution_profile"]
            stubs = job.snapshot["suite"]["stubs"]
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout, follow_redirects=False, trust_env=False
            ) as client:
                await checkpoint("baseline_build")
                tag_a, image_a, build_log = await docker.build(baseline_source, "baseline")
                evidence["baseline"] = {"image_id": image_a, "build_log": redactor.clean(build_log)}
                await checkpoint()
                mock_a = await docker.start_mock("baseline", client, stubs, profile["app_port"])
                env_a = {
                    **values,
                    **{
                        k: "http://mock-baseline:8080"
                        for k, v in profile["env_schema"].items()
                        if v["source"] == "runner_mock"
                    },
                }
                app_a, inventory_a = await docker.start_app(
                    tag_a, "baseline", profile, env_a, client
                )
                evidence["baseline"]["installed_packages"] = inventory_a
                if inventory_a.get(job.package) != job.snapshot["baseline_version"]:
                    raise GuardError(
                        "VERSION_MISMATCH", "Baseline installed version differs from lock"
                    )
                await checkpoint("baseline_test")
                results_a = await cases_for(client, "baseline", app_a, mock_a)
                if not all(result["passed"] for result in results_a):
                    verdict = compare_results(results_a, [])
                    return
                await checkpoint("update")
                changed = await updater.update(
                    baseline_source, candidate_source, job.package, job.target_version
                )
                evidence["update"] = {
                    **{k: v for k, v in changed.items() if k != "file_diffs"},
                    "file_diffs": {
                        name: bounded_text(value, self.settings.log_bytes * 2, redactor)
                        for name, value in changed["file_diffs"].items()
                    },
                    "lock_package_diff": package_diff(
                        lock_versions(baseline_source / "uv.lock"),
                        lock_versions(candidate_source / "uv.lock"),
                    ),
                    "no_change_control": job.snapshot["baseline_version"] == job.target_version,
                }
                await checkpoint("candidate_build")
                tag_b, image_b, build_log = await docker.build(candidate_source, "candidate")
                evidence["candidate"] = {
                    "image_id": image_b,
                    "build_log": redactor.clean(build_log),
                }
                await checkpoint()
                mock_b = await docker.start_mock("candidate", client, stubs, profile["app_port"])
                env_b = {
                    **values,
                    **{
                        k: "http://mock-candidate:8080"
                        for k, v in profile["env_schema"].items()
                        if v["source"] == "runner_mock"
                    },
                }
                app_b, inventory_b = await docker.start_app(
                    tag_b, "candidate", profile, env_b, client
                )
                evidence["candidate"]["installed_packages"] = inventory_b
                evidence["installed_package_diff"] = package_diff(inventory_a, inventory_b)
                if inventory_b.get(job.package) != job.target_version:
                    raise GuardError(
                        "VERSION_MISMATCH", "Candidate did not install the requested version"
                    )
                await checkpoint("candidate_test")
                results_b = await cases_for(client, "candidate", app_b, mock_b)
                evidence["not_evaluated_case_ids"] = []
                await checkpoint("compare")
                verdict = compare_results(results_a, results_b)
        except GuardError as exc:
            status, error_code, error_message = "FAILED", exc.code, redactor.text(exc.message)
            if hasattr(exc, "log"):
                evidence["failure_log"] = redactor.clean(exc.log)
        except TimeoutError:
            status, error_code, error_message = (
                "FAILED",
                "STAGE_TIMEOUT",
                f"Stage timed out: {phase}",
            )
        except asyncio.CancelledError:
            status, error_code, error_message = (
                "INTERRUPTED",
                "RUNNER_INTERRUPTED",
                "Runner was stopped",
            )
            raise
        except Exception as exc:
            # Keep target logs in masked evidence, never in an unfiltered traceback.
            logger.error("run %s failed: %s", run_id, type(exc).__name__)
            status, error_code, error_message = "FAILED", "INTERNAL_ERROR", type(exc).__name__
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            evidence["last_execution_phase"] = phase
            try:
                evidence["container_logs"] = redactor.clean(await docker.logs())
            except Exception:
                evidence["container_logs"] = {"unavailable": True}
            # Save execution evidence before deleting any container or source file.
            await self.save(
                run_id,
                evidence=bounded_value(evidence, self.settings.evidence_bytes, redactor),
                phase="cleanup",
            )
            cleanup = await docker.cleanup()
            if cleanup["status"] == "SUCCEEDED":
                try:
                    self.remove_work(work)
                except OSError:
                    cleanup = {"status": "FAILED", "errors": ["workspace removal failed"]}
            evidence["cleanup"] = cleanup
            await self.save(
                run_id,
                execution_status=status,
                phase="finished",
                verdict=verdict,
                error_code=error_code,
                error_message=error_message,
                finished_at=now(),
                cleanup_status=cleanup["status"],
                evidence=bounded_value(evidence, self.settings.evidence_bytes, redactor),
            )


async def serve():
    settings = get_settings()
    engine, sessions = database(settings.database_url.get_secret_value())
    worker = ExecutionWorker(settings, sessions)
    try:
        with process_lock(settings.work_dir.resolve() / f"{settings.runner_id}.lock"):
            async with engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                if not await connection.scalar(text(f"SELECT pg_try_advisory_lock({RUNNER_LOCK})")):
                    raise RuntimeError("Another runner owns the global execution lock")
                await worker.recover()
                logger.info("Runner ready: %s", settings.runner_id)
                while True:
                    await connection.execute(text("SELECT 1"))
                    job = await worker.claim()
                    if job:
                        await worker.execute(job)
                    else:
                        await asyncio.sleep(settings.poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
