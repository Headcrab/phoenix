from __future__ import annotations

from app.core.config import Settings
from app.services.shell import run_command


class LifecycleManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._repo_path = settings.repo_path

    def restart(self) -> tuple[bool, str]:
        command = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/restart_service.ps1",
        ]
        result = run_command(command, self._repo_path, 120)
        details = "\n".join(x for x in [result.stdout, result.stderr] if x)
        return result.ok, details

    def health_check(self) -> tuple[bool, str]:
        command = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/health_check.ps1",
        ]
        result = run_command(command, self._repo_path, 120)
        details = "\n".join(x for x in [result.stdout, result.stderr] if x)
        return result.ok, details
