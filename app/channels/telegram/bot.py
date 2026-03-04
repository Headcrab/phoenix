from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests

from app.services.gemini_chat import GeminiChatService
from app.services.orchestrator import Orchestrator

LOGGER = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE_LEN = 3900


class TelegramApi(Protocol):
    def get_updates(self, offset: int | None, timeout_sec: int) -> list[dict[str, Any]]:
        ...

    def send_message(self, chat_id: int, text: str) -> None:
        ...


class TelegramApiClient:
    def __init__(self, token: str, request_timeout_sec: int):
        self._token = token
        self._request_timeout_sec = request_timeout_sec
        self._session = requests.Session()
        self._base_url = f"https://api.telegram.org/bot{token}"

    def get_updates(self, offset: int | None, timeout_sec: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_sec,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset
        response = self._session.get(
            f"{self._base_url}/getUpdates",
            params=params,
            timeout=self._request_timeout_sec + timeout_sec,
        )
        payload = self._ensure_ok(response)
        return payload.get("result") or []

    def send_message(self, chat_id: int, text: str) -> None:
        for chunk in _chunk_text(text):
            response = self._session.post(
                f"{self._base_url}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=self._request_timeout_sec,
            )
            self._ensure_ok(response)

    @staticmethod
    def _ensure_ok(response: requests.Response) -> dict[str, Any]:
        if response.status_code >= 300:
            raise RuntimeError(f"Telegram API error {response.status_code}: {response.text}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API rejected request: {payload}")
        return payload


@dataclass(slots=True)
class TelegramBot:
    api_client: TelegramApi
    orchestrator: Orchestrator
    gemini: GeminiChatService
    allowed_chat_ids: set[int] | None = None
    _history: dict[int, list[dict[str, str]]] = field(default_factory=dict, init=False)

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return
        if not self._is_allowed(chat_id):
            self.api_client.send_message(
                chat_id,
                "Доступ запрещен для этого chat_id. Добавьте его в TELEGRAM_ALLOWED_CHAT_IDS.",
            )
            return
        try:
            response = self._dispatch(chat_id=chat_id, text=text)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Failed to handle Telegram update")
            response = f"Ошибка: {exc}"
        if response:
            self.api_client.send_message(chat_id, response)

    def _is_allowed(self, chat_id: int) -> bool:
        if self.allowed_chat_ids is None:
            return True
        return chat_id in self.allowed_chat_ids

    def _dispatch(self, chat_id: int, text: str) -> str:
        if text in {"/start", "/help"}:
            return _help_text()
        if text.startswith("/improve"):
            return self._cmd_improve(text)
        if text.startswith("/status"):
            return self._cmd_status(text)
        if text.startswith("/list"):
            return self._cmd_list(text)
        if text.startswith("/logs"):
            return self._cmd_logs(text)
        if text.startswith("/rollback"):
            return self._cmd_rollback(text)
        if text.startswith("/ask"):
            return self._cmd_ask(chat_id, text.partition(" ")[2].strip())
        if text.startswith("/"):
            return "Неизвестная команда. Используйте /help."
        return self._cmd_ask(chat_id, text)

    def _cmd_improve(self, text: str) -> str:
        instruction = text.partition(" ")[2].strip()
        if not instruction:
            return "Формат: /improve <текст задачи>"
        result = self.orchestrator.submit_task(instruction=instruction, priority="normal")
        return f"Задача поставлена: {result.task_id}\nСтатус: {result.status}"

    def _cmd_status(self, text: str) -> str:
        task_id = text.partition(" ")[2].strip()
        if not task_id:
            return "Формат: /status <task_id>"
        task = self.orchestrator.get_task(task_id)
        if not task:
            return "Задача не найдена."
        return _format_task(task)

    def _cmd_list(self, text: str) -> str:
        raw_limit = text.partition(" ")[2].strip()
        limit = 10
        if raw_limit:
            try:
                limit = max(1, min(int(raw_limit), 30))
            except ValueError:
                return "Формат: /list [limit]"
        tasks = self.orchestrator.list_tasks(limit=limit)
        if not tasks:
            return "Список задач пуст."
        lines = ["Последние задачи:"]
        for task in tasks:
            lines.append(
                f"- {task['id'][:8]} | {task['status']} | {task.get('updated_at', '-')}"
            )
        return "\n".join(lines)

    def _cmd_logs(self, text: str) -> str:
        task_id = text.partition(" ")[2].strip()
        if not task_id:
            return "Формат: /logs <task_id>"
        task = self.orchestrator.get_task(task_id)
        if not task:
            return "Задача не найдена."
        events = task.get("events") or []
        if not events:
            return "Логи отсутствуют."
        lines = [f"Логи для {task_id}:"]
        for event in reversed(events[:10]):
            lines.append(f"- {event.get('created_at', '-')}: {event.get('message', '')}")
        return "\n".join(lines)

    def _cmd_rollback(self, text: str) -> str:
        task_id = text.partition(" ")[2].strip()
        if not task_id:
            return "Формат: /rollback <task_id>"
        task = self.orchestrator.get_task(task_id)
        if not task:
            return "Задача не найдена."
        updated = self.orchestrator.rollback_task(task_id)
        return f"Rollback выполнен.\nTask: {task_id}\nСтатус: {updated.get('status', '-')}"

    def _cmd_ask(self, chat_id: int, user_text: str) -> str:
        prompt = user_text.strip()
        if not prompt:
            return "Формат: /ask <вопрос>"
        if not self.gemini.configured:
            return "Gemini не настроен. Укажите GEMINI_API_KEY и GEMINI_MODEL."
        history = self._history.setdefault(chat_id, [])
        answer = self.gemini.chat(history=history, user_text=prompt)
        history.append({"role": "user", "text": prompt})
        history.append({"role": "assistant", "text": answer})
        if self.gemini.last_notice:
            notice = self.gemini.last_notice
            self.gemini.last_notice = ""
            return f"note> {notice}\n\n{answer}"
        return answer


@dataclass(slots=True)
class TelegramPollingRunner:
    api_client: TelegramApi
    bot: TelegramBot
    poll_timeout_sec: int = 25
    backoff_sec: int = 3

    def run_forever(self) -> None:
        offset: int | None = None
        while True:
            try:
                updates = self.api_client.get_updates(
                    offset=offset,
                    timeout_sec=self.poll_timeout_sec,
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    self.bot.handle_update(update)
            except KeyboardInterrupt:
                LOGGER.info("Telegram polling stopped by user")
                return
            except Exception:  # noqa: BLE001
                LOGGER.exception("Telegram polling failed, retrying after backoff")
                time.sleep(self.backoff_sec)


def _chunk_text(text: str) -> Iterable[str]:
    if len(text) <= MAX_TELEGRAM_MESSAGE_LEN:
        yield text
        return
    remaining = text
    while remaining:
        chunk = remaining[:MAX_TELEGRAM_MESSAGE_LEN]
        split_at = chunk.rfind("\n")
        if split_at > 0:
            chunk = chunk[:split_at]
        yield chunk
        remaining = remaining[len(chunk) :].lstrip("\n")


def _help_text() -> str:
    return (
        "Phoenix Telegram Bot команды:\n"
        "/improve <текст> - создать self-improve задачу\n"
        "/status <task_id> - статус задачи\n"
        "/list [limit] - последние задачи\n"
        "/logs <task_id> - последние события задачи\n"
        "/rollback <task_id> - откатить изменения задачи\n"
        "/ask <вопрос> - запрос в Gemini\n"
        "/help - справка"
    )


def _format_task(task: dict[str, Any]) -> str:
    lines = [
        f"ID: {task.get('id', '-')}",
        f"Статус: {task.get('status', '-')}",
        f"Приоритет: {task.get('priority', '-')}",
        f"Branch: {task.get('branch_name') or '-'}",
        f"PR: {task.get('pr_url') or '-'}",
        f"Ошибка: {task.get('last_error') or '-'}",
        f"Обновлено: {task.get('updated_at', '-')}",
    ]
    return "\n".join(lines)
