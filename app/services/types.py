from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class ExecutionResult:
    ok: bool
    summary: str
    details: str = ""


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    steps: list[dict[str, object]]


@dataclass(slots=True)
class PullRequestResult:
    created: bool
    number: int | None
    url: str | None
    details: str


@dataclass(slots=True)
class MergeCheckResult:
    merged: bool
    pending: bool
    failed: bool
    message: str

