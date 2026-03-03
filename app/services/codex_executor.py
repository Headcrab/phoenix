from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

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

    def run(
        self,
        instruction: str,
        task_id: str,
        on_output: Callable[[str], None] | None = None,
    ) -> ExecutionResult:
        try:
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
            command = self._build_command(instruction, payload_path)
            ok, returncode, output = self._run_streaming_command(command, on_output)
            if ok:
                return ExecutionResult(
                    ok=True,
                    summary="Executor finished successfully",
                    details=output,
                )
            return ExecutionResult(
                ok=False,
                summary="Executor command failed",
                details=output or f"exit code {returncode}",
            )
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                ok=False,
                summary="Executor crashed",
                details=str(exc),
            )

    def _build_command(self, instruction: str, payload_path: Path) -> str:
        trimmed = self._executor_cmd.strip()
        lower = trimmed.lower()
        is_codex = (
            lower == "codex"
            or lower.endswith("\\codex.ps1")
            or lower.endswith("\\codex.cmd")
        )
        if is_codex:
            prompt = instruction.replace('"', '\\"')
            return (
                "powershell -NoProfile -ExecutionPolicy Bypass "
                f"-Command \"{trimmed} exec -s workspace-write \\\"{prompt}\\\"\""
            )
        payload_arg = str(payload_path).replace('"', '\\"')
        return (
            "powershell -NoProfile -ExecutionPolicy Bypass "
            f"-Command \"{trimmed} \\\"{payload_arg}\\\"\""
        )

    def _run_streaming_command(
        self,
        command: str,
        on_output: Callable[[str], None] | None = None,
    ) -> tuple[bool, int, str]:
        proc = subprocess.Popen(
            command,
            cwd=str(self._repo_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=True,
        )
        started_at = time.monotonic()
        lines: list[str] = []
        if not proc.stdout:
            proc.wait(timeout=self._timeout_sec)
            return proc.returncode == 0, proc.returncode, ""

        while True:
            if time.monotonic() - started_at > self._timeout_sec:
                proc.kill()
                timeout_line = f"Executor timeout after {self._timeout_sec} seconds"
                lines.append(timeout_line)
                if on_output:
                    on_output(timeout_line)
                return False, 124, "\n".join(lines).strip()

            line = proc.stdout.readline()
            if line:
                cleaned = line.rstrip()
                if cleaned:
                    lines.append(cleaned)
                    if on_output:
                        on_output(cleaned)
                continue

            if proc.poll() is not None:
                break
            time.sleep(0.05)

        tail = proc.stdout.read()
        if tail:
            for line in tail.splitlines():
                cleaned = line.rstrip()
                if cleaned:
                    lines.append(cleaned)
                    if on_output:
                        on_output(cleaned)

        returncode = proc.wait(timeout=10)
        return returncode == 0, returncode, "\n".join(lines).strip()

