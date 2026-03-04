from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


@dataclass(slots=True)
class SearchHit:
    rank: int | None
    title: str
    url: str
    snippet: str


class KagiSearchError(RuntimeError):
    pass


class KagiSearchService:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://kagi.com/api/v0",
        timeout_sec: int = 20,
        session: requests.Session | None = None,
    ):
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._session = session or requests.Session()
        self.last_notice: str = ""

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        if not self.configured:
            raise KagiSearchError("Kagi is not configured. Set KAGI_API_KEY.")
        cleaned_query = query.strip()
        if not cleaned_query:
            raise KagiSearchError("Search query is empty.")
        capped_limit = max(1, min(10, int(limit)))
        self.last_notice = ""

        search_error: KagiSearchError | None = None
        try:
            payload = self._request("/search", {"q": cleaned_query, "limit": capped_limit})
            hits = self._parse_hits(payload, capped_limit)
            if hits:
                return hits
        except KagiSearchError as exc:
            search_error = exc

        payload = self._request("/enrich/web", {"q": cleaned_query})
        hits = self._parse_hits(payload, capped_limit)
        if search_error is not None:
            self.last_notice = (
                "Kagi /search недоступен (закрытая бета), использован /enrich/web."
            )
        if hits:
            return hits
        if search_error:
            raise KagiSearchError(
                f"Kagi returned no results from /search and /enrich/web. {search_error}"
            ) from search_error
        raise KagiSearchError("Kagi returned no results.")

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = self._session.get(
                url,
                headers={
                    "Authorization": f"Bot {self._api_key}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=self._timeout_sec,
            )
        except requests.RequestException as exc:
            raise KagiSearchError(str(exc)) from exc
        if response.status_code >= 300:
            message = self._extract_error_message(response)
            raise KagiSearchError(
                f"Kagi API error {response.status_code} at {path}: {message}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise KagiSearchError(f"Kagi API returned non-JSON payload for {path}.") from exc
        if not isinstance(payload, dict):
            raise KagiSearchError(f"Kagi API returned invalid payload for {path}.")
        return payload

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("error", "message", "detail"):
                    value = payload.get(key)
                    if value:
                        return str(value)
        except ValueError:
            pass
        body = response.text.strip()
        return body[:240] or "unknown error"

    @staticmethod
    def _parse_hits(payload: dict[str, Any], limit: int) -> list[SearchHit]:
        rows = payload.get("data")
        if not isinstance(rows, list):
            return []
        hits: list[SearchHit] = []
        for index, item in enumerate(rows, start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "")).strip()
            title = str(item.get("title", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            if not url or not title:
                continue
            rank_raw = item.get("rank")
            rank = rank_raw if isinstance(rank_raw, int) else index
            hits.append(
                SearchHit(
                    rank=rank,
                    title=title,
                    url=url,
                    snippet=snippet,
                )
            )
            if len(hits) >= limit:
                break
        return hits
