from pathlib import Path

from app.core.config import Settings
from app.db.repository import TaskRepository
from app.services.orchestrator import Orchestrator
from app.services.types import ExecutionResult, PullRequestResult, ValidationResult


class FakeExecutor:
    def __init__(self, ok: bool):
        self._ok = ok

    def run(self, instruction: str, task_id: str, on_output=None) -> ExecutionResult:
        if on_output:
            on_output("fake executor output")
        if self._ok:
            return ExecutionResult(ok=True, summary="ok", details="done")
        return ExecutionResult(ok=False, summary="fail", details="bad")


class FakeValidator:
    def __init__(self, ok: bool):
        self._ok = ok

    def run(self) -> ValidationResult:
        return ValidationResult(ok=self._ok, steps=[{"name": "x", "ok": self._ok}])


class FakeGitOps:
    def ensure_repo(self) -> None:
        return None

    def create_task_branch(self, task_id: str, instruction: str) -> str:
        return f"agent/{task_id[:8]}"

    def has_changes(self) -> bool:
        return True

    def commit_all(self, message: str) -> str:
        return "abc123"

    def push_branch(self, branch: str) -> None:
        return None

    def create_pull_request(self, branch: str, title: str, body: str) -> PullRequestResult:
        return PullRequestResult(created=False, number=None, url=None, details="disabled")

    def checkout_main_and_pull(self) -> None:
        return None

    def revert_head_and_push(self, task_id: str) -> str:
        return "rollback123"

    def check_and_maybe_merge(self, pr_number: int):
        raise AssertionError("not used")


class FakeLifecycle:
    def restart(self):
        return True, "restarted"

    def health_check(self):
        return True, "healthy"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        repo_path=tmp_path,
        db_path=tmp_path / "phoenix.db",
        main_branch="main",
        remote_name="origin",
        executor_cmd="",
        executor_timeout_sec=10,
        quality_gate_timeout_sec=10,
        auto_process_on_submit=True,
        auto_merge=True,
        ci_poll_interval_sec=5,
        queue_poll_interval_sec=5,
        service_name="svc",
        healthcheck_url="http://127.0.0.1:8666/health",
        api_host="127.0.0.1",
        api_port=8666,
        github_owner="",
        github_repo="",
        github_token="",
        gemini_api_key="",
        gemini_model="gemini-3.1",
        gemini_timeout_sec=30,
        kagi_api_key="",
        kagi_api_base_url="https://kagi.com/api/v0",
        kagi_timeout_sec=20,
        telegram_bot_token="",
        telegram_allowed_chat_ids=None,
        telegram_poll_timeout_sec=25,
        telegram_queue_poll_interval_sec=5,
        telegram_ci_poll_interval_sec=5,
    )


def test_orchestrator_completes_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = TaskRepository(settings.db_path)
    orchestrator = Orchestrator(
        settings=settings,
        repository=repo,
        executor=FakeExecutor(ok=True),
        validator=FakeValidator(ok=True),
        gitops=FakeGitOps(),
        lifecycle=FakeLifecycle(),
    )
    result = orchestrator.submit_task("Improve yourself")
    task = orchestrator.get_task(result.task_id)
    assert task is not None
    assert task["status"] == "completed"
    assert task["commit_sha"] == "abc123"


def test_orchestrator_validation_failure(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    repo = TaskRepository(settings.db_path)
    orchestrator = Orchestrator(
        settings=settings,
        repository=repo,
        executor=FakeExecutor(ok=True),
        validator=FakeValidator(ok=False),
        gitops=FakeGitOps(),
        lifecycle=FakeLifecycle(),
    )
    result = orchestrator.submit_task("Break tests")
    task = orchestrator.get_task(result.task_id)
    assert task is not None
    assert task["status"] == "validation_failed"
