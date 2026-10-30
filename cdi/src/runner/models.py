from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass
class Result:
    kind: str
    status: str = "ERROR"
    id: str = field(default_factory=lambda: str(uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    duration_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)
