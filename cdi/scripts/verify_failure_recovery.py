"""Real Docker failures in a dedicated temporary PostgreSQL container.

Never reads .env or DATABASE_URL from the caller. Never stops the API server.
Only the subprocesses and UUID-labeled resources created here are terminated.
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import docker
import httpx
from sqlalchemy.engine import URL

from ossguard.node_red import NodeRedAdapter
from ossguard.storage import Repository

ROOT = Path(__file__).resolve().parents[1]


def counts(client, run_id):
    labels = ["ossguard.owner=execution-v1", f"ossguard.run={UUID(run_id)}"]
    return {
        "containers": len(client.containers.list(all=True, filters={"label": labels})),
        "volumes": len(client.volumes.list(filters={"label": labels})),
        "networks": len(client.networks.list(filters={"label": labels})),
    }


def health():
    try:
        return httpx.get("http://127.0.0.1:8000/health/live", timeout=3, trust_env=False).status_code
    except httpx.HTTPError:
        return None


async def read_run(url, run_id):
    repository = Repository(url)
    try:
        return await repository.get(run_id)
    finally:
        await repository.close()


def run_worker(mode, directory, env, kill=False):
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "tests/runtime_failure_worker.py"), mode, str(directory)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (directory / f"{mode}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log, creationflags=flags)
        try:
            if kill:
                deadline = time.monotonic() + 120
                while not (directory / "ready-to-kill").exists():
                    if process.poll() is not None:
                        raise AssertionError("Crash worker exited before the kill checkpoint")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Crash checkpoint was not reached")
                    time.sleep(0.2)
                process.kill()
                process.wait(timeout=20)
            else:
                if process.wait(timeout=180) != 0:
                    raise AssertionError(f"{mode} failed; inspect its test log")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=20)


def main():
    suite_id = uuid4().hex
    output = ROOT / "artifacts/failure-tests" / suite_id
    output.mkdir(parents=True)
    client = docker.from_env(timeout=30)
    db = None
    report = {
        "suite_id": suite_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "api_before": health(),
        "cases": [],
        "status": "FAILED",
    }
    existing = {container.id: container.attrs["State"]["StartedAt"] for container in client.containers.list()}
    password = secrets.token_hex(24)
    try:
        # Local image lookup prevents unexpected downloads during fault tests.
        db_image = client.images.get("postgres:16-alpine")
        db = client.containers.run(
            db_image.id,
            detach=True,
            name=f"ossguard-fault-db-{suite_id[:12]}",
            environment={
                "POSTGRES_USER": "fault_test",
                "POSTGRES_DB": "fault_test",
                "POSTGRES_PASSWORD": password,
            },
            labels={"ossguard.failure_suite": suite_id},
            ports={"5432/tcp": ("127.0.0.1", None)},
            tmpfs={"/var/lib/postgresql/data": "rw,size=256m"},
            mem_limit="384m",
        )
        deadline = time.monotonic() + 60
        while db.exec_run(["pg_isready", "-U", "fault_test", "-d", "fault_test"]).exit_code:
            if time.monotonic() >= deadline:
                raise TimeoutError("Isolated PostgreSQL did not start")
            time.sleep(0.5)
        db.reload()
        port = int(db.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
        url = URL.create(
            "postgresql+asyncpg",
            username="fault_test",
            password=password,
            host="127.0.0.1",
            port=port,
            database="fault_test",
        ).render_as_string(hide_password=False)
        env = {**os.environ, "DATABASE_URL": url}
        report["isolated_db_port"] = port
        with (output / "migration.log").open("w", encoding="utf-8") as log:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env=env,
                cwd=ROOT,
                stdout=log,
                stderr=log,
                check=True,
                timeout=60,
            )

        def save(mode, run_id, expected_status, expected_counts):
            stored = asyncio.run(read_run(url, run_id))
            (output / mode / "evidence.json").write_text(json.dumps(stored, indent=2), encoding="utf-8")
            assert stored["status"] == expected_status, (mode, stored["status"])
            resources = counts(client, run_id)
            assert resources == expected_counts, (mode, resources)
            return stored, resources

        empty = {"containers": 0, "volumes": 0, "networks": 0}
        for mode in ("preparation", "timeout", "crash", "cleanup", "normal"):
            print(f"Running isolated scenario: {mode}", flush=True)
            directory = output / mode
            run_worker(mode, directory, env, kill=mode == "crash")
            run_id = (directory / "run-id.txt").read_text().strip()
            before = asyncio.run(read_run(url, run_id))
            if mode in {"crash", "cleanup"}:
                resources_before = counts(client, run_id)
                assert resources_before["containers"] == 6
                if mode == "crash":
                    assert before["status"] == "RUNNING_BASELINE"
                    assert len(before["results"]) == 1 and before["results"][0]["status"] == "COMPLETE"
                else:
                    assert before["report"]["cleanup_status"] == "PENDING"
                    assert (directory / "duplicate-blocked").exists()
                (directory / "before-recovery.json").write_text(
                    json.dumps(before, indent=2), encoding="utf-8"
                )
                run_worker("recover", directory, env)
            expected = (
                "INTERRUPTED"
                if mode == "crash"
                else "FAILED"
                if mode in {"preparation", "timeout"}
                else "COMPLETED"
            )
            stored, remaining = save(mode, run_id, expected, empty)
            assert stored["report"]["cleanup_status"] == "COMPLETE"
            assert len(stored["results"]) == 8
            if mode == "timeout":
                errors = [row for row in stored["results"] if row["status"] == "ERROR"]
                assert len(errors) == 2 and all(row["error"] == "ReadTimeout" for row in errors)
                assert all(
                    any(event["event_type"] == "shipping_requested" for event in row["events"])
                    for row in errors
                )
                assert sum(row["status"] == "UNVERIFIED" for row in stored["results"]) == 6
                assert stored["report"]["logs"]
            elif mode == "crash":
                assert stored["report"]["error"] == "server_restart"
                assert stored["report"]["recovery_logs"]
                assert sum(row["status"] == "COMPLETE" for row in stored["results"]) == 1
                assert stored["results"][0] == before["results"][0]
            elif mode == "preparation":
                assert stored["report"]["logs"]
                assert all(row["status"] == "UNVERIFIED" for row in stored["results"])
            else:
                assert stored["report"]["all_passed"] is True
            if mode in {"timeout", "crash", "preparation"}:
                assert stored["report"]["all_passed"] is False
            report["cases"].append(
                {
                    "scenario": mode,
                    "run_id": run_id,
                    "status": stored["status"],
                    "cleanup": stored["report"]["cleanup_status"],
                    "remaining": remaining,
                    "assertions": "PASS",
                }
            )
            print(f"PASS: {mode}; evidence retained; run resources removed", flush=True)
        report["status"] = "PASSED"
    finally:
        # Only clean this suite's recorded run IDs, never globally prune resources.
        adapter = NodeRedAdapter(ROOT, client)
        report["final_cleanup"] = []
        for file in output.glob("*/run-id.txt"):
            run_id = UUID(file.read_text().strip())
            report["final_cleanup"].append(
                {"run_id": str(run_id), **asyncio.run(adapter.cleanup(run_id)).model_dump()}
            )
        if db is not None:
            db.remove(force=True, v=True)
        report["api_after"] = health()
        report["existing_containers_unchanged"] = all(
            client.containers.get(cid).attrs["State"]["StartedAt"] == started
            for cid, started in existing.items()
        )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        client.close()
        print(f"Evidence directory: {output}", flush=True)
    assert report["existing_containers_unchanged"]
    assert report["api_after"] == report["api_before"]


if __name__ == "__main__":
    main()
