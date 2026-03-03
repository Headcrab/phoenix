from __future__ import annotations

from typing import Any

import requests


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

    def _generate(self, model: str, contents: list[dict[str, Any]]) -> requests.Response:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={self._api_key}"
        )
        return self._session.post(
            url,
            json={
                "contents": contents,
                "generationConfig": {"temperature": 0.7},
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
            "models/gemini-2.5-pro",
            "models/gemini-2.5-flash",
            "models/gemini-2.0-flash",
            "models/gemini-1.5-pro",
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
