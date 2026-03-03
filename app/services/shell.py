from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.types import CommandResult


def run_command(
    command: str | list[str],
    cwd: Path,
    timeout_sec: int,
    shell: bool = False,
) -> CommandResult:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        shell=shell,
        check=False,
    )
    return CommandResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
    )

