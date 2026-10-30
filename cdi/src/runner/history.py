import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from runner.models import Result


class History:
    """Replace this adapter when integrating with the API team's database."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, result: Result):
        with self._connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?)",
                (result.id, result.started_at, json.dumps(result.to_dict(), ensure_ascii=False)),
            )

    def latest(self, limit: int = 20):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
