from __future__ import annotations

from types import SimpleNamespace

from app.channels.telegram.bot import BotRuntimeConfig, TelegramBot


class FakeApi:
    def send_message(self, chat_id: int, text: str) -> None:  # noqa: ARG002
        return None


class FakeOrchestrator:
    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, object]] = {
            "task-1": {
                "id": "task-1",
                "status": "running",
                "instruction": "Initial task",
                "events": [{"id": 1, "created_at": "2026-03-03T00:00:00Z", "message": "Started"}],
            }
        }
        self.submitted: list[str] = []

    def submit_task(
        self,
        instruction: str,
        priority: str,  # noqa: ARG002
        idempotency_key: str | None,  # noqa: ARG002
        process_now: bool,  # noqa: ARG002
    ) -> SimpleNamespace:
        self.submitted.append(instruction)
        task_id = f"task-{len(self._tasks) + 1}"
        self._tasks[task_id] = {
            "id": task_id,
            "status": "queued",
            "instruction": instruction,
            "events": [],
        }
        return SimpleNamespace(task_id=task_id, status="queued")

    def get_task(self, task_id: str) -> dict[str, object] | None:
        return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50, status: str | None = None) -> list[dict[str, object]]:
        items = list(self._tasks.values())
        if status:
            items = [task for task in items if task.get("status") == status]
        return items[:limit]

    def list_subagents(
        self,
        limit: int = 100,
        active_only: bool = False,
    ) -> list[dict[str, object]]:
        rows = [
            {
                "id": "codex:task-1",
                "kind": "codex",
                "status": "running",
                "activity": "Doing work",
                "task_id": "task-1",
                "updated_at": "2026-03-03T00:00:00Z",
            }
        ]
        if active_only:
            return rows[:limit]
        return rows[:limit]

    def rollback_task(self, task_id: str) -> dict[str, object]:
        task = self._tasks[task_id]
        task["status"] = "rolled_back"
        return task

    def process_next_queued(self) -> None:
        return None

    def sync_waiting_prs(self) -> None:
        return None


class FakeKagi:
    configured = True

    def __init__(self) -> None:
        self.last_notice = ""
        self.last_query = ""

    def search(self, query: str, limit: int = 5):  # noqa: ANN001, ARG002
        self.last_query = query
        return [
            SimpleNamespace(
                rank=1,
                title="Phoenix",
                url="https://example.com/phoenix",
                snippet="Phoenix search result",
            )
        ]


class FakeGemini:
    configured = True

    def __init__(self, decision: SimpleNamespace, chat_answer: str = "") -> None:
        self._decision = decision
        self._chat_answer = chat_answer
        self.last_notice = ""

    def route_intent(
        self,
        user_text: str,  # noqa: ARG002
        active_subagents: list[dict[str, object]],  # noqa: ARG002
        tracked_task_ids: list[str],  # noqa: ARG002
    ) -> SimpleNamespace:
        return self._decision

    def chat(self, history: list[dict[str, str]], user_text: str) -> str:  # noqa: ARG002
        return self._chat_answer


def _build_bot(
    orchestrator: FakeOrchestrator,
    gemini: FakeGemini | None = None,
    kagi: FakeKagi | None = None,
) -> TelegramBot:
    return TelegramBot(
        orchestrator=orchestrator,
        api=FakeApi(),
        config=BotRuntimeConfig(),
        gemini_chat=gemini,
        kagi_search=kagi,
    )


def test_search_command_uses_kagi_service() -> None:
    orchestrator = FakeOrchestrator()
    kagi = FakeKagi()
    bot = _build_bot(orchestrator=orchestrator, kagi=kagi)

    reply = bot._handle_text(chat_id=1, text="/search phoenix agent")

    assert "Результаты поиска:" in reply
    assert "https://example.com/phoenix" in reply
    assert kagi.last_query == "phoenix agent"


def test_natural_language_status_uses_gemini_router() -> None:
    orchestrator = FakeOrchestrator()
    decision = SimpleNamespace(action="show_status", instruction=None, task_id=None, reply=None)
    bot = _build_bot(orchestrator=orchestrator, gemini=FakeGemini(decision=decision))
    bot._last_task_by_chat[1] = "task-1"

    reply = bot._handle_text(chat_id=1, text="какой статус?")

    assert "task_id: task-1" in reply
    assert "status: running" in reply


def test_natural_language_chat_falls_back_to_gemini_chat() -> None:
    orchestrator = FakeOrchestrator()
    decision = SimpleNamespace(action="chat", instruction=None, task_id=None, reply=None)
    gemini = FakeGemini(decision=decision, chat_answer="Сделал анализ.")
    bot = _build_bot(orchestrator=orchestrator, gemini=gemini)

    reply = bot._handle_text(chat_id=7, text="что ты умеешь?")

    assert reply == "Сделал анализ."
    assert len(bot._chat_history_by_chat[7]) == 2


def test_plain_text_without_gemini_submits_task() -> None:
    orchestrator = FakeOrchestrator()
    bot = _build_bot(orchestrator=orchestrator)

    reply = bot._handle_text(chat_id=3, text="Добавь новый флоу")

    assert "Задача поставлена в очередь." in reply
    assert orchestrator.submitted == ["Добавь новый флоу"]
