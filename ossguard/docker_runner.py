import asyncio
import json
import shutil
from pathlib import Path

import httpx

from ossguard.commands import command
from ossguard.config import Settings
from ossguard.errors import GuardError
from ossguard.repository import DOCKERFILE


class DockerRunner:
    def __init__(self, settings: Settings, run_id: str, work: Path):
        self.settings = settings
        self.run_id = run_id
        self.work = work
        self.label = f"ossguard.run={run_id}"
        self.network = f"ossguard-{run_id}"
        self.control_network = f"ossguard-{run_id}-control"
        self.relays: dict[str, str] = {}
        self.resources: list[str] = []

    async def create_network(self):
        await command(
            ["docker", "network", "create", "--internal", "--label", self.label, self.network]
        )
        await command(["docker", "network", "create", "--label", self.label, self.control_network])

    async def start_relay(self, environment: str, app_port: int):
        name = f"ossguard-{self.run_id}-{environment}-relay"
        code = Path(__file__).with_name("relay.py").read_text()
        await command(
            [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                self.label,
                "--network",
                self.control_network,
                "--publish",
                "127.0.0.1::8080",
                "--env",
                f"APP_HOST=app-{environment}",
                "--env",
                f"APP_PORT={app_port}",
                "--env",
                f"MOCK_HOST=mock-{environment}",
                "--user",
                "65534:65534",
                "--read-only",
                "--memory",
                "64m",
                "--cpus",
                "0.5",
                "--pids-limit",
                "64",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                self.settings.python_image,
                "python",
                "-B",
                "-u",
                "-c",
                code,
            ]
        )
        await command(["docker", "network", "connect", self.network, name])
        await command(["docker", "start", name])
        self.resources.append(name)
        self.relays[environment] = await self.inspect_port(name, 8080)

    async def build(self, source: Path, environment: str):
        context = self.work / f"context-{environment}"
        shutil.copytree(
            source,
            context,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", ".env", ".env.*", "__pycache__", ".cache"
            ),
        )
        # The platform-controlled context excludes host secrets and repository ignore rules.
        (context / ".dockerignore").write_text(".git\n.venv\n.env\n.env.*\n__pycache__\n")
        (context / "Dockerfile.ossguard").write_text(DOCKERFILE.read_text())
        tag = f"ossguard-{self.run_id}:{environment}"
        log = await command(
            [
                "docker",
                "build",
                "--progress=plain",
                "--label",
                self.label,
                "--tag",
                tag,
                "--build-arg",
                f"PYTHON_IMAGE={self.settings.python_image}",
                "--build-arg",
                f"UV_IMAGE={self.settings.uv_image}",
                "--file",
                str(context / "Dockerfile.ossguard"),
                str(context),
            ],
            timeout=self.settings.build_timeout,
            limit=self.settings.log_bytes,
            code="BUILD_FAILED",
        )
        image = json.loads((await command(["docker", "image", "inspect", tag]))["text"])[0]
        return tag, image["Id"], log

    async def inspect_port(self, name: str, port: int):
        data = json.loads((await command(["docker", "inspect", name]))["text"])[0]
        try:
            bindings = data["NetworkSettings"]["Ports"][f"{port}/tcp"]
            binding = next(value for value in bindings if value["HostIp"] == "127.0.0.1")
            return f"http://127.0.0.1:{int(binding['HostPort'])}"
        except (KeyError, TypeError, StopIteration) as exc:
            raise GuardError("PORT_UNAVAILABLE", "Container has no loopback test port") from exc

    async def ready(self, url: str, client: httpx.AsyncClient):
        deadline = asyncio.get_running_loop().time() + self.settings.startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(url, timeout=2)
                if 200 <= response.status_code < 300:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        raise GuardError("STARTUP_TIMEOUT", "Container did not become ready")

    async def start_mock(
        self, environment: str, client: httpx.AsyncClient, stubs: list[dict], app_port: int = 8000
    ):
        name = f"ossguard-{self.run_id}-{environment}-mock"
        await command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--label",
                self.label,
                "--network",
                self.network,
                "--network-alias",
                f"mock-{environment}",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "256",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                self.settings.wiremock_image,
                "--max-request-journal-entries",
                "1000",
            ],
            timeout=self.settings.build_timeout,
            code="MOCK_START_FAILED",
        )
        self.resources.append(name)
        await self.start_relay(environment, app_port)
        url = self.relays[environment] + "/mock"
        await self.ready(url + "/__admin/mappings", client)
        for stub in stubs:
            response = await client.post(
                url + "/__admin/mappings",
                json={
                    "request": {"method": stub["method"], "urlPath": stub["path"]},
                    "response": {
                        "status": stub["status"],
                        "jsonBody": stub["body"],
                        "headers": {"Content-Type": "application/json"},
                    },
                },
            )
            if response.status_code != 201:
                raise GuardError("OBSERVATION_ERROR", "Cannot install mock response stub")
        return url

    async def start_app(
        self, image: str, environment: str, profile: dict, values: dict, client: httpx.AsyncClient
    ):
        name = f"ossguard-{self.run_id}-{environment}-app"
        env_path = self.work / f"env-{environment}"
        env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        env_path.chmod(0o600)
        try:
            await command(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--label",
                    self.label,
                    "--network",
                    self.network,
                    "--network-alias",
                    f"app-{environment}",
                    "--env-file",
                    str(env_path),
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "128",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=32m",
                    image,
                ],
                timeout=60,
                code="APP_START_FAILED",
            )
        finally:
            env_path.unlink(missing_ok=True)
        self.resources.append(name)
        url = self.relays[environment] + "/app"
        await self.ready(url + profile["health_path"], client)
        inventory = await command(
            [
                "docker",
                "exec",
                name,
                self.settings.target_python,
                "-c",
                "import importlib.metadata as m,json; "
                "print(json.dumps({d.metadata['Name'].lower().replace('_','-'): "
                "d.version for d in m.distributions()}))",
            ],
            limit=131072,
        )
        return url, json.loads(inventory["text"])

    async def logs(self):
        result = {}
        for name in self.resources:
            try:
                result[name] = await command(
                    ["docker", "logs", "--tail", "200", name], limit=self.settings.log_bytes
                )
            except GuardError as exc:
                result[name] = {"error": exc.code}
        return result

    async def cleanup(self):
        errors = []
        # Label queries also find a container created immediately before a runner crash.
        for resource, query in (
            ("container", ["docker", "ps", "-aq", "--filter", f"label={self.label}"]),
            ("image", ["docker", "images", "-q", "--filter", f"label={self.label}"]),
            ("network", ["docker", "network", "ls", "-q", "--filter", f"label={self.label}"]),
        ):
            try:
                ids = list(dict.fromkeys((await command(query))["text"].split()))
                for resource_id in ids:
                    remove = ["docker", resource, "rm"]
                    if resource in {"container", "image"}:
                        remove.append("--force")
                    await command([*remove, resource_id], timeout=30, code="CLEANUP_FAILED")
            except (GuardError, TimeoutError, OSError) as exc:
                errors.append(f"{resource}: {type(exc).__name__}")
        return {"status": "FAILED" if errors else "SUCCEEDED", "errors": errors}
