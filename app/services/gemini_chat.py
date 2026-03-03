from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(slots=True)
class IntentDecision:
    action: str
    instruction: str | None = None
    task_id: str | None = None
    reply: str | None = None


class GeminiChatService:
    def __init__(self, api_key: str, model: str, timeout_sec: int):
        self._api_key = api_key
        self._model = model
        self._timeout_sec = timeout_sec
        self._session = requests.Session()
        self.last_notice: str = ""

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._model)

    def chat(self, history: list[dict[str, str]], user_text: str) -> str:
        if not self.configured:
            raise RuntimeError("Gemini is not configured. Set GEMINI_API_KEY and GEMINI_MODEL.")
        payload_history = list(history)
        payload_history.append({"role": "user", "text": user_text})
        contents = [self._map_message(x) for x in payload_history]
        resp = self._generate(self._model, contents)
        if resp.status_code == 404:
            fallback = self._pick_fallback_model()
            if fallback:
                self.last_notice = (
                    f"Model '{self._model}' недоступен, использую '{fallback}' для текущей сессии."
                )
                resp = self._generate(fallback, contents)
        if resp.status_code >= 300:
            raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text}")
        data = resp.json()
        text = self._extract_text(data)
        if not text:
            raise RuntimeError("Gemini returned empty response")
        return text

    def route_intent(
        self,
        user_text: str,
        active_subagents: list[dict[str, object]],
        tracked_task_ids: list[str],
    ) -> IntentDecision:
        if not self.configured:
            raise RuntimeError("Gemini is not configured. Set GEMINI_API_KEY and GEMINI_MODEL.")

        action_schema = {
            "allowed_actions": [
                "chat",
                "self_improve",
                "show_active",
                "show_subagents",
                "show_status",
                "show_logs",
                "list_tasks",
            ]
        }
        context = {
            "active_subagents": active_subagents,
            "tracked_task_ids": tracked_task_ids,
            "user_text": user_text,
        }
        router_prompt = (
            "Ты роутер намерений для CLI-агента.\n"
            "Выбери ОДНО действие и верни ТОЛЬКО JSON-объект.\n"
            f"Схема: {json.dumps(action_schema, ensure_ascii=False)}\n"
            "Поля JSON: action, instruction, task_id, reply.\n"
            "Правила:\n"
            "1) Если пользователь просит изменить/добавить/исправить "
            "агент или код -> self_improve.\n"
            "2) Если спрашивает чем агент занят сейчас -> show_active.\n"
            "3) Если просит показать субагентов -> show_subagents.\n"
            "4) Если просит статус задачи -> show_status.\n"
            "5) Если просит логи задачи -> show_logs.\n"
            "6) Если просит список задач -> list_tasks.\n"
            "7) Иначе -> chat с ответом в reply.\n"
            f"Контекст: {json.dumps(context, ensure_ascii=False)}"
        )

        contents = [{"role": "user", "parts": [{"text": router_prompt}]}]
        resp = self._generate(self._model, contents)
        if resp.status_code == 404:
            fallback = self._pick_fallback_model()
            if fallback:
                self.last_notice = (
                    f"Model '{self._model}' недоступен, использую '{fallback}' для текущей сессии."
                )
                resp = self._generate(fallback, contents)
        if resp.status_code >= 300:
            raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text}")

        text = self._extract_text(resp.json())
        parsed = self._parse_json_object(text)
        action = str(parsed.get("action", "chat")).strip()
        if action not in action_schema["allowed_actions"]:
            action = "chat"
        return IntentDecision(
            action=action,
            instruction=self._as_optional_str(parsed.get("instruction")),
            task_id=self._as_optional_str(parsed.get("task_id")),
            reply=self._as_optional_str(parsed.get("reply")),
        )

    def summarize_task_result(self, task: dict[str, Any]) -> str:
        if not self.configured:
            return (
                f"Задача {task.get('id')} завершена со статусом {task.get('status')}."
            )
        payload = {
            "task_id": task.get("id"),
            "status": task.get("status"),
            "last_error": task.get("last_error"),
            "branch_name": task.get("branch_name"),
            "commit_sha": task.get("commit_sha"),
            "pr_url": task.get("pr_url"),
            "events_tail": [
                e.get("message")
                for e in (task.get("events") or [])[:10]
            ],
        }
        prompt = (
            "Ты главный агент. Коротко (1-3 предложения, русский язык) сообщи пользователю "
            "итог задачи. Без технического шума, только суть и что делать дальше при ошибке.\n"
            f"Данные: {json.dumps(payload, ensure_ascii=False)}"
        )
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        resp = self._generate(self._model, contents)
        if resp.status_code == 404:
            fallback = self._pick_fallback_model()
            if fallback:
                self.last_notice = (
                    f"Model '{self._model}' недоступен, использую '{fallback}' для текущей сессии."
                )
                resp = self._generate(fallback, contents)
        if resp.status_code >= 300:
            return (
                f"Задача {task.get('id')} завершена со статусом {task.get('status')}. "
                f"Не удалось сформировать пояснение: Gemini API {resp.status_code}."
            )
        text = self._extract_text(resp.json())
        if not text:
            return f"Задача {task.get('id')} завершена со статусом {task.get('status')}."
        return text

    def _generate(self, model: str, contents: list[dict[str, Any]]) -> requests.Response:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self._api_key}"
        )
        return self._session.post(
            url,
            json={
                "contents": contents,
                "generationConfig": {"temperature": 0.2},
            },
            timeout=self._timeout_sec,
        )

    def _pick_fallback_model(self) -> str | None:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self._api_key}"
        resp = self._session.get(url, timeout=self._timeout_sec)
        if resp.status_code >= 300:
            return None
        models = resp.json().get("models") or []
        available = [
            x.get("name", "")
            for x in models
            if "generateContent" in (x.get("supportedGenerationMethods") or [])
        ]
        preferred = [
            "models/gemini-3-flash-preview",
            "models/gemini-3.1-flash-lite-preview",
        ]
        for name in preferred:
            if name in available:
                return name.removeprefix("models/")
        for name in available:
            if "gemini" in name:
                return name.removeprefix("models/")
        return None

    @staticmethod
    def _map_message(message: dict[str, str]) -> dict[str, Any]:
        role = message.get("role", "user")
        mapped_role = "model" if role == "assistant" else "user"
        return {
            "role": mapped_role,
            "parts": [{"text": message.get("text", "")}],
        }

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            return ""
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        texts: list[str] = []
        for part in parts:
            text = part.get("text")
            if text:
                texts.append(text)
        return "\n".join(texts).strip()

    @staticmethod
    def _as_optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        stripped = text.strip()
        try:
            value = json.loads(stripped)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", stripped)
        if match:
            candidate = match.group(0)
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
        return {"action": "chat", "reply": stripped}
