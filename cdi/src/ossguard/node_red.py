import asyncio
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path
from uuid import UUID

import docker
import httpx

from ossguard.contracts import Case, Cleanup, Environment, Event, Evidence, Snapshot, Target

ALLOWED_IMAGES = {"nodered/node-red:4.0.9", "nodered/node-red:4.1.7"}
MOCK_IMAGE = "ossguard-mock:1"


class NodeRedAdapter:
    version = "1.1"

    def __init__(self, root: Path, client=None):
        self.root = root.resolve()
        self.client = client

    def docker(self):
        if self.client is None:
            self.client = docker.from_env(timeout=15)
        return self.client

    async def blocking(self, function, *args):
        # Cancellation must not let a background Docker creation race with cleanup.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception:
                pass
            raise

    def labels(self, run_id, env_id=None):
        labels = {"ossguard.owner": "execution-v1", "ossguard.run": str(UUID(str(run_id)))}
        if env_id:
            labels["ossguard.env"] = env_id
        return labels

    async def prepare(self, run_id: UUID, env_id: str, target: Target, snapshot: Snapshot):
        if target.image not in ALLOWED_IMAGES:
            raise ValueError("Unsupported image; only team-approved versions may execute")
        if target.artifact not in {"normal", "missing_shipping", "fixed"}:
            raise ValueError("Unsupported artifact; only team-prepared samples may execute")
        if env_id not in {"baseline", "candidate"}:
            raise ValueError("Unknown environment")
        artifact = self.root / "fixtures/node-red" / f"{target.artifact}.json"
        content = artifact.read_bytes()
        # Validate input before starting any containers. Demo domain rules stay in this adapter.
        for case in snapshot.cases:
            if (
                type(case.input.get("quantity")) is not int
                or case.input["quantity"] <= 0
                or type(case.input.get("stock")) is not int
                or case.input["stock"] < 0
                or type(case.input.get("paid")) is not bool
            ):
                raise ValueError(
                    "Demo requires positive integer quantity, nonnegative stock and boolean paid"
                )
        image_lock = json.loads((self.root / "fixtures/node-red/images.lock.json").read_text())
        image_reference = image_lock[target.image]
        image = await self.blocking(lambda: self.docker().images.get(image_reference))
        mock = await self.blocking(lambda: self.docker().images.get(MOCK_IMAGE))
        return Environment(
            run_id=str(run_id),
            env_id=env_id,
            image=image.id,
            artifact_hash=hashlib.sha256(content).hexdigest(),
            adapter_version=self.version,
            metadata={
                "artifact": target.artifact,
                "flow_json": json.loads(content),
                "artifact_text": content.decode("utf-8"),
                "settings": (self.root / "fixtures/node-red/settings.js").read_text(),
                "image_tag": target.image,
                "image_reference": image_reference,
                "repo_digests": image.attrs.get("RepoDigests", []),
                "mock_image_id": mock.id,
                "step_map": snapshot.step_map,
                "request_timeout": snapshot.request_timeout_seconds,
            },
        )

    async def start(self, environment: Environment):
        await self.blocking(self._start_containers, environment)
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            for url in (environment.metadata["mock_url"], environment.metadata["target_url"]):
                while True:
                    try:
                        response = await client.get(url + "/health")
                        if response.status_code == 200 and response.json().get("ready"):
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Environment readiness timed out")
                    await asyncio.sleep(0.25)

    def _start_containers(self, environment):
        client = self.docker()
        labels = self.labels(environment.run_id, environment.env_id)
        suffix = UUID(environment.run_id).hex[:12] + ("b" if environment.env_id == "baseline" else "c")
        network = client.networks.create(
            f"ossguard-{suffix}",
            driver="bridge",
            internal=True,
            labels=labels,
            options={"com.docker.network.bridge.name": f"og{suffix}"},
        )
        volume = client.volumes.create(name=f"ossguard-data-{suffix}", labels=labels)
        control = client.networks.create(f"ossguard-control-{suffix}", driver="bridge", labels=labels)
        common = dict(
            labels=labels,
            detach=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=128,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            log_config=docker.types.LogConfig(type="json-file", config={"max-size": "5m", "max-file": "2"}),
        )
        mock = client.containers.create(
            environment.metadata["mock_image_id"],
            name=f"ossguard-mock-{suffix}",
            network=network.name,
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            **common,
        )
        network.disconnect(mock)
        network.connect(mock, aliases=["mock"])
        target = client.containers.create(
            environment.image,
            name=f"ossguard-target-{suffix}",
            network=network.name,
            volumes={volume.name: {"bind": "/data", "mode": "rw"}},
            **common,
        )
        network.disconnect(target)
        network.connect(target, aliases=["target"])
        relay = client.containers.create(
            environment.metadata["mock_image_id"],
            command=["python", "-u", "/app/relay_service.py"],
            name=f"ossguard-relay-{suffix}",
            network=control.name,
            ports={"8080/tcp": ("127.0.0.1", None)},
            read_only=True,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=16m"},
            **common,
        )
        network.connect(relay)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, content in [
                ("flows.json", json.dumps(environment.metadata["flow_json"]).encode()),
                ("settings.js", environment.metadata["settings"].encode()),
            ]:
                info = tarfile.TarInfo(name)
                info.size, info.uid, info.gid, info.mode = len(content), 1000, 1000, 0o644
                tar.addfile(info, io.BytesIO(content))
        target.put_archive("/data", archive.getvalue())
        mock.start()
        target.start()
        relay.start()
        mock.reload()
        target.reload()
        relay.reload()
        gateway = self._url(relay, "8080/tcp")
        environment.metadata.update(
            mock_id=mock.id,
            target_id=target.id,
            relay_id=relay.id,
            mock_url=gateway + "/mock",
            target_url=gateway + "/target",
        )

    @staticmethod
    def _url(container, port):
        bindings = container.attrs["NetworkSettings"]["Ports"].get(port)
        if not bindings:
            raise RuntimeError("Gateway host port was not published")
        binding = bindings[0]
        return f"http://127.0.0.1:{binding['HostPort']}"

    async def execute(self, environment: Environment, case: Case):
        evidence = Evidence(
            run_id=environment.run_id,
            env_id=environment.env_id,
            case_id=case.id,
            step_map=environment.metadata["step_map"],
        )
        identifiers = {"run_id": environment.run_id, "env_id": environment.env_id, "case_id": case.id}
        headers = {f"X-{key.replace('_', '-')}": value for key, value in identifiers.items()}
        async with httpx.AsyncClient(
            timeout=environment.metadata["request_timeout"], trust_env=False
        ) as client:
            try:
                reset = await client.post(
                    environment.metadata["mock_url"] + "/reset", json={**identifiers, **case.input}
                )
                reset.raise_for_status()
                response = await client.post(
                    environment.metadata["target_url"] + "/orders", json=case.input, headers=headers
                )
                if response.headers.get("X-OSSGuard-Transport-Error") == "1":
                    raise httpx.RequestError("Gateway could not reach target")
                evidence.response = {"status_code": response.status_code, "body": response.json()}
                evidence.status = "COMPLETE"
            except (httpx.HTTPError, ValueError) as error:
                evidence.status, evidence.error = "ERROR", type(error).__name__
        return evidence

    async def collect(self, environment: Environment, evidence: Evidence):
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            try:
                response = await client.get(environment.metadata["mock_url"] + "/state")
                response.raise_for_status()
                state = response.json()
                if any(state.get(key) != getattr(evidence, key) for key in ("run_id", "env_id", "case_id")):
                    raise ValueError("Evidence correlation mismatch")
                evidence.response["mock"] = {
                    "stock": state["stock"],
                    "shipping_count": state["shipping_count"],
                }
                evidence.events = [
                    Event(step_key=evidence.step_map.get(event["node_id"], "unmapped"), **event)
                    for event in state["events"]
                ]
                evidence.logs.extend(await self.blocking(self._logs, environment.run_id, environment.env_id))
            except Exception as error:
                evidence.status = "ERROR"
                evidence.error = f"collection:{type(error).__name__}"
        return evidence

    def _logs(self, run_id, env_id=None):
        filters = {"label": [f"{key}={value}" for key, value in self.labels(run_id, env_id).items()]}
        logs = []
        for container in self.docker().containers.list(all=True, filters=filters):
            logs.append(f"[{container.name}]\n" + container.logs(tail=200).decode(errors="replace"))
        return logs

    async def capture_logs(self, run_id: UUID):
        return await self.blocking(self._logs, run_id)

    async def cleanup(self, run_id: UUID):
        return await self.blocking(self._cleanup, run_id)

    def _cleanup(self, run_id):
        filters = {"label": [f"{key}={value}" for key, value in self.labels(run_id).items()]}
        errors, remaining = [], []
        try:
            client = self.docker()
            for kind, group in (
                ("container", client.containers),
                ("volume", client.volumes),
                ("network", client.networks),
            ):
                try:
                    resources = (
                        group.list(all=True, filters=filters)
                        if kind == "container"
                        else group.list(filters=filters)
                    )
                    for resource in resources:
                        try:
                            if kind == "container":
                                resource.remove(force=True, v=True)
                            else:
                                resource.remove()
                        except docker.errors.NotFound:
                            pass
                        except Exception as error:
                            errors.append(type(error).__name__)
                            remaining.append(resource.id)
                except Exception as error:
                    errors.append(type(error).__name__)
                    remaining.append("resource-list-unavailable")
        except Exception as error:
            errors.append(type(error).__name__)
            remaining.append("docker-unavailable")
        return Cleanup(status="PENDING" if errors else "COMPLETE", remaining=remaining, errors=errors)
