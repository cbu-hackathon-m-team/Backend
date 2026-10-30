"""Subprocess-only fault injection. Not imported by the production API."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from ossguard.contracts import Cleanup, Snapshot
from ossguard.engine import Engine
from ossguard.node_red import NodeRedAdapter
from ossguard.storage import BusyError, Repository

ROOT = Path(__file__).resolve().parents[1]


class FaultAdapter(NodeRedAdapter):
    def __init__(self, mode, directory):
        super().__init__(ROOT)
        self.mode, self.directory = mode, directory

    async def prepare(self, run_id, env_id, target, snapshot):
        environment = await super().prepare(run_id, env_id, target, snapshot)
        if self.mode == "timeout":
            for node in environment.metadata["flow_json"]:
                if node["id"] == "finish":
                    node["func"] = node["func"].replace(
                        "return msg;", "setTimeout(() => { node.send(msg); node.done(); }, 5000); return;"
                    )
            content = json.dumps(environment.metadata["flow_json"], indent=2)
            environment.metadata.update(artifact_text=content, fault_injection="5 second response delay")
            environment.artifact_hash = hashlib.sha256(content.encode()).hexdigest()
        return environment

    async def start(self, environment):
        await super().start(environment)
        if self.mode == "preparation":
            raise RuntimeError("Injected failure after real containers became ready")

    async def execute(self, environment, case):
        if self.mode == "crash" and environment.env_id == "baseline" and case.id == "T02":
            # T01 has already been committed by Engine.run. Keep real runtimes alive
            # while the parent kills this exact subprocess without executing finally.
            (self.directory / "ready-to-kill").write_text(environment.run_id, encoding="utf-8")
            await asyncio.Event().wait()
        return await super().execute(environment, case)

    async def cleanup(self, run_id):
        if self.mode == "cleanup":
            return Cleanup(status="PENDING", errors=["injected_cleanup_failure"])
        return await super().cleanup(run_id)


async def main(args):
    repo = Repository(os.environ["DATABASE_URL"])
    adapter = FaultAdapter(args.mode, args.directory)
    engine = Engine(repo, {"node-red": adapter})
    await repo.own_worker()
    try:
        if args.mode == "recover":
            await engine.recover()
        else:
            snapshot = Snapshot.model_validate_json((ROOT / "fixtures/node-red/suite.json").read_text())
            snapshot.candidate.artifact = "normal"
            snapshot.rules += f" [isolated failure test: {args.mode}]"
            if args.mode == "timeout":
                snapshot.request_timeout_seconds = 1
            run_id = await engine.submit(snapshot)
            (args.directory / "run-id.txt").write_text(run_id, encoding="utf-8")
            await engine.run(run_id)
            if args.mode == "cleanup":
                try:
                    await engine.submit(snapshot)
                except BusyError:
                    (args.directory / "duplicate-blocked").write_text("yes", encoding="utf-8")
                else:
                    raise AssertionError("Cleanup-pending validation did not block another submission")
    finally:
        await repo.close()
        if adapter.client is not None:
            adapter.client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["preparation", "timeout", "crash", "cleanup", "recover", "normal"])
    parser.add_argument("directory", type=Path)
    asyncio.run(main(parser.parse_args()))
