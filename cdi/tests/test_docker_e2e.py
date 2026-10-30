"""Opt-in real Node-RED/PostgreSQL test. Never silently replaces Docker with a fake."""

import os
from pathlib import Path

import pytest

from ossguard.engine import Engine
from ossguard.node_red import NodeRedAdapter
from ossguard.storage import Repository

pytestmark = pytest.mark.skipif(
    os.environ.get("OSSGUARD_DOCKER_TESTS") != "1",
    reason="requires prepared Docker images and migrated PostgreSQL",
)


async def test_real_normal_broken_fixed(snapshot):
    repository = Repository(os.environ["DATABASE_URL"])
    adapter = NodeRedAdapter(Path(__file__).resolve().parents[1])
    engine = Engine(repository, {"node-red": adapter})
    await repository.own_worker()
    try:
        saved_ids = []
        for artifact in ("normal", "missing_shipping", "fixed"):
            snapshot.candidate.artifact = artifact
            run_id = await engine.submit(snapshot)
            saved_ids.append(run_id)
            await engine.run(run_id)
            saved = await repository.get(run_id)
            assert saved["status"] == "COMPLETED", saved
            assert saved["report"]["cleanup_status"] == "COMPLETE", saved
            expected = (
                ["REGRESSION_SUSPECTED", "REGRESSION_SUSPECTED", "PASS", "PASS"]
                if artifact == "missing_shipping"
                else ["PASS"] * 4
            )
            assert [row["verdict"] for row in saved["report"]["comparisons"]] == expected, saved
            assert saved["report"]["environments"]["baseline"]["image"].startswith("sha256:")
        assert (await repository.get(saved_ids[1]))["report"]["comparisons"][0][
            "verdict"
        ] == "REGRESSION_SUSPECTED"
    finally:
        await repository.close()
