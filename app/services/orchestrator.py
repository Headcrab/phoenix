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

    @staticmethod
    def _subagent_id(task_id: str) -> str:
        return f"codex:{task_id}"

    def _set_subagent(
        self,
        task_id: str,
        status: str,
        activity: str,
        details: str = "",
    ) -> None:
        self._repo.upsert_subagent(
            subagent_id=self._subagent_id(task_id),
            kind="codex",
            task_id=task_id,
            status=status,
            activity=activity,
            details=details[:1000],
        )

    def submit_task(
        self,
        instruction: str,
        priority: str = "normal",
        idempotency_key: str | None = None,
        process_now: bool | None = None,
    ) -> SubmitResult:
        task = self._repo.create_task(instruction, priority, idempotency_key)
        self._repo.append_event(task["id"], "Queued for execution")
        self._set_subagent(task["id"], "queued", "Ожидает запуска")
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
            self._set_subagent(task_id, "running", "Подготовка окружения")
            self._repo.append_event(task_id, "Starting executor")
            self._gitops.ensure_repo()

            branch = self._gitops.create_task_branch(task_id, task["instruction"])
            self._repo.update_task(task_id, branch_name=branch)
            self._repo.append_event(task_id, f"Using branch {branch}")

            self._set_subagent(task_id, "running", "Codex выполняет задачу")
            execution = self._executor.run(
                task["instruction"],
                task_id,
                on_output=lambda line: self._on_executor_output(task_id, line),
            )
            self._repo.append_event(task_id, f"Executor: {execution.summary}")
            if execution.details:
                self._repo.append_event(task_id, execution.details[:1500])
            if not execution.ok:
                self._set_subagent(task_id, "failed", "Ошибка Codex", execution.summary)
                self._repo.update_task(
                    task_id,
                    status="executor_failed",
                    last_error=execution.summary,
                )
                return

            self._set_subagent(task_id, "running", "Проверка lint/tests/health")
            validation = self._validator.run()
            self._repo.append_event(task_id, f"Validation report: {json.dumps(validation.steps)}")
            if not validation.ok:
                self._set_subagent(task_id, "failed", "Проверки не пройдены")
                self._repo.update_task(
                    task_id,
                    status="validation_failed",
                    last_error="Validation failed",
                )
                return

            if not self._gitops.has_changes():
                self._set_subagent(task_id, "failed", "После Codex нет изменений в файлах")
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
            self._set_subagent(task_id, "running", "Код отправлен в git, создаю PR")

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
                self._set_subagent(task_id, "waiting", "Ожидание CI и auto-merge")
            else:
                self._repo.append_event(task_id, f"PR skipped: {pr.details}")
                self._post_merge_restart_flow(task_id)
        except Exception as exc:  # noqa: BLE001
            self._set_subagent(task_id, "failed", "Непредвиденная ошибка", str(exc))
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
                self._set_subagent(task["id"], "failed", "Ошибка CI/merge", result.message)
                self._repo.update_task(
                    task["id"],
                    status="git_failed",
                    last_error=result.message,
                )
            elif result.merged:
                self._set_subagent(task["id"], "running", "CI пройден, перезапуск агента")
                self._post_merge_restart_flow(task["id"])

    def _post_merge_restart_flow(self, task_id: str) -> None:
        try:
            self._set_subagent(task_id, "running", "Обновляю main и перезапускаю сервис")
            self._gitops.checkout_main_and_pull()
            self._repo.append_event(task_id, "Checked out main and pulled latest")
            restart_ok, restart_details = self._lifecycle.restart()
            if restart_details:
                self._repo.append_event(task_id, restart_details[:1500])
            if not restart_ok:
                self._set_subagent(task_id, "failed", "Ошибка перезапуска сервиса")
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
                self._set_subagent(task_id, "completed", "Обновление успешно применено")
                self._repo.update_task(task_id, status="completed")
                self._repo.append_event(task_id, "Task completed successfully")
                return

            self._repo.append_event(task_id, "Health-check failed after restart, rolling back")
            self._set_subagent(task_id, "running", "Health-check failed, выполняю rollback")
            rollback_sha = self._gitops.revert_head_and_push(task_id)
            self._repo.append_event(task_id, f"Rollback commit created: {rollback_sha}")
            restart_ok_2, details_2 = self._lifecycle.restart()
            if details_2:
                self._repo.append_event(task_id, details_2[:1500])
            if restart_ok_2:
                self._set_subagent(task_id, "rolled_back", "Откат выполнен успешно")
                self._repo.update_task(
                    task_id,
                    status="rolled_back",
                    last_error="Rolled back after failed health-check",
                )
            else:
                self._set_subagent(
                    task_id,
                    "failed",
                    "Откат выполнен, но рестарт после отката упал",
                )
                self._repo.update_task(
                    task_id,
                    status="restart_failed",
                    last_error="Rollback restart failed",
                )
        except Exception as exc:  # noqa: BLE001
            self._set_subagent(task_id, "failed", "Ошибка post-merge этапа", str(exc))
            self._repo.update_task(task_id, status="restart_failed", last_error=str(exc))
            self._repo.append_event(task_id, f"Post-merge flow error: {exc}")

    def rollback_task(self, task_id: str) -> dict[str, Any]:
        self._set_subagent(task_id, "running", "Ручной rollback")
        rollback_sha = self._gitops.revert_head_and_push(task_id)
        restart_ok, details = self._lifecycle.restart()
        health_ok, health_details = self._lifecycle.health_check()
        status = "rolled_back" if restart_ok and health_ok else "restart_failed"
        subagent_status = "rolled_back" if status == "rolled_back" else "failed"
        self._set_subagent(task_id, subagent_status, f"Ручной rollback: {status}")
        task = self._repo.update_task(task_id, status=status, last_error=None)
        self._repo.append_event(task_id, f"Manual rollback SHA: {rollback_sha}")
        if details:
            self._repo.append_event(task_id, details[:1500])
        if health_details:
            self._repo.append_event(task_id, health_details[:1500])
        return task

    def _on_executor_output(self, task_id: str, line: str) -> None:
        text = line.strip()
        if not text:
            return
        self._repo.append_event(task_id, f"codex> {text[:500]}")
        self._set_subagent(task_id, "running", f"Codex: {text[:200]}")

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._repo.get_task(task_id)
        if not task:
            return None
        task["events"] = self._repo.get_events(task_id)
        return task

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return self._repo.list_tasks(limit=limit, status=status)

    def list_subagents(self, limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
        return self._repo.list_subagents(limit=limit, active_only=active_only)

