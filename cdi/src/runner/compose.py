import re
import subprocess
from pathlib import Path


class Compose:
    """Control only explicitly named runner projects using trusted Compose files."""

    def __init__(self, file: Path, project: str):
        if not re.fullmatch(r"runner-[a-z0-9][a-z0-9-]{0,48}", project):
            raise ValueError(
                "Project must start with runner- and contain lowercase letters, numbers or hyphens"
            )
        self.file = file.resolve(strict=True)
        self.project = project

    def _run(self, *arguments: str, timeout: int = 180):
        return subprocess.run(
            ["docker", "compose", "-f", str(self.file), "-p", self.project, *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        ).stdout

    def up(self):
        return self._run("up", "-d", "--wait", "--wait-timeout", "120")

    def down(self):
        # Preserve volumes unless a later explicit lifecycle policy says otherwise.
        return self._run("down", timeout=60)

    def logs(self):
        return self._run("logs", "--no-color", "--tail", "200", timeout=30)
