import base64
import hashlib
import json
import tomllib
from pathlib import Path
from urllib.parse import quote

import httpx
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from ossguard.config import Settings
from ossguard.errors import GuardError
from ossguard.schemas import ExecutionProfile, MockStub, SuiteUpdate, repository

DOCKERFILE = Path(__file__).resolve().parent.parent / "templates" / "Dockerfile.ossguard"
REQUIRED = ["pyproject.toml", "uv.lock", "ossguard.json", "Dockerfile.ossguard", "app.py"]


def allowed_repository(url: str, settings: Settings):
    normalized = repository(url)
    allowed = {repository(value).lower() for value in settings.allowed_repos}
    if normalized.lower() not in allowed:
        raise GuardError(
            "REPOSITORY_NOT_ALLOWED", "Add the reviewed repository to the allowlist", 403
        )
    return normalized


def parse_project(manifest: str, lock: str) -> list[dict]:
    try:
        project = tomllib.loads(manifest)
        locked = tomllib.loads(lock)
        metadata = project["project"]
        if "dependencies" in metadata.get("dynamic", []):
            raise ValueError("dynamic dependencies are not supported")
        if not SpecifierSet(metadata.get("requires-python", "")).contains("3.12.14"):
            raise ValueError("project must support Python 3.12.14")
        uv_config = project.get("tool", {}).get("uv", {})
        if any(
            uv_config.get(key)
            for key in (
                "workspace",
                "sources",
                "index",
                "constraint-dependencies",
                "override-dependencies",
            )
        ):
            raise ValueError("custom indexes, sources, workspaces and overrides are not supported")
        project_name = canonicalize_name(metadata["name"])
        versions = {}
        for package in locked["package"]:
            source = package.get("source", {})
            if source.get("registry") != "https://pypi.org/simple":
                if canonicalize_name(package["name"]) != project_name or source not in (
                    {"virtual": "."},
                    {"editable": "."},
                ):
                    raise ValueError("only PyPI packages and the root project are supported")
                continue
            name = canonicalize_name(package["name"])
            versions.setdefault(name, set()).add(package["version"])
        dependencies = []
        for declaration in metadata.get("dependencies", []):
            req = Requirement(declaration)
            if req.url:
                raise ValueError("Git/path/URL dependencies are not supported")
            if req.marker and not req.marker.evaluate(
                {"python_version": "3.12", "sys_platform": "linux"}
            ):
                continue
            name = canonicalize_name(req.name)
            found = sorted(versions.get(name, []))
            if len(found) != 1:
                raise ValueError(f"{name}: exactly one locked version required for MVP")
            dependencies.append(
                {"name": name, "declaration": declaration, "locked_version": found[0]}
            )
        if not dependencies:
            raise ValueError("at least one direct runtime PyPI dependency required")
        return dependencies
    except (KeyError, TypeError, ValueError, InvalidRequirement) as exc:
        raise GuardError("UNSUPPORTED_PROJECT", str(exc)) from exc


def validate_dockerfile(content: str):
    if content.replace("\r\n", "\n").strip() != DOCKERFILE.read_text().strip():
        raise GuardError("UNSUPPORTED_DOCKERFILE", "Use the provided Dockerfile.ossguard template")


async def bounded_json(client: httpx.AsyncClient, url: str, **kwargs):
    try:
        async with client.stream("GET", url, **kwargs) as response:
            if response.status_code >= 400:
                raise GuardError("UPSTREAM_ERROR", f"Upstream HTTP {response.status_code}", 502)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise GuardError("UPSTREAM_TOO_LARGE", "Upstream response exceeds limit", 502)
            return json.loads(data)
    except (httpx.HTTPError, ValueError) as exc:
        raise GuardError("UPSTREAM_ERROR", "Upstream unavailable or invalid JSON", 502) from exc


class RepositoryService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def inspect(self, url: str, ref: str, cached: dict | None = None):
        url = allowed_repository(url, self.settings)
        owner_repo = url.removeprefix("https://github.com/")
        base = f"https://api.github.com/repos/{owner_repo}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token := self.settings.github_token.get_secret_value():
            headers["Authorization"] = f"Bearer {token}"
        commit = await bounded_json(
            self.client, f"{base}/commits/{quote(ref, safe='')}", headers=headers
        )
        sha = commit.get("sha", "")
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise GuardError("INVALID_COMMIT", "GitHub returned an invalid commit SHA", 502)
        if cached and cached.get("sha") == sha:
            return cached

        async def read(path):
            data = await bounded_json(
                self.client,
                f"{base}/contents/{quote(path, safe='/')}",
                params={"ref": sha},
                headers=headers,
            )
            if (
                not isinstance(data, dict)
                or data.get("type") != "file"
                or data.get("size", 2_000_000) > 1_000_000
            ):
                raise GuardError("INVALID_SOURCE_FILE", f"Bounded regular file required: {path}")
            try:
                return base64.b64decode(data["content"], validate=False).decode("utf-8")
            except (KeyError, ValueError, UnicodeError) as exc:
                raise GuardError("INVALID_SOURCE_FILE", f"Cannot decode {path}") from exc

        contents = {name: await read(name) for name in REQUIRED}
        validate_dockerfile(contents["Dockerfile.ossguard"])
        try:
            profile = ExecutionProfile.model_validate_json(contents["ossguard.json"])
            tests = json.loads(await read(profile.test_cases))
            # One bounded JSON file avoids unbounded recursive GitHub directory traversal.
            stubs = json.loads(await read(f"{profile.mock_stubs}/stubs.json"))
            suite = SuiteUpdate(cases=tests, stubs=[MockStub.model_validate(s) for s in stubs])
        except ValueError as exc:
            raise GuardError("INVALID_PROFILE", str(exc)) from exc
        return {
            "sha": sha,
            "dependencies": parse_project(contents["pyproject.toml"], contents["uv.lock"]),
            "profile": profile.model_dump(),
            "suite": suite.model_dump(),
            "file_hashes": {
                name: hashlib.sha256(value.encode()).hexdigest() for name, value in contents.items()
            },
        }

    async def versions(self, name: str):
        data = await bounded_json(self.client, f"https://pypi.org/pypi/{quote(name, safe='')}/json")
        found = []
        for version, files in data.get("releases", {}).items():
            try:
                parsed = Version(version)
            except InvalidVersion:
                continue
            if files and any(not f.get("yanked") for f in files):
                found.append((parsed, version))
        return [value for _, value in sorted(found, reverse=True)]

    async def check_version(self, name: str, version: str):
        data = await bounded_json(
            self.client,
            f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json",
        )
        if not data.get("urls") or not any(not f.get("yanked") for f in data["urls"]):
            raise GuardError(
                "VERSION_UNAVAILABLE", "No non-yanked distribution for the target version"
            )
