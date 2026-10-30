import json
import re
from typing import Any

SENSITIVE = re.compile(r"authorization|password|secret|token|credential|api[-_]?key|cookie", re.I)


class Redactor:
    def __init__(self, secrets=()):
        self.secrets = sorted({str(v) for v in secrets if v}, key=len, reverse=True)

    def text(self, value: str):
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(
            r"(?i)((?:authorization|password|api[_-]?key|token|secret)\s*[=:]\s*)([^\s,;]+)",
            r"\1[REDACTED]",
            value,
        )

    def clean(self, value: Any):
        if isinstance(value, dict):
            return {
                str(k): "[REDACTED]" if SENSITIVE.search(str(k)) else self.clean(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return self.text(value) if isinstance(value, str) else value


def bounded_text(value: str, limit: int, redactor: Redactor):
    original = len(value.encode())
    clean = redactor.text(value).encode()
    retained = clean[-limit:].decode(errors="replace")
    return {
        "text": retained,
        "original_bytes": original,
        "retained_bytes": len(retained.encode()),
        "truncated": len(clean) > limit,
    }


def bounded_value(value: Any, limit: int, redactor: Redactor):
    clean = redactor.clean(value)
    encoded = json.dumps(clean, ensure_ascii=False).encode()
    if len(encoded) <= limit:
        return clean
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "retained_bytes": 0,
        "reason": "evidence size limit",
    }
