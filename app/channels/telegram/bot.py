from __future__ import annotations

import json
import threading
from typing import Any

import requests

from app.bootstrap import get_gemini_chat_service, get_orchestrator, get_settings
from app.services.gemini_chat import IntentDecision


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _chunk_text(text: str, chunk_size: int = 3500) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        chunks.append(text[cursor : cursor + chunk_size])
        cursor += chunk_size
    return chunks


class _TypingPulse:
    def __init__(self, send_typing, interval_sec: float = 4.0):
        self._send_typing = send_typing
        self._interval_sec = interval_sec
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> _TypingPulse:
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._send_typing()
            except Exception:  # noqa: BLE001
                pass
            self._stop_event.wait(self._interval_sec)


class TelegramBot:
    FINAL_STATUSES = {
        "executor_failed",
        "validation_failed",
        "git_failed",
        "restart_failed",
        "rolled_back",
        "completed",
    }

    def __init__(self) -> None:
        self._settings = get_settings()
        self._orchestrator = get_orchestrator()
        self._gemini = get_gemini_chat_service()
        self._token = self._settings.telegram_bot_token
        self._timeout_sec = self._settings.telegram_request_timeout_sec
        self._poll_timeout_sec = self._settings.telegram_poll_timeout_sec
        self._allowed_chat_ids = set(self._settings.telegram_allowed_chat_ids)
        self._session = requests.Session()
        self._stop_event = threading.Event()
        self._histories: dict[int, list[dict[str, str]]] = {}
        self._tracked_task_ids: dict[int, set[str]] = {}
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)

    @property
    def configured(self) -> bool:
        return bool(self._token and self._gemini.configured)

    def run(self) -> int:
        if not self._token:
            print("TELEGRAM_BOT_TOKEN не задан. Добавь его в .env.")
            return 1
        if not self._gemini.configured:
            print("Gemini не настроен. Укажи GEMINI_API_KEY и GEMINI_MODEL в .env.")
            return 1
        me = self._api("getMe")
        if not bool(me.get("ok")):
            print(f"Telegram API getMe failed: {me}")
            return 1
        username = (me.get("result") or {}).get("username", "unknown")
        print(f"Telegram bot started: @{username}")
        self._worker_thread.start()
        offset = 0
        try:
            while not self._stop_event.is_set():
                payload: dict[str, Any] = {
                    "timeout": self._poll_timeout_sec,
                    "offset": offset,
                    "allowed_updates": ["message"],
                }
                updates = self._api("getUpdates", payload)
                if not bool(updates.get("ok")):
                    continue
                for item in updates.get("result") or []:
                    update_id = int(item.get("update_id", 0))
                    if update_id >= offset:
                        offset = update_id + 1
                    self._handle_update(item)
        except KeyboardInterrupt:
            return 0
        finally:
            self._stop_event.set()
            self._worker_thread.join(timeout=1.0)
        return 0

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._orchestrator.process_next_queued()
                self._orchestrator.sync_waiting_prs()
            except Exception:  # noqa: BLE001
                pass
            self._stop_event.wait(1.5)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if not text:
            return
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        if chat_id == 0:
            return
        if self._allowed_chat_ids and chat_id not in self._allowed_chat_ids:
            self._send_message(chat_id, "Этот чат не разрешен для управления Phoenix.")
            return
        if text in {"/start", "/help"}:
            self._send_message(chat_id, self._help_text())
            return
        self._handle_text(chat_id, text)

    @staticmethod
    def _help_text() -> str:
        return (
            "Phoenix Telegram bot.\n"
            "Пиши свободно, агент сам выберет действие.\n"
            "Команды:\n"
            "/help - показать помощь\n"
            "/active - активные задачи"
        )

    def _handle_text(self, chat_id: int, text: str) -> None:
        if text == "/active":
            summary = self._subagent_summary(active_only=True)
            if summary:
                self._send_message(chat_id, _json(summary))
            else:
                self._send_message(chat_id, "Сейчас активных задач нет.")
            return

        with _TypingPulse(lambda: self._send_typing(chat_id)):
            reply = self._build_reply(chat_id, text)
        self._send_message(chat_id, reply)

    def _build_reply(self, chat_id: int, text: str) -> str:
        history = self._histories.setdefault(chat_id, [])
        tracked = sorted(self._tracked_task_ids.get(chat_id, set()))
        active_summary = self._subagent_summary(active_only=True)
        decision = self._gemini.route_intent(
            user_text=text,
            active_subagents=active_summary,
            tracked_task_ids=tracked,
        )
        notices: list[str] = []
        if self._gemini.last_notice:
            notices.append(f"note> {self._gemini.last_notice}")
            self._gemini.last_notice = ""

        if decision.action == "self_improve":
            instruction = (decision.instruction or text).strip()
            result = self._orchestrator.submit_task(
                instruction=instruction,
                priority="normal",
                process_now=False,
            )
            self._tracked_task_ids.setdefault(chat_id, set()).add(result.task_id)
            notices.append(
                "Принял. Поставил задачу в очередь: "
                f"task_id={result.task_id}, статус={result.status}."
            )
            return "\n".join(notices)

        if decision.action == "show_active":
            if active_summary:
                notices.append(_json(active_summary))
            else:
                notices.append("Сейчас активных задач нет.")
            return "\n".join(notices)

        if decision.action == "show_subagents":
            notices.append(_json(self._subagent_summary(active_only=False)))
            return "\n".join(notices)

        if decision.action in {"show_status", "show_logs"}:
            task_id = self._pick_task_id(decision, active_summary, tracked)
            if not task_id:
                notices.append("Не вижу активной задачи. Уточни task_id.")
                return "\n".join(notices)
            task = self._orchestrator.get_task(task_id)
            if not task:
                notices.append(f"Задача `{task_id}` не найдена.")
                return "\n".join(notices)
            if decision.action == "show_status":
                notices.append(_json(task))
            else:
                notices.append(_json(task.get("events", [])))
            return "\n".join(notices)

        if decision.action == "list_tasks":
            notices.append(_json(self._orchestrator.list_tasks(limit=20)))
            return "\n".join(notices)

        answer = decision.reply
        if not answer:
            answer = self._gemini.chat(history=history, user_text=text)
        history.append({"role": "user", "text": text})
        history.append({"role": "assistant", "text": answer})
        notices.append(answer)
        return "\n".join(notices)

    @staticmethod
    def _pick_task_id(
        decision: IntentDecision,
        active_summary: list[dict[str, object]],
        tracked_task_ids: list[str],
    ) -> str | None:
        if decision.task_id:
            return decision.task_id
        for item in active_summary:
            task_id = item.get("task_id")
            if task_id:
                return str(task_id)
        if tracked_task_ids:
            return tracked_task_ids[-1]
        return None

    def _subagent_summary(self, active_only: bool) -> list[dict[str, object]]:
        rows = self._orchestrator.list_subagents(limit=100, active_only=active_only)
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

    def _send_typing(self, chat_id: int) -> None:
        payload = {"chat_id": chat_id, "action": "typing"}
        self._api("sendChatAction", payload)

    def _send_message(self, chat_id: int, text: str) -> None:
        if not text.strip():
            text = "ok"
        for chunk in _chunk_text(text):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            self._api("sendMessage", payload)

    def _api(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            response = self._session.post(
                url,
                json=payload or {},
                timeout=self._timeout_sec + self._poll_timeout_sec,
            )
        except requests.RequestException:
            return {"ok": False}
        if response.status_code >= 300:
            return {"ok": False, "status_code": response.status_code, "text": response.text}
        try:
            return response.json()
        except ValueError:
            return {"ok": False, "text": response.text}
