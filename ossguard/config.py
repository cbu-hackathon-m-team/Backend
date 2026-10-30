from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OSSGUARD_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://ossguard:ossguard@127.0.0.1:54329/ossguard"
    )
    api_token: SecretStr = SecretStr("")
    allowed_repos: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
    github_token: SecretStr = SecretStr("")
    runner_id: str = Field(default="runner-1", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    work_dir: Path = Path(".work")
    poll_seconds: float = 2
    heartbeat_seconds: float = 5
    source_timeout: float = 60
    dependency_timeout: float = 180
    build_timeout: float = 600
    startup_timeout: float = 60
    request_timeout: float = 10
    ai_timeout: float = 30
    email_timeout: float = 15
    log_bytes: int = 32768
    response_bytes: int = 131072
    evidence_bytes: int = 2_000_000
    python_image: str = "python:3.12.14-slim-bookworm"
    uv_image: str = "ghcr.io/astral-sh/uv:0.9.26"
    wiremock_image: str = "wiremock/wiremock:3.13.1"
    target_python: str = "/app/.venv/bin/python"
    ai_key: SecretStr = SecretStr("")
    ai_model: str = ""
    ai_url: str = "https://api.openai.com/v1/chat/completions"
    email_key: SecretStr = SecretStr("")
    email_from: str = ""
    result_base_url: str = "http://localhost:3000/validations"
    secret_project_id: str = ""


def get_settings() -> Settings:
    return Settings()
