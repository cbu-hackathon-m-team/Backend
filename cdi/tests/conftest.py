import json
from pathlib import Path

import pytest

from ossguard.contracts import Cleanup, Environment, Event, Evidence, Snapshot
from ossguard.storage import Repository

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def snapshot():
    return Snapshot.model_validate(json.loads((ROOT / "fixtures/node-red/suite.json").read_text()))


@pytest.fixture
async def repository(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await repo.initialize_for_tests()
    yield repo
    await repo.close()


class FakeAdapter:
    """Contract test double, not a second supported runtime."""

    version = "fake-contract-v1"

    def __init__(self):
        self.cleaned = []
        self.cleanup_pending = False
        self.fail_start = False
        self.fail_execute = False

    async def prepare(self, run_id, env_id, target, snapshot):
        return Environment(
            run_id=str(run_id),
            env_id=env_id,
            image="fake-image-id",
            artifact_hash=target.artifact,
            adapter_version=self.version,
            metadata={"artifact": target.artifact, "step_map": snapshot.step_map},
        )

    async def start(self, environment):
        if self.fail_start:
            raise RuntimeError("injected preparation failure")

    async def execute(self, environment, case):
        if self.fail_execute:
            raise TimeoutError("injected request timeout")
        remaining, ship = {"T01": (8, 1), "T02": (0, 1), "T03": (10, 0), "T04": (1, 0)}[case.id]
        event_specs = [("received", "order_received"), ("payment", "payment_checked")]
        if ship:
            event_specs.append(("inventory", "inventory_decremented"))
            if environment.metadata["artifact"] != "missing_shipping":
                event_specs.append(("shipping", "shipping_requested"))
        elif case.id == "T04":
            event_specs.append(("inventory", "inventory_rejected"))
        return Evidence(
            run_id=environment.run_id,
            env_id=environment.env_id,
            case_id=case.id,
            status="COMPLETE",
            response={"status_code": 200, "mock": {"stock": remaining}},
            events=[
                Event(step_key=step, event_type=kind, sequence=i + 1)
                for i, (step, kind) in enumerate(event_specs)
            ],
            step_map=environment.metadata["step_map"],
        )

    async def collect(self, environment, evidence):
        evidence.logs = ["fake-contract-evidence"]
        return evidence

    async def cleanup(self, run_id):
        self.cleaned.append(str(run_id))
        return Cleanup(status="PENDING" if self.cleanup_pending else "COMPLETE")


@pytest.fixture
def fake():
    return FakeAdapter()
