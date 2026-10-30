import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from ossguard.db import database

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def postgres_url():
    name = f"ossguard-test-db-{uuid4().hex[:10]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "ossguard.test=true",
            "-e",
            "POSTGRES_USER=ossguard",
            "-e",
            "POSTGRES_PASSWORD=test-only-password",
            "-e",
            "POSTGRES_DB=ossguard_test",
            "-p",
            "127.0.0.1::5432",
            "postgres:17.6-alpine",
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )
    try:
        for _ in range(60):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "ossguard"],
                capture_output=True,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("test postgres did not start")
        inspected = json.loads(
            subprocess.run(
                ["docker", "inspect", name], check=True, capture_output=True, text=True
            ).stdout
        )[0]
        port = inspected["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        url = f"postgresql+asyncpg://ossguard:test-only-password@127.0.0.1:{port}/ossguard_test"
        env = {**os.environ, "OSSGUARD_DATABASE_URL": url}
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


@pytest_asyncio.fixture
async def db(postgres_url):
    engine, sessions = database(postgres_url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE test_results, email_notifications, validations, "
                "test_suites, projects CASCADE"
            )
        )
    try:
        yield engine, sessions
    finally:
        await engine.dispose()
