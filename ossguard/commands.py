import asyncio
import os
import signal
import subprocess
from pathlib import Path

from ossguard.errors import GuardError


def execution_env():
    allowed = {
        "PATH",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "PATHEXT",
        "LOCALAPPDATA",
        "APPDATA",
        "USERPROFILE",
        "HOME",
        "PROGRAMDATA",
        "PROGRAMFILES",
    }
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "UV_NO_PROGRESS": "1",
        }
    )
    return env


async def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,  # noqa: ASYNC109 - subprocess wall timeout is enforced below
    limit: int = 32768,
    code: str = "COMMAND_FAILED",
):
    options = (
        {"start_new_session": True}
        if os.name != "nt"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    )
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=execution_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **options,
    )
    output = bytearray()
    total = 0

    async def drain():
        nonlocal total
        while chunk := await process.stdout.read(8192):
            total += len(chunk)
            output.extend(chunk)
            if len(output) > limit:
                del output[:-limit]
        await process.wait()

    try:
        await asyncio.wait_for(drain(), timeout)
    except BaseException as exc:
        if process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()
        if isinstance(exc, TimeoutError):
            failure = GuardError("STAGE_TIMEOUT", f"{args[0]} exceeded its wall timeout")
            failure.log = {
                "text": output.decode(errors="replace"),
                "original_bytes": total,
                "retained_bytes": len(output),
                "truncated": total > len(output),
            }
            raise failure from exc
        raise
    result = {
        "text": output.decode(errors="replace"),
        "original_bytes": total,
        "retained_bytes": len(output),
        "truncated": total > len(output),
    }
    if process.returncode:
        exc = GuardError(code, f"{args[0]} exited with status {process.returncode}")
        exc.log = result
        raise exc
    return result
