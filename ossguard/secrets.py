import asyncio

from ossguard.config import Settings
from ossguard.errors import GuardError


async def resolve_environment(settings: Settings, snapshot: dict):
    profile = snapshot["execution_profile"]
    environment = snapshot["environment_profile"]
    values = dict(environment.get("values", {}))
    secrets = []
    refs = environment.get("secret_refs", {})
    if refs:
        if not settings.secret_project_id:
            raise GuardError(
                "SECRET_CONFIG_MISSING", "Configure a dedicated target Secret Manager project"
            )
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        try:
            for name, ref in refs.items():
                if not ref.startswith(f"projects/{settings.secret_project_id}/secrets/"):
                    raise GuardError(
                        "SECRET_PROJECT_DENIED", "Target secret reference uses the wrong project"
                    )
                response = await asyncio.to_thread(
                    client.access_secret_version, request={"name": ref}, timeout=10
                )
                value = response.payload.data.decode()
                if not value or len(value) > 4096 or any(c in value for c in "\r\n\0"):
                    raise GuardError(
                        "INVALID_SECRET", "Target secret must be a bounded single-line value"
                    )
                values[name] = value
                secrets.append(value)
        finally:
            client.transport.close()
    for name, definition in profile["env_schema"].items():
        if definition["source"] == "runner_mock":
            continue
        if definition["required"] and name not in values:
            raise GuardError("MISSING_ENVIRONMENT", f"Missing required variable: {name}")
    return values, secrets
