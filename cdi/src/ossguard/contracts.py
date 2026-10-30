from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Event(Model):
    step_key: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    sequence: int = Field(ge=1)
    node_id: str | None = None


class Evidence(Model):
    run_id: str
    env_id: Literal["baseline", "candidate"]
    case_id: str
    status: Literal["COMPLETE", "ERROR", "UNVERIFIED"] = "UNVERIFIED"
    response: dict[str, Any] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    step_map: dict[str, str] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    error: str | None = None


class Expectation(Model):
    op: Literal["eq", "gte", "event_count", "event_order"]
    path: str | None = None
    expected: Any
    step_key: str | None = None
    event_type: str | None = None

    @model_validator(mode="after")
    def valid_operation(self):
        if self.op in {"eq", "gte"} and not self.path:
            raise ValueError("field comparisons require a path")
        if self.op == "gte" and (type(self.expected) not in {int, float}):
            raise ValueError("gte expects a number")
        if self.op == "event_count" and (type(self.expected) is not int or self.expected < 0):
            raise ValueError("event_count expects a nonnegative integer")
        if self.op == "event_order" and (
            not isinstance(self.expected, list)
            or not self.expected
            or not all(isinstance(x, str) for x in self.expected)
        ):
            raise ValueError("event_order expects a nonempty list of step keys")
        return self


class Case(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,48}$")
    input: dict[str, Any]
    expectations: list[Expectation] = Field(min_length=1)


class Target(Model):
    image: str
    artifact: str = Field(min_length=1, max_length=128)


class Snapshot(Model):
    target_type: str = "node-red"
    confirmed: bool
    rules: str
    suite_revision: str
    baseline: Target
    candidate: Target
    cases: list[Case] = Field(min_length=1, max_length=30)
    step_map: dict[str, str]
    ignore_paths: list[str] = Field(default_factory=lambda: ["timestamp", "request_id"])
    job_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    request_timeout_seconds: int = Field(default=15, ge=1, le=120)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        if not self.step_map:
            raise ValueError("step_map is required")
        return self


class Environment(Model):
    run_id: str
    env_id: Literal["baseline", "candidate"]
    image: str
    artifact_hash: str
    adapter_version: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Cleanup(Model):
    status: Literal["COMPLETE", "PENDING"]
    remaining: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Adapter(Protocol):
    version: str

    async def prepare(self, run_id: UUID, env_id: str, target: Target, snapshot: Snapshot) -> Environment: ...
    async def start(self, environment: Environment) -> None: ...
    async def execute(self, environment: Environment, case: Case) -> Evidence: ...
    async def collect(self, environment: Environment, evidence: Evidence) -> Evidence: ...
    async def cleanup(self, run_id: UUID) -> Cleanup: ...
