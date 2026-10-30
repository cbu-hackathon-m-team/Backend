import asyncio
import difflib
from uuid import UUID

from ossguard.comparison import VERSION, compare
from ossguard.contracts import Adapter, Cleanup, Evidence, Snapshot
from ossguard.storage import Repository


class Engine:
    def __init__(self, repository: Repository, adapters: dict[str, Adapter]):
        self.repository = repository
        self.adapters = adapters

    async def submit(self, snapshot: Snapshot):
        if not snapshot.confirmed:
            raise ValueError("Tests must be confirmed before execution")
        if snapshot.target_type not in self.adapters:
            raise ValueError("Unsupported target type")
        return await self.repository.create(snapshot.model_dump(mode="json"))

    async def run(self, run_id: str):
        stored = await self.repository.get(run_id)
        snapshot = Snapshot.model_validate(stored["snapshot"])
        adapter = self.adapters[snapshot.target_type]
        report = {
            "analysis_status": "NOT_CONFIGURED",
            "cleanup_status": "NOT_STARTED",
            "comparison_version": VERSION,
            "adapter_version": adapter.version,
            "environments": {},
            "logs": [],
            "error": None,
        }
        evidence = {
            (env, case.id): Evidence(run_id=run_id, env_id=env, case_id=case.id, step_map=snapshot.step_map)
            for env in ("baseline", "candidate")
            for case in snapshot.cases
        }
        final_status = "COMPLETED"
        try:
            async with asyncio.timeout(snapshot.job_timeout_seconds):
                await self.repository.update(run_id, status="PREPARING", report=report)
                environments = {}
                for env_id in ("baseline", "candidate"):
                    environment = await adapter.prepare(
                        UUID(run_id), env_id, getattr(snapshot, env_id), snapshot
                    )
                    environments[env_id] = environment
                    report["environments"][env_id] = environment.model_dump(mode="json")
                    await self.repository.update(run_id, report=report)
                    await adapter.start(environment)
                for env_id, environment in environments.items():
                    await self.repository.update(run_id, status=f"RUNNING_{env_id.upper()}")
                    for case in snapshot.cases:
                        try:
                            result = await adapter.execute(environment, case)
                            result = await adapter.collect(environment, result)
                        except Exception as error:
                            result = Evidence(
                                run_id=run_id,
                                env_id=env_id,
                                case_id=case.id,
                                status="ERROR",
                                error=type(error).__name__,
                                step_map=snapshot.step_map,
                            )
                        if (result.run_id, result.env_id, result.case_id) != (run_id, env_id, case.id):
                            raise ValueError("Adapter returned evidence for a different execution")
                        evidence[(env_id, case.id)] = result
                        await self.repository.save_evidence(result)
                        # An unresolved timed-out request could mutate a subsequent reset.
                        # Stop this environment after execution/collection errors.
                        if result.status != "COMPLETE":
                            final_status = "FAILED"
                            break
        except asyncio.CancelledError:
            final_status, report["error"] = "INTERRUPTED", "execution_cancelled"
        except TimeoutError:
            final_status, report["error"] = "FAILED", "job_timeout"
        except Exception as error:
            final_status, report["error"] = "FAILED", type(error).__name__
        finally:
            # Save evidence before destroying the runtime. A DB error intentionally leaves
            # resources labeled for restart recovery instead of discarding unsaved evidence.
            for result in evidence.values():
                await self.repository.save_evidence(result)
            await self.repository.update(run_id, status="COMPARING")
            report["comparisons"] = [
                compare(
                    case,
                    evidence[("baseline", case.id)],
                    evidence[("candidate", case.id)],
                    snapshot.ignore_paths,
                )
                for case in snapshot.cases
            ]
            report["all_passed"] = all(item["verdict"] == "PASS" for item in report["comparisons"])
            flows = report["environments"]
            if all(env in flows for env in ("baseline", "candidate")):
                report["artifact_diff"] = "\n".join(
                    difflib.unified_diff(
                        flows["baseline"]["metadata"].get("artifact_text", "").splitlines(),
                        flows["candidate"]["metadata"].get("artifact_text", "").splitlines(),
                        fromfile="baseline",
                        tofile="candidate",
                        lineterm="",
                    )
                )
            try:
                if hasattr(adapter, "capture_logs"):
                    report["logs"] = await adapter.capture_logs(UUID(run_id))
            except Exception as error:
                report["log_collection_error"] = type(error).__name__
            report["cleanup_status"] = "PENDING"
            await self.repository.update(run_id, status="CLEANING", report=report)
            try:
                cleanup = await adapter.cleanup(UUID(run_id))
            except Exception as error:
                cleanup = Cleanup(status="PENDING", errors=[type(error).__name__])
            report["cleanup_status"] = cleanup.status
            report["cleanup"] = cleanup.model_dump()
            await self.repository.update(run_id, status=final_status, report=report)
            if cleanup.status == "COMPLETE":
                await self.repository.release(run_id)

    async def recover(self):
        for run_id in await self.repository.unfinished():
            stored = await self.repository.get(run_id)
            report = stored["report"]
            snapshot = Snapshot.model_validate(stored["snapshot"])
            adapter = self.adapters.get(snapshot.target_type)
            if adapter is None:
                report.update(cleanup_status="PENDING", error="adapter_unavailable")
                await self.repository.update(run_id, status="INTERRUPTED", report=report)
                continue
            terminal = stored["status"] in {"COMPLETED", "FAILED", "INTERRUPTED"}
            if not terminal:
                report["error"] = "server_restart"
                report["all_passed"] = False
                present = {(item["env_id"], item["case_id"]) for item in stored["results"]}
                for env in ("baseline", "candidate"):
                    for case in snapshot.cases:
                        if (env, case.id) not in present:
                            await self.repository.save_evidence(
                                Evidence(
                                    run_id=run_id, env_id=env, case_id=case.id, step_map=snapshot.step_map
                                )
                            )
                recovered = await self.repository.get(run_id)
                evidence = {
                    (item["env_id"], item["case_id"]): Evidence.model_validate(item)
                    for item in recovered["results"]
                }
                report["comparisons"] = [
                    compare(
                        case,
                        evidence[("baseline", case.id)],
                        evidence[("candidate", case.id)],
                        snapshot.ignore_paths,
                    )
                    for case in snapshot.cases
                ]
            try:
                if hasattr(adapter, "capture_logs"):
                    report["recovery_logs"] = await adapter.capture_logs(UUID(run_id))
            except Exception as error:
                report["log_collection_error"] = type(error).__name__
            await self.repository.update(
                run_id, status=stored["status"] if terminal else "INTERRUPTED", report=report
            )
            try:
                cleanup = await adapter.cleanup(UUID(run_id))
            except Exception as error:
                cleanup = Cleanup(status="PENDING", errors=[type(error).__name__])
            report.update(cleanup_status=cleanup.status, cleanup=cleanup.model_dump())
            await self.repository.update(run_id, report=report)
            if cleanup.status == "COMPLETE":
                await self.repository.release(run_id)
