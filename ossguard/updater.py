import asyncio
import hashlib
import shutil
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

from ossguard.commands import command
from ossguard.config import Settings
from ossguard.errors import GuardError
from ossguard.repository import allowed_repository, parse_project, validate_dockerfile


def source_hashes(root: Path):
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            raise GuardError("UNSUPPORTED_SOURCE", "Symbolic links are not supported")
        if path.is_file():
            if path.name == ".env" or path.name.startswith(".env."):
                raise GuardError(
                    "UNSUPPORTED_SOURCE", "Repository must not contain environment secrets"
                )
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class PythonUvUpdater:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def collect(self, url: str, sha: str, root: Path):
        url = allowed_repository(url, self.settings)
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=False)
        await command(["git", "init", str(root)], timeout=self.settings.source_timeout)
        await command(["git", "remote", "add", "origin", url + ".git"], cwd=root)
        await command(
            ["git", "-c", "protocol.file.allow=never", "fetch", "--depth=1", "origin", sha],
            cwd=root,
            timeout=self.settings.source_timeout,
            code="SOURCE_FAILED",
        )
        await command(
            ["git", "-c", "core.autocrlf=false", "checkout", "--detach", "FETCH_HEAD"],
            cwd=root,
            timeout=self.settings.source_timeout,
        )
        actual = (await command(["git", "rev-parse", "HEAD"], cwd=root))["text"].strip()
        if actual != sha:
            raise GuardError("SOURCE_MISMATCH", "Fetched SHA differs from the approved SHA")
        if (root / ".gitmodules").exists():
            raise GuardError("UNSUPPORTED_SOURCE", "Submodules are not supported")
        source_hashes(root)
        validate_dockerfile((root / "Dockerfile.ossguard").read_text())
        parse_project((root / "pyproject.toml").read_text(), (root / "uv.lock").read_text())

    async def update(self, original: Path, candidate: Path, package: str, version: str):
        shutil.copytree(original, candidate, ignore=shutil.ignore_patterns(".git", ".venv"))
        before = source_hashes(candidate)
        tool = await command(["uv", "--version"])
        if tool["text"].split()[1] != "0.9.26":
            raise GuardError("TOOL_VERSION_MISMATCH", "Runner requires uv 0.9.26")
        log = await command(
            [
                "uv",
                "add",
                "--no-sync",
                "--no-config",
                "--python",
                sys.executable,
                f"{package}=={version}",
            ],
            cwd=candidate,
            timeout=self.settings.dependency_timeout,
            limit=self.settings.log_bytes,
            code="DEPENDENCY_CONFLICT",
        )
        after = source_hashes(candidate)
        changed = [
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        ]
        if set(changed) - {"pyproject.toml", "uv.lock"}:
            raise GuardError("SOURCE_CHANGED", "Updater modified files other than manifest/lock")
        deps = parse_project(
            (candidate / "pyproject.toml").read_text(), (candidate / "uv.lock").read_text()
        )
        actual = next(
            (d["locked_version"] for d in deps if d["name"] == canonicalize_name(package)), None
        )
        if actual != version:
            raise GuardError(
                "VERSION_MISMATCH", "Updated lock does not contain the requested version"
            )
        import difflib

        diffs = {}
        for name in ("pyproject.toml", "uv.lock"):
            a = (original / name).read_text().splitlines(keepends=True)
            b = (candidate / name).read_text().splitlines(keepends=True)
            diffs[name] = "".join(
                difflib.unified_diff(a, b, fromfile=f"a/{name}", tofile=f"b/{name}")
            )
        return {
            "changed_files": sorted(changed),
            "source_unchanged": True,
            "file_diffs": diffs,
            "update_log": log,
        }


def package_diff(baseline: dict, candidate: dict):
    return [
        {"name": name, "before": baseline.get(name), "after": candidate.get(name)}
        for name in sorted(baseline.keys() | candidate.keys())
        if baseline.get(name) != candidate.get(name)
    ]
