from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_LEN = 4000
DEFAULT_POLL_TIMEOUT_SEC = 25


class TelegramApiError(RuntimeError):
    pass


class TelegramApiClient:
    def __init__(self, token: str, session: requests.Session | None = None):
        if not token:
            raise ValueError("Telegram bot token is required")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session = session or requests.Session()

    def get_updates(self, offset: int | None, timeout_sec: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout_sec, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        data = self._call("getUpdates", payload, timeout_sec + 10)
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, chat_id: int, text: str) -> None:
        payload = {
            "chat_id": chat_id,
            "text": text[:MAX_TELEGRAM_MESSAGE_LEN],
            "disable_web_page_preview": True,
        }
        self._call("sendMessage", payload, 15)

    def _call(self, method: str, payload: dict[str, Any], timeout_sec: int) -> dict[str, Any]:
        try:
            response = self._session.post(
                f"{self._base_url}/{method}",
                json=payload,
                timeout=timeout_sec,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise TelegramApiError(str(exc)) from exc
        if not body.get("ok"):
            description = body.get("description", "Telegram API error")
            raise TelegramApiError(str(description))
        return body


@dataclass(slots=True)
class BotRuntimeConfig:
    poll_timeout_sec: int = DEFAULT_POLL_TIMEOUT_SEC
    queue_poll_interval_sec: int = 20
    ci_poll_interval_sec: int = 30
    allowed_chat_ids: set[int] | None = None


class TelegramBot:
    def __init__(
        self,
        orchestrator: Any,
        api: TelegramApiClient,
        config: BotRuntimeConfig,
        gemini_chat: Any | None = None,
        kagi_search: Any | None = None,
    ):
        self._orchestrator = orchestrator
        self._api = api
        self._config = config
        self._gemini_chat = gemini_chat
        self._kagi_search = kagi_search
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._last_task_by_chat: dict[int, str] = {}
        self._chat_history_by_chat: dict[int, list[dict[str, str]]] = {}

    def run_forever(self) -> None:
        self._worker_thread.start()
        offset: int | None = None
        LOGGER.info("telegram bot started")
        try:
            while not self._stop_event.is_set():
                updates = self._safe_get_updates(offset)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    self._handle_update(update)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)

    def _safe_get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        try:
            return self._api.get_updates(offset=offset, timeout_sec=self._config.poll_timeout_sec)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("getUpdates failed: %s", exc)
            self._stop_event.wait(2.0)
            return []

    def _worker_loop(self) -> None:
        next_queue = time.monotonic()
        next_ci = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_queue:
                self._safe_worker_call(
                    self._orchestrator.process_next_queued,
                    "process_next_queued",
                )
                next_queue = now + max(1, self._config.queue_poll_interval_sec)
            if now >= next_ci:
                self._safe_worker_call(self._orchestrator.sync_waiting_prs, "sync_waiting_prs")
                next_ci = now + max(1, self._config.ci_poll_interval_sec)
            wake_at = min(next_queue, next_ci)
            self._stop_event.wait(max(0.1, wake_at - time.monotonic()))

    @staticmethod
    def _safe_worker_call(fn, name: str) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("worker call %s failed: %s", name, exc)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        text = message.get("text")
        if not isinstance(text, str):
            return
        text = text.strip()
        if not text:
            return

        if not self._is_chat_allowed(chat_id):
            self._safe_send(chat_id, "Этот бот не разрешен для данного чата.")
            return

        response = self._handle_text(chat_id, text)
        self._safe_send(chat_id, response)

    def _is_chat_allowed(self, chat_id: int) -> bool:
        allowed = self._config.allowed_chat_ids
        if not allowed:
            return True
        return chat_id in allowed

    def _handle_text(self, chat_id: int, text: str) -> str:
        if text.startswith("/"):
            return self._handle_command(chat_id, text)
        routed = self._handle_natural_text(chat_id, text)
        if routed is not None:
            return routed
        if len(text) < 3:
            return "Текст слишком короткий. Используй /submit <инструкция>."
        return self._submit_task(chat_id, text)

    def _handle_command(self, chat_id: int, text: str) -> str:
        command, payload = self._split_command(text)
        if command in {"/start", "/help"}:
            return self._help_text()
        if command == "/submit":
            if not payload:
                return "Использование: /submit <инструкция>"
            return self._submit_task(chat_id, payload)
        if command == "/search":
            if not payload:
                return "Использование: /search <запрос>"
            return self._search_text(payload)
        if command == "/status":
            task_id = payload or self._last_task_by_chat.get(chat_id)
            if not task_id:
                return "Не указан task_id. Использование: /status <task_id>"
            return self._status_text(chat_id, task_id)
        if command == "/logs":
            task_id = payload or self._last_task_by_chat.get(chat_id)
            if not task_id:
                return "Не указан task_id. Использование: /logs <task_id>"
            return self._logs_text(chat_id, task_id)
        if command == "/list":
            limit = 10
            if payload:
                try:
                    limit = max(1, min(50, int(payload)))
                except ValueError:
                    return "Лимит должен быть числом. Использование: /list 10"
            return self._list_text(limit)
        if command == "/active":
            return self._active_text()
        if command == "/subagents":
            limit = 20
            if payload:
                try:
                    limit = max(1, min(100, int(payload)))
                except ValueError:
                    return "Лимит должен быть числом. Использование: /subagents 20"
            return self._subagents_text(limit=limit, active_only=False)
        if command == "/rollback":
            if not payload:
                return "Использование: /rollback <task_id>"
            return self._rollback_text(chat_id, payload)
        return "Неизвестная команда. Используй /help."

    @staticmethod
    def _split_command(text: str) -> tuple[str, str]:
        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        payload = parts[1].strip() if len(parts) > 1 else ""
        return command, payload

    def _submit_task(self, chat_id: int, instruction: str) -> str:
        result = self._orchestrator.submit_task(
            instruction=instruction,
            priority="normal",
            idempotency_key=None,
            process_now=False,
        )
        self._last_task_by_chat[chat_id] = result.task_id
        return (
            "Задача поставлена в очередь.\n"
            f"task_id: {result.task_id}\n"
            f"status: {result.status}"
        )

    def _status_text(self, chat_id: int, task_id: str) -> str:
        task = self._orchestrator.get_task(task_id)
        if not task:
            return f"Задача `{task_id}` не найдена."
        self._last_task_by_chat[chat_id] = task_id
        status = task.get("status", "unknown")
        instruction = str(task.get("instruction", ""))[:220]
        lines = [
            f"task_id: {task.get('id')}",
            f"status: {status}",
            f"instruction: {instruction}",
        ]
        if task.get("pr_url"):
            lines.append(f"pr: {task['pr_url']}")
        if task.get("last_error"):
            lines.append(f"error: {task['last_error']}")
        return "\n".join(lines)

    def _logs_text(self, chat_id: int, task_id: str) -> str:
        task = self._orchestrator.get_task(task_id)
        if not task:
            return f"Задача `{task_id}` не найдена."
        self._last_task_by_chat[chat_id] = task_id
        events = task.get("events") or []
        if not events:
            return f"Логи задачи `{task_id}` отсутствуют."
        recent = events[:10]
        lines = [f"Логи {task_id} (последние {len(recent)}):"]
        for event in recent:
            created_at = str(event.get("created_at", ""))
            message = str(event.get("message", "")).replace("\n", " ")
            lines.append(f"- {created_at} | {message[:220]}")
        return "\n".join(lines)

    def _list_text(self, limit: int) -> str:
        tasks = self._orchestrator.list_tasks(limit=limit)
        if not tasks:
            return "Задач пока нет."
        lines = ["Последние задачи:"]
        for task in tasks:
            task_id = str(task.get("id", ""))
            status = str(task.get("status", "unknown"))
            instruction = str(task.get("instruction", "")).replace("\n", " ")
            lines.append(f"- {task_id[:8]} | {status} | {instruction[:120]}")
        return "\n".join(lines)

    def _active_text(self) -> str:
        active = self._orchestrator.list_subagents(limit=20, active_only=True)
        if not active:
            return "Активных subagent-ов сейчас нет."
        lines = ["Активные subagent-ы:"]
        for row in active:
            lines.append(
                "- "
                f"{row.get('id')} | status={row.get('status')} | "
                f"task={row.get('task_id')} | {row.get('activity')}"
            )
        return "\n".join(lines)

    def _subagents_text(self, limit: int, active_only: bool) -> str:
        rows = self._subagent_summary(limit=limit, active_only=active_only)
        if not rows:
            return "Список subagent-ов пуст."
        lines = ["Subagent-ы:"]
        for row in rows:
            lines.append(
                "- "
                f"{row.get('subagent_id')} | status={row.get('status')} | "
                f"task={row.get('task_id')} | task_status={row.get('task_status')} | "
                f"{row.get('activity')}"
            )
        return "\n".join(lines)

    def _rollback_text(self, chat_id: int, task_id: str) -> str:
        task = self._orchestrator.get_task(task_id)
        if not task:
            return f"Задача `{task_id}` не найдена."
        self._last_task_by_chat[chat_id] = task_id
        result = self._orchestrator.rollback_task(task_id)
        return f"Rollback выполнен. Новый статус: {result.get('status', 'unknown')}"

    def _help_text(self) -> str:
        natural_mode = (
            "Естественный язык через Gemini включен."
            if self._gemini_configured
            else "Естественный язык работает как /submit (Gemini не настроен)."
        )
        return (
            "Phoenix Telegram Bot\n"
            "Команды:\n"
            "/submit <инструкция> - создать задачу\n"
            "/search <запрос> - поиск через Kagi\n"
            "/status <task_id> - статус задачи\n"
            "/logs <task_id> - последние логи задачи\n"
            "/list [limit] - список задач\n"
            "/active - активные subagent-ы\n"
            "/subagents [limit] - список всех subagent-ов\n"
            "/rollback <task_id> - ручной rollback\n"
            f"{natural_mode}\n"
            "Можно отправить обычный текст без команды."
        )

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self._api.send_message(chat_id, text=text[:MAX_TELEGRAM_MESSAGE_LEN])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("sendMessage failed for chat %s: %s", chat_id, exc)

    @property
    def _gemini_configured(self) -> bool:
        return bool(self._gemini_chat and getattr(self._gemini_chat, "configured", False))

    @property
    def _kagi_configured(self) -> bool:
        return bool(self._kagi_search and getattr(self._kagi_search, "configured", False))

    def _search_text(self, query: str) -> str:
        if not self._kagi_configured:
            return "Kagi не настроен. Укажи KAGI_API_KEY в .env."
        try:
            hits = self._kagi_search.search(query=query, limit=5)
        except Exception as exc:  # noqa: BLE001
            return f"Ошибка Kagi поиска: {exc}"
        if not hits:
            return "Результаты не найдены."
        lines = ["Результаты поиска:"]
        for hit in hits:
            rank = getattr(hit, "rank", None)
            lines.append(f"{rank if rank is not None else '-'}: {getattr(hit, 'title', '')}")
            lines.append(f"{getattr(hit, 'url', '')}")
            snippet = str(getattr(hit, "snippet", "")).replace("\n", " ").strip()
            if snippet:
                lines.append(snippet[:220])
        notice = str(getattr(self._kagi_search, "last_notice", "")).strip()
        if notice:
            lines.append(f"note: {notice}")
        return "\n".join(lines)

    def _handle_natural_text(self, chat_id: int, text: str) -> str | None:
        if not self._gemini_configured:
            return None

        active_summary = self._subagent_summary(limit=50, active_only=True)
        tracked_task_ids: list[str] = []
        last_task_id = self._last_task_by_chat.get(chat_id)
        if last_task_id:
            tracked_task_ids.append(last_task_id)

        try:
            decision = self._gemini_chat.route_intent(
                user_text=text,
                active_subagents=active_summary,
                tracked_task_ids=tracked_task_ids,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("route_intent failed: %s", exc)
            return f"Ошибка AI-роутинга: {exc}"

        action = str(getattr(decision, "action", "chat")).strip()
        if action == "self_improve":
            instruction = str(getattr(decision, "instruction", "") or text).strip()
            if len(instruction) < 3:
                return "Текст слишком короткий. Используй /submit <инструкция>."
            return self._submit_task(chat_id, instruction)
        if action == "show_active":
            return self._active_text()
        if action == "show_subagents":
            return self._subagents_text(limit=50, active_only=False)
        if action == "show_status":
            task_id = self._pick_task_id(
                explicit_task_id=getattr(decision, "task_id", None),
                chat_id=chat_id,
                active_summary=active_summary,
            )
            if not task_id:
                return "Не вижу активной задачи. Укажи task_id."
            return self._status_text(chat_id, task_id)
        if action == "show_logs":
            task_id = self._pick_task_id(
                explicit_task_id=getattr(decision, "task_id", None),
                chat_id=chat_id,
                active_summary=active_summary,
            )
            if not task_id:
                return "Не вижу активной задачи. Укажи task_id."
            return self._logs_text(chat_id, task_id)
        if action == "list_tasks":
            return self._list_text(limit=20)

        decision_reply = getattr(decision, "reply", None)
        answer = str(decision_reply).strip() if decision_reply is not None else ""
        if not answer:
            answer = self._chat_with_gemini(chat_id, text)
        else:
            self._remember_chat_turn(chat_id, text, answer)

        notice = self._consume_gemini_notice()
        if notice:
            return f"note: {notice}\n{answer}"
        return answer

    def _chat_with_gemini(self, chat_id: int, user_text: str) -> str:
        history = self._chat_history_by_chat.get(chat_id, [])
        try:
            answer = self._gemini_chat.chat(history=history, user_text=user_text)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("gemini chat failed: %s", exc)
            return f"Ошибка Gemini: {exc}"
        self._remember_chat_turn(chat_id, user_text, answer)
        return answer

    def _remember_chat_turn(self, chat_id: int, user_text: str, answer: str) -> None:
        history = self._chat_history_by_chat.setdefault(chat_id, [])
        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": answer})
        if len(history) > 20:
            self._chat_history_by_chat[chat_id] = history[-20:]

    def _consume_gemini_notice(self) -> str:
        if not self._gemini_chat:
            return ""
        notice = str(getattr(self._gemini_chat, "last_notice", "")).strip()
        if notice and hasattr(self._gemini_chat, "last_notice"):
            self._gemini_chat.last_notice = ""
        return notice

    def _subagent_summary(self, limit: int, active_only: bool) -> list[dict[str, object]]:
        rows = self._orchestrator.list_subagents(limit=limit, active_only=active_only)
        active_task_statuses = {"queued", "running", "waiting_ci"}
        result: list[dict[str, object]] = []
        for row in rows:
            task_id = str(row.get("task_id", ""))
            task = self._orchestrator.get_task(task_id) if task_id else None
            task_status = task.get("status") if task else None
            if active_only and task_status not in active_task_statuses:
                continue
            result.append(
                {
                    "subagent_id": row.get("id"),
                    "kind": row.get("kind"),
                    "status": row.get("status"),
                    "activity": row.get("activity"),
                    "task_id": task_id,
                    "task_status": task_status,
                    "updated_at": row.get("updated_at"),
                }
            )
        return result

    def _pick_task_id(
        self,
        explicit_task_id: str | None,
        chat_id: int,
        active_summary: list[dict[str, object]],
    ) -> str | None:
        if explicit_task_id:
            return str(explicit_task_id)
        last = self._last_task_by_chat.get(chat_id)
        if last:
            return last
        for item in active_summary:
            task_id = item.get("task_id")
            if task_id:
                return str(task_id)
        return None


def run_telegram_bot(
    orchestrator: Any,
    token: str,
    queue_poll_interval_sec: int,
    ci_poll_interval_sec: int,
    poll_timeout_sec: int = DEFAULT_POLL_TIMEOUT_SEC,
    allowed_chat_ids: set[int] | None = None,
    gemini_chat_service: Any | None = None,
    kagi_search_service: Any | None = None,
) -> None:
    api = TelegramApiClient(token=token)
    bot = TelegramBot(
        orchestrator=orchestrator,
        api=api,
        config=BotRuntimeConfig(
            poll_timeout_sec=max(1, poll_timeout_sec),
            queue_poll_interval_sec=max(1, queue_poll_interval_sec),
            ci_poll_interval_sec=max(1, ci_poll_interval_sec),
            allowed_chat_ids=allowed_chat_ids,
        ),
        gemini_chat=gemini_chat_service,
        kagi_search=kagi_search_service,
    )
    bot.run_forever()
