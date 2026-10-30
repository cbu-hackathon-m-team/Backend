import asyncio

import pytest

from ossguard.engine import Engine
from ossguard.storage import BusyError


async def test_missing_shipping_and_fixed_run_keep_history(repository, snapshot, fake):
    engine = Engine(repository, {"node-red": fake})
    broken_id = await engine.submit(snapshot)
    await engine.run(broken_id)
    broken = await repository.get(broken_id)
    assert [c["verdict"] for c in broken["report"]["comparisons"]] == [
        "REGRESSION_SUSPECTED",
        "REGRESSION_SUSPECTED",
        "PASS",
        "PASS",
    ]
    assert broken["status"] == "COMPLETED"
    assert len(broken["results"]) == 8
    assert broken["report"]["cleanup_status"] == "COMPLETE"
    snapshot.candidate.artifact = "fixed"
    fixed_id = await engine.submit(snapshot)
    await engine.run(fixed_id)
    assert fixed_id != broken_id
    assert (await repository.get(fixed_id))["report"]["all_passed"]
    assert (await repository.get(broken_id))["snapshot"]["candidate"]["artifact"] == "missing_shipping"
    assert not (await repository.get(broken_id))["report"]["all_passed"]


async def test_bad_baseline_is_not_new_regression(repository, snapshot, fake):
    snapshot.baseline.artifact = "missing_shipping"
    engine = Engine(repository, {"node-red": fake})
    run_id = await engine.submit(snapshot)
    await engine.run(run_id)
    report = (await repository.get(run_id))["report"]
    assert report["comparisons"][0]["verdict"] == "BASELINE_REVIEW"


async def test_unconfirmed_and_duplicate_rejected(repository, snapshot, fake):
    engine = Engine(repository, {"node-red": fake})
    snapshot.confirmed = False
    with pytest.raises(ValueError):
        await engine.submit(snapshot)
    snapshot.confirmed = True
    await engine.submit(snapshot)
    with pytest.raises(BusyError):
        await engine.submit(snapshot)


async def test_concurrent_submission_has_one_winner(repository, snapshot, fake):
    engine = Engine(repository, {"node-red": fake})
    outcomes = await asyncio.gather(engine.submit(snapshot), engine.submit(snapshot), return_exceptions=True)
    assert sum(isinstance(item, str) for item in outcomes) == 1
    assert sum(isinstance(item, BusyError) for item in outcomes) == 1


@pytest.mark.parametrize("failure", ["fail_start", "fail_execute"])
async def test_failure_preserves_unverified_and_cleans(repository, snapshot, fake, failure):
    setattr(fake, failure, True)
    engine = Engine(repository, {"node-red": fake})
    run_id = await engine.submit(snapshot)
    await engine.run(run_id)
    stored = await repository.get(run_id)
    assert stored["status"] == "FAILED"
    assert not stored["report"]["all_passed"]
    assert any(item["status"] == "UNVERIFIED" for item in stored["results"])
    assert fake.cleaned == [run_id]


async def test_job_timeout_cleanup(repository, snapshot, fake):
    async def stall(_):
        await asyncio.sleep(10)

    fake.start = stall
    snapshot.job_timeout_seconds = 1
    engine = Engine(repository, {"node-red": fake})
    run_id = await engine.submit(snapshot)
    await engine.run(run_id)
    stored = await repository.get(run_id)
    assert stored["report"]["error"] == "job_timeout"
    assert stored["report"]["cleanup_status"] == "COMPLETE"


async def test_restart_recovery_preserves_partial_evidence(repository, snapshot, fake):
    engine = Engine(repository, {"node-red": fake})
    run_id = await engine.submit(snapshot)
    await repository.update(run_id, status="RUNNING_BASELINE")
    await engine.recover()
    stored = await repository.get(run_id)
    assert stored["status"] == "INTERRUPTED"
    assert len(stored["results"]) == 8
    assert all(item["status"] == "UNVERIFIED" for item in stored["results"])
    assert all(item["verdict"] == "UNVERIFIED" for item in stored["report"]["comparisons"])
    await engine.submit(snapshot)


async def test_non_node_red_contract_is_accepted(repository, snapshot, fake):
    snapshot.target_type = "contract-only-fake"
    snapshot.baseline.artifact = "tool-specific-baseline"
    snapshot.candidate.artifact = "tool-specific-candidate"
    engine = Engine(repository, {snapshot.target_type: fake})
    run_id = await engine.submit(snapshot)
    await engine.run(run_id)
    assert (await repository.get(run_id))["report"]["all_passed"]


async def test_recovery_releases_slot_after_crash_between_completion_and_release(repository, snapshot, fake):
    engine = Engine(repository, {"node-red": fake})
    run_id = await engine.submit(snapshot)
    await repository.update(run_id, status="COMPLETED", report={"cleanup_status": "COMPLETE"})
    await engine.recover()
    await engine.submit(snapshot)


async def test_cleanup_pending_blocks_next_job_then_retries(repository, snapshot, fake):
    fake.cleanup_pending = True
    engine = Engine(repository, {"node-red": fake})
    run_id = await engine.submit(snapshot)
    await engine.run(run_id)
    with pytest.raises(BusyError):
        await engine.submit(snapshot)
    fake.cleanup_pending = False
    await engine.recover()
    stored = await repository.get(run_id)
    assert stored["status"] == "COMPLETED"
    assert stored["report"]["cleanup_status"] == "COMPLETE"
    await engine.submit(snapshot)
