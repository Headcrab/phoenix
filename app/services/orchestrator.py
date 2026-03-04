from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.db.repository import TaskRepository
from app.services.codex_executor import CodexExecutor
from app.services.gitops import GitOps
from app.services.lifecycle import LifecycleManager
from app.services.validator import Validator


@dataclass(slots=True)
class SubmitResult:
    task_id: str
    status: str


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        repository: TaskRepository,
        executor: CodexExecutor,
        validator: Validator,
        gitops: GitOps,
        lifecycle: LifecycleManager,
    ):
        self._settings = settings
        self._repo = repository
        self._executor = executor
        self._validator = validator
        self._gitops = gitops
        self._lifecycle = lifecycle
        self._lock = threading.Lock()

    def submit_task(
        self,
        instruction: str,
        priority: str = "normal",
        idempotency_key: str | None = None,
        process_now: bool | None = None,
    ) -> SubmitResult:
        task = self._repo.create_task(instruction, priority, idempotency_key)
        self._repo.append_event(task["id"], "Queued for execution")
        do_process = self._settings.auto_process_on_submit if process_now is None else process_now
        if do_process:
            self.process_task(task["id"])
        refreshed = self._repo.get_task(task["id"])
        return SubmitResult(
            task_id=task["id"],
            status=refreshed["status"] if refreshed else "unknown",
        )

    def process_next_queued(self) -> None:
        if self._lock.locked():
            return
        tasks = self._repo.list_tasks_by_status("queued")
        if not tasks:
            return
        self.process_task(tasks[0]["id"])

    def process_task(self, task_id: str) -> None:
        if not self._lock.acquire(blocking=False):
            self._repo.append_event(task_id, "Skipped processing: another task is running")
            return
        try:
            task = self._repo.get_task(task_id)
            if not task:
                return
            if task["status"] not in {"queued", "running"}:
                return
            self._repo.update_task(task_id, status="running", last_error=None)
            self._repo.append_event(task_id, "Starting executor")
            self._gitops.ensure_repo()
            branch = self._gitops.create_task_branch(task_id, task["instruction"])
            self._repo.update_task(task_id, branch_name=branch)
            self._repo.append_event(task_id, f"Using branch {branch}")
            execution = self._executor.run(
                task["instruction"],
                task_id,
                on_output=lambda line: self._repo.append_event(task_id, f"codex> {line[:500]}"),
            )
            self._repo.append_event(task_id, f"Executor: {execution.summary}")
            if execution.details:
                self._repo.append_event(task_id, execution.details[:1500])
            if not execution.ok:
                self._repo.update_task(
                    task_id,
                    status="executor_failed",
                    last_error=execution.summary,
                )
                return

            validation = self._validator.run()
            self._repo.append_event(task_id, f"Validation report: {json.dumps(validation.steps)}")
            if not validation.ok:
                self._repo.update_task(
                    task_id,
                    status="validation_failed",
                    last_error="Validation failed",
                )
                return

            if not self._gitops.has_changes():
                self._repo.update_task(
                    task_id,
                    status="git_failed",
                    last_error="No file changes after execution",
                )
                return

            message = f"feat(agent): self-improve [task:{task_id}]"
            commit_sha = self._gitops.commit_all(message)
            self._repo.update_task(task_id, commit_sha=commit_sha)
            self._repo.append_event(task_id, f"Committed {commit_sha}")
            self._gitops.push_branch(branch)
            self._repo.append_event(task_id, f"Pushed branch {branch}")

            pr = self._gitops.create_pull_request(
                branch=branch,
                title=f"agent: self-improve task {task_id[:8]}",
                body=f"Automated self-improve task.\n\nTask ID: `{task_id}`",
            )
            if pr.created:
                self._repo.update_task(
                    task_id,
                    status="waiting_ci",
                    pr_number=pr.number,
                    pr_url=pr.url,
                )
                self._repo.append_event(task_id, f"PR created: {pr.url}")
            else:
                self._repo.append_event(task_id, f"PR skipped: {pr.details}")
                self._post_merge_restart_flow(task_id)
        except Exception as exc:  # noqa: BLE001
            self._repo.update_task(task_id, status="git_failed", last_error=str(exc))
            self._repo.append_event(task_id, f"Unexpected error: {exc}")
        finally:
            self._lock.release()

    def sync_waiting_prs(self) -> None:
        waiting = self._repo.list_tasks_by_status("waiting_ci")
        for task in waiting:
            pr_number = task.get("pr_number")
            if not pr_number:
                continue
            try:
                result = self._gitops.check_and_maybe_merge(pr_number)
            except Exception as exc:  # noqa: BLE001
                self._repo.append_event(task["id"], f"PR sync error: {exc}")
                continue
            self._repo.append_event(task["id"], result.message)
            if result.failed:
                self._repo.update_task(
                    task["id"],
                    status="git_failed",
                    last_error=result.message,
                )
            elif result.merged:
                self._post_merge_restart_flow(task["id"])

    def _post_merge_restart_flow(self, task_id: str) -> None:
        try:
            self._gitops.checkout_main_and_pull()
            self._repo.append_event(task_id, "Checked out main and pulled latest")
            restart_ok, restart_details = self._lifecycle.restart()
            if restart_details:
                self._repo.append_event(task_id, restart_details[:1500])
            if not restart_ok:
                self._repo.update_task(
                    task_id,
                    status="restart_failed",
                    last_error="Service restart failed",
                )
                return
            health_ok, health_details = self._lifecycle.health_check()
            if health_details:
                self._repo.append_event(task_id, health_details[:1500])
            if health_ok:
                self._repo.update_task(task_id, status="completed")
                self._repo.append_event(task_id, "Task completed successfully")
                return

            self._repo.append_event(task_id, "Health-check failed after restart, rolling back")
            rollback_sha = self._gitops.revert_head_and_push(task_id)
            self._repo.append_event(task_id, f"Rollback commit created: {rollback_sha}")
            restart_ok_2, details_2 = self._lifecycle.restart()
            if details_2:
                self._repo.append_event(task_id, details_2[:1500])
            if restart_ok_2:
                self._repo.update_task(
                    task_id,
                    status="rolled_back",
                    last_error="Rolled back after failed health-check",
                )
            else:
                self._repo.update_task(
                    task_id,
                    status="restart_failed",
                    last_error="Rollback restart failed",
                )
        except Exception as exc:  # noqa: BLE001
            self._repo.update_task(task_id, status="restart_failed", last_error=str(exc))
            self._repo.append_event(task_id, f"Post-merge flow error: {exc}")

    def rollback_task(self, task_id: str) -> dict[str, Any]:
        rollback_sha = self._gitops.revert_head_and_push(task_id)
        restart_ok, details = self._lifecycle.restart()
        health_ok, health_details = self._lifecycle.health_check()
        status = "rolled_back" if restart_ok and health_ok else "restart_failed"
        task = self._repo.update_task(task_id, status=status, last_error=None)
        self._repo.append_event(task_id, f"Manual rollback SHA: {rollback_sha}")
        if details:
            self._repo.append_event(task_id, details[:1500])
        if health_details:
            self._repo.append_event(task_id, health_details[:1500])
        return task

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._repo.get_task(task_id)
        if not task:
            return None
        task["events"] = self._repo.get_events(task_id)
        return task

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._repo.list_tasks(limit=limit, status=status)
