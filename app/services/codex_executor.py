from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.services.shell import run_command
from app.services.types import ExecutionResult


class CodexExecutor:
    def __init__(
        self,
        repo_path: Path,
        executor_cmd: str,
        timeout_sec: int,
    ):
        self._repo_path = repo_path
        self._executor_cmd = executor_cmd
        self._timeout_sec = timeout_sec

    def run(self, instruction: str, task_id: str) -> ExecutionResult:
        if not self._executor_cmd:
            return ExecutionResult(
                ok=False,
                summary="PHOENIX_EXECUTOR_CMD is not configured",
                details="Set PHOENIX_EXECUTOR_CMD to run external Codex worker command.",
            )
        payload_path = self._repo_path / ".phoenix" / f"task-{task_id}.json"
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        payload_path.write_text(
            json.dumps({"task_id": task_id, "instruction": instruction}, ensure_ascii=True),
            encoding="utf-8",
        )
        command = shlex.split(self._executor_cmd) + [str(payload_path)]
        result = run_command(command=command, cwd=self._repo_path, timeout_sec=self._timeout_sec)
        if result.ok:
            return ExecutionResult(
                ok=True,
                summary="Executor finished successfully",
                details=result.stdout,
            )
        details = "\n".join(x for x in [result.stdout, result.stderr] if x)
        return ExecutionResult(
            ok=False,
            summary="Executor command failed",
            details=details or f"exit code {result.returncode}",
        )

