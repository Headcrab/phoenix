from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.channels.telegram.bot import TelegramBot
from app.services.orchestrator import SubmitResult


class FakeApiClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def get_updates(self, offset: int | None, timeout_sec: int) -> list[dict[str, Any]]:
        return []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class FakeOrchestrator:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.tasks: dict[str, dict[str, Any]] = {
            "task-1": {
                "id": "task-1",
                "status": "queued",
                "priority": "normal",
                "branch_name": None,
                "pr_url": None,
                "last_error": None,
                "updated_at": "2026-03-03T00:00:00+00:00",
                "events": [
                    {
                        "created_at": "2026-03-03T00:00:01+00:00",
                        "message": "Task created",
                    }
                ],
            }
        }

    def submit_task(
        self,
        instruction: str,
        priority: str = "normal",
        idempotency_key: str | None = None,
        process_now: bool | None = None,
    ) -> SubmitResult:
        self.submitted.append(instruction)
        task_id = "task-2"
        self.tasks[task_id] = {
            "id": task_id,
            "status": "running",
            "priority": priority,
            "branch_name": None,
            "pr_url": None,
            "last_error": None,
            "updated_at": "2026-03-03T00:00:02+00:00",
            "events": [],
        }
        return SubmitResult(task_id=task_id, status="running")

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        tasks = list(self.tasks.values())
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
        return tasks[:limit]

    def rollback_task(self, task_id: str) -> dict[str, Any]:
        task = self.tasks[task_id]
        task["status"] = "rolled_back"
        return task


@dataclass
class FakeGemini:
    configured: bool = True
    last_notice: str = ""

    def chat(self, history: list[dict[str, str]], user_text: str) -> str:
        return f"AI: {user_text}"


def _update(text: str, chat_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": 100,
        "message": {
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def test_telegram_improve_command_submits_task() -> None:
    api = FakeApiClient()
    orchestrator = FakeOrchestrator()
    bot = TelegramBot(api_client=api, orchestrator=orchestrator, gemini=FakeGemini())

    bot.handle_update(_update("/improve improve validation"))

    assert orchestrator.submitted == ["improve validation"]
    assert api.messages
    assert "Задача поставлена: task-2" in api.messages[-1][1]


def test_telegram_uses_gemini_for_plain_text() -> None:
    api = FakeApiClient()
    orchestrator = FakeOrchestrator()
    bot = TelegramBot(api_client=api, orchestrator=orchestrator, gemini=FakeGemini())

    bot.handle_update(_update("Какой статус у системы?"))

    assert api.messages
    assert api.messages[-1][1] == "AI: Какой статус у системы?"


def test_telegram_blocks_disallowed_chat() -> None:
    api = FakeApiClient()
    orchestrator = FakeOrchestrator()
    bot = TelegramBot(
        api_client=api,
        orchestrator=orchestrator,
        gemini=FakeGemini(),
        allowed_chat_ids={42},
    )

    bot.handle_update(_update("/help", chat_id=7))

    assert api.messages
    assert "Доступ запрещен" in api.messages[-1][1]


def test_telegram_status_not_found() -> None:
    api = FakeApiClient()
    orchestrator = FakeOrchestrator()
    bot = TelegramBot(api_client=api, orchestrator=orchestrator, gemini=FakeGemini())

    bot.handle_update(_update("/status missing"))

    assert api.messages
    assert api.messages[-1][1] == "Задача не найдена."
