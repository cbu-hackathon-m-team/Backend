import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def relative_path(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_./-]+", value) or any(
        p in {"", ".", "..", ".git"} for p in value.split("/")
    ):
        raise ValueError("safe repository-relative path required")
    return value


def http_path(value: str) -> str:
    parts = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parts.scheme
        or parts.netloc
        or parts.fragment
        or "\\" in value
        or any(part in {".", ".."} for part in unquote(parts.path).split("/"))
        or "\\" in unquote(parts.path)
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("absolute HTTP path inside the test app required")
    return value


def repository(value: str) -> str:
    match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?", value)
    if not match or any(p in {".", ".."} for p in match.groups()):
        raise ValueError("public HTTPS github.com owner/repository URL required")
    return f"https://github.com/{match[1]}/{match[2]}"


class ProjectCreate(InputModel):
    name: str = Field(min_length=1, max_length=120)
    repository_url: str
    ref: str = Field(default="main", min_length=1, max_length=200, pattern=r"^[\w./-]+$")

    _repo = field_validator("repository_url")(repository)


class EnvDefinition(InputModel):
    required: bool = True
    secret: bool = False
    source: Literal["user", "runner_mock"] = "user"

    @model_validator(mode="after")
    def check_mock(self):
        if self.source == "runner_mock" and self.secret:
            raise ValueError("runner mock address is not secret")
        return self


class ExecutionProfile(InputModel):
    schema_version: Literal[1] = 1
    ecosystem: Literal["python-uv"] = "python-uv"
    dockerfile: Literal["Dockerfile.ossguard"] = "Dockerfile.ossguard"
    python: Literal["3.12"] = "3.12"
    app_port: int = Field(default=8000, ge=1024, le=65535)
    health_path: str = "/health"
    test_cases: str = "ossguard/tests.json"
    mock_stubs: str = "ossguard/stubs"
    env_schema: dict[str, EnvDefinition] = Field(default_factory=dict, max_length=30)

    _health = field_validator("health_path")(http_path)
    _paths = field_validator("test_cases", "mock_stubs")(relative_path)

    @field_validator("env_schema")
    @classmethod
    def env_names(cls, value):
        reserved = {"PATH", "HOME", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}
        for key, definition in value.items():
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", key)
                or key.startswith(("OSSGUARD_", "UV_", "DOCKER_", "PYTHON"))
                or key in reserved
            ):
                raise ValueError(f"unsupported environment variable: {key}")
            if (
                re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL", key)
                and not definition.secret
            ):
                raise ValueError(f"{key} must use a Secret Manager reference")
        return value


class EnvironmentUpdate(InputModel):
    values: dict[str, str] = Field(default_factory=dict, max_length=30)
    secret_refs: dict[str, str] = Field(default_factory=dict, max_length=30)

    @field_validator("values")
    @classmethod
    def env_values(cls, value):
        if any(len(v) > 4096 or "\n" in v or "\r" in v or "\0" in v for v in value.values()):
            raise ValueError("single-line environment value up to 4096 characters required")
        return value

    @field_validator("secret_refs")
    @classmethod
    def pinned_refs(cls, value):
        for ref in value.values():
            if not re.fullmatch(r"projects/[\w-]+/secrets/[\w-]+/versions/[1-9][0-9]*", ref):
                raise ValueError("numeric Secret Manager version reference required")
        return value


class JsonAssertion(InputModel):
    path: str = Field(max_length=200)
    op: Literal["eq", "gte"] = "eq"
    value: Any

    @model_validator(mode="after")
    def numeric_gte(self):
        if self.op == "gte" and (
            isinstance(self.value, bool) or not isinstance(self.value, (int, float))
        ):
            raise ValueError("gte requires a number")
        return self


class ExternalExpectation(InputModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str
    body_contains: dict[str, Any] = Field(default_factory=dict)
    count: int = Field(ge=0, le=100)
    response_status: int = Field(default=200, ge=100, le=599)

    _path = field_validator("path")(http_path)


class HttpCase(InputModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str
    body: dict[str, Any] | list | None = None
    expected_status: int = Field(default=200, ge=100, le=599)
    assertions: list[JsonAssertion] = Field(default_factory=list, max_length=30)
    external: list[ExternalExpectation] = Field(default_factory=list, max_length=20)
    ignore_paths: list[str] = Field(default_factory=list, max_length=30)

    _path = field_validator("path")(http_path)


class MockStub(InputModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    path: str
    status: int = Field(default=200, ge=100, le=599)
    body: dict[str, Any] = Field(default_factory=dict)

    _path = field_validator("path")(http_path)


class SuiteUpdate(InputModel):
    cases: list[HttpCase] = Field(min_length=1, max_length=30)
    stubs: list[MockStub] = Field(default_factory=list, max_length=30)
    approved: bool = False

    @model_validator(mode="after")
    def unique_ids(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("test case ids must be unique")
        return self


class RulesUpdate(InputModel):
    description: str = Field(default="", max_length=10000)
    input_schema: dict[str, Any] = Field(default_factory=dict)


class NotificationUpdate(InputModel):
    enabled: bool = False
    email: str = Field(default="", max_length=254)

    @model_validator(mode="after")
    def email_address(self):
        if self.enabled and not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", self.email):
            raise ValueError("valid recipient email required")
        return self


class ValidationCreate(InputModel):
    project_id: str
    package: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}$")
    target_version: str = Field(min_length=1, max_length=100)
    suite_id: str
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[\w-]+$")

    @field_validator("package")
    @classmethod
    def normalize_package(cls, value):
        return canonicalize_name(value)

    @field_validator("target_version")
    @classmethod
    def version(cls, value):
        try:
            return str(Version(value))
        except InvalidVersion as exc:
            raise ValueError("PEP 440 version required") from exc


class Analysis(InputModel):
    summary: str = Field(max_length=3000)
    candidates: list[str] = Field(default_factory=list, max_length=6)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class ProjectResponse(BaseModel):
    id: str
    name: str
    repository_url: str
    ref: str
    commit_sha: str | None
    revision: int
    execution_profile: dict
    environment_profile: dict
    rules: dict
    notification: dict
    inspection: dict
    created_at: datetime
    test_suites: list[dict] | None = None


class ValidationSubmitted(BaseModel):
    id: str
    execution_status: str


class ValidationResponse(BaseModel):
    id: str
    project_id: str
    package: str
    target_version: str
    execution_status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"]
    phase: str
    verdict: Literal["PASS", "REGRESSION_SUSPECTED", "BASELINE_INVALID", "NOT_EVALUATED"]
    error_code: str | None
    error_message: str | None
    runner_id: str | None
    heartbeat_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cleanup_status: str
    analysis_status: str
    analysis: dict
    snapshot: dict
    evidence: dict
    notification_statuses: list[str] | None = None
