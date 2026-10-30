import base64
import json
from pathlib import Path

import httpx
import pytest

from ossguard.config import Settings
from ossguard.errors import GuardError
from ossguard.repository import RepositoryService, parse_project

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "purchase-app"
REPO = "https://github.com/team/purchase"


async def test_github_preflight_pins_sha_and_caches_files():
    requests = []

    def github(request):
        requests.append(request)
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": "a" * 40})
        assert request.url.params["ref"] == "a" * 40
        name = request.url.path.split("/contents/", 1)[1]
        content = (SAMPLE / name).read_bytes()
        return httpx.Response(
            200,
            json={
                "type": "file",
                "size": len(content),
                "content": base64.b64encode(content).decode(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        service = RepositoryService(Settings(_env_file=None, allowed_repos=[REPO]), client)
        result = await service.inspect(REPO, "main")
        assert result["sha"] == "a" * 40
        assert result["dependencies"][0]["locked_version"] == "1.10.24"
        assert result["suite"]["approved"] is False
        count = len(requests)
        assert await service.inspect(REPO, "main", result) == result
        assert len(requests) == count + 1


async def test_preflight_upstream_failure_and_allowlist():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        service = RepositoryService(Settings(_env_file=None, allowed_repos=[REPO]), client)
        with pytest.raises(GuardError) as exc:
            await service.inspect(REPO, "main")
        assert exc.value.code == "UPSTREAM_ERROR"
        with pytest.raises(GuardError) as exc:
            await service.inspect("https://github.com/other/repo", "main")
        assert exc.value.code == "REPOSITORY_NOT_ALLOWED"


def test_no_git_path_or_custom_index_dependencies():
    lock = (SAMPLE / "uv.lock").read_text()
    original = (SAMPLE / "pyproject.toml").read_text()
    for manifest in (
        original.replace("pydantic==1.10.24", "pydantic @ git+https://github.com/org/repo.git"),
        original + '\n[[tool.uv.index]]\nurl="https://private.example/simple"\n',
    ):
        with pytest.raises(GuardError):
            parse_project(manifest, lock)


async def test_ai_invalid_json_object_is_rejected():
    from ossguard.ai import generate_suite

    settings = Settings(_env_file=None, ai_key="fake-key", ai_model="test-model")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps([])}}]}
            )
        )
    ) as client:
        with pytest.raises(GuardError) as exc:
            await generate_suite(settings, client, {}, {})
        assert exc.value.code == "AI_FAILED"
