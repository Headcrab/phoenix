from __future__ import annotations

from pathlib import Path

from app.services.shell import run_command
from app.services.types import ValidationResult


class Validator:
    def __init__(self, repo_path: Path, timeout_sec: int):
        self._repo_path = repo_path
        self._timeout_sec = timeout_sec
        self._steps = [
            ("lint", ["powershell", "-ExecutionPolicy", "Bypass", "-File", "scripts/run_lint.ps1"]),
            (
                "tests",
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", "scripts/run_tests.ps1"],
            ),
            (
                "health-check",
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", "scripts/health_check.ps1"],
            ),
        ]

    def run(self) -> ValidationResult:
        results: list[dict[str, object]] = []
        all_ok = True
        for name, command in self._steps:
            command_result = run_command(
                command=command,
                cwd=self._repo_path,
                timeout_sec=self._timeout_sec,
            )
            step_ok = command_result.ok
            all_ok = all_ok and step_ok
            results.append(
                {
                    "name": name,
                    "ok": step_ok,
                    "returncode": command_result.returncode,
                    "stdout": command_result.stdout,
                    "stderr": command_result.stderr,
                }
            )
            if not step_ok:
                break
        return ValidationResult(ok=all_ok, steps=results)
