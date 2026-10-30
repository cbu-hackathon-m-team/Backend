import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from ossguard.contracts import Snapshot
from ossguard.engine import Engine
from ossguard.node_red import NodeRedAdapter
from ossguard.storage import Repository


def summarize(result):
    report = result["report"]
    lines = [
        f"Run ID: {result['id']}",
        f"Status: {result['status']}",
        f"All passed: {report.get('all_passed', False)}",
        f"Cleanup: {report.get('cleanup_status', 'NOT_STARTED')}",
    ]
    for item in report.get("comparisons", []):
        lines.append(f"  {item['case_id']}: {item['verdict']}")
    if report.get("error"):
        lines.append(f"Error: {report['error']}")
    for item in result.get("results", []):
        if item.get("error"):
            lines.append(f"  {item['env_id']}/{item['case_id']}: {item['error']}")
    return "\n".join(lines)


async def execute(args):
    repository = Repository(os.environ["DATABASE_URL"])
    adapter = NodeRedAdapter(Path.cwd())
    engine = Engine(repository, {"node-red": adapter})
    try:
        if args.command == "show":
            result = await repository.get(str(UUID(args.run_id)))
            if result is None:
                raise ValueError("Validation not found")
        else:
            await repository.own_worker()
            if args.command == "recover":
                await engine.recover()
                result = {"pending": await repository.unfinished()}
            else:
                snapshot = Snapshot.model_validate_json(args.suite.read_text(encoding="utf-8"))
                if args.candidate:
                    snapshot.candidate.artifact = args.candidate
                run_id = await engine.submit(snapshot)
                await engine.run(run_id)
                result = await repository.get(run_id)
        if args.json or args.command == "recover":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(summarize(result))
        if args.command == "run" and not result["report"].get("all_passed"):
            return 1
        return 0
    finally:
        await repository.close()


def main():
    parser = argparse.ArgumentParser(description="OSSGuard Node-RED execution backend")
    parser.add_argument("--json", action="store_true", help="Print complete evidence instead of a summary")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--suite", type=Path, default=Path("fixtures/node-red/suite.json"))
    run.add_argument("--candidate", choices=["normal", "missing_shipping", "fixed"])
    show = commands.add_parser("show")
    show.add_argument("run_id")
    commands.add_parser("recover")
    raise SystemExit(asyncio.run(execute(parser.parse_args())))


if __name__ == "__main__":
    main()
