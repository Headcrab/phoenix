from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
            continue
        key, value = cleaned.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _read_int_set(name: str) -> set[int] | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    result: set[int] = set()
    for chunk in value.split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        result.add(int(cleaned))
    return result or None


@dataclass(slots=True)
class Settings:
    repo_path: Path
    db_path: Path
    main_branch: str
    remote_name: str
    executor_cmd: str
    executor_timeout_sec: int
    quality_gate_timeout_sec: int
    auto_process_on_submit: bool
    auto_merge: bool
    ci_poll_interval_sec: int
    queue_poll_interval_sec: int
    service_name: str
    healthcheck_url: str
    api_host: str
    api_port: int
    github_owner: str
    github_repo: str
    github_token: str
    gemini_api_key: str
    gemini_model: str
    gemini_timeout_sec: int
    kagi_api_key: str
    kagi_api_base_url: str
    kagi_timeout_sec: int
    telegram_bot_token: str
    telegram_allowed_chat_ids: set[int] | None
    telegram_poll_timeout_sec: int
    telegram_queue_poll_interval_sec: int
    telegram_ci_poll_interval_sec: int

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(Path(".env").resolve())
        repo_path = Path(os.getenv("PHOENIX_REPO_PATH", ".")).resolve()
        db_path = Path(os.getenv("PHOENIX_DB_PATH", ".phoenix/phoenix.db")).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        ci_poll_interval_sec = _read_int("PHOENIX_CI_POLL_INTERVAL_SEC", 30)
        queue_poll_interval_sec = _read_int("PHOENIX_QUEUE_POLL_INTERVAL_SEC", 20)
        return cls(
            repo_path=repo_path,
            db_path=db_path,
            main_branch=os.getenv("PHOENIX_MAIN_BRANCH", "main"),
            remote_name=os.getenv("PHOENIX_REMOTE_NAME", "origin"),
            executor_cmd=os.getenv("PHOENIX_EXECUTOR_CMD", "").strip(),
            executor_timeout_sec=_read_int("PHOENIX_EXECUTOR_TIMEOUT_SEC", 1800),
            quality_gate_timeout_sec=_read_int("PHOENIX_QUALITY_GATE_TIMEOUT_SEC", 1200),
            auto_process_on_submit=_read_bool("PHOENIX_AUTO_PROCESS_ON_SUBMIT", True),
            auto_merge=_read_bool("PHOENIX_AUTO_MERGE", True),
            ci_poll_interval_sec=ci_poll_interval_sec,
            queue_poll_interval_sec=queue_poll_interval_sec,
            service_name=os.getenv("PHOENIX_SERVICE_NAME", "PhoenixAgent"),
            healthcheck_url=os.getenv("PHOENIX_HEALTHCHECK_URL", "http://127.0.0.1:8666/health"),
            api_host=os.getenv("PHOENIX_API_HOST", "127.0.0.1"),
            api_port=_read_int("PHOENIX_API_PORT", 8666),
            github_owner=os.getenv("GITHUB_OWNER", "").strip(),
            github_repo=os.getenv("GITHUB_REPO", "").strip(),
            github_token=os.getenv("GITHUB_TOKEN", "").strip(),
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview").strip(),
            gemini_timeout_sec=_read_int("GEMINI_TIMEOUT_SEC", 60),
            kagi_api_key=os.getenv("KAGI_API_KEY", "").strip(),
            kagi_api_base_url=os.getenv("KAGI_API_BASE_URL", "https://kagi.com/api/v0").strip(),
            kagi_timeout_sec=_read_int("KAGI_TIMEOUT_SEC", 20),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_allowed_chat_ids=_read_int_set("TELEGRAM_ALLOWED_CHAT_IDS"),
            telegram_poll_timeout_sec=_read_int("PHOENIX_TELEGRAM_POLL_TIMEOUT_SEC", 25),
            telegram_queue_poll_interval_sec=_read_int(
                "PHOENIX_TELEGRAM_QUEUE_POLL_INTERVAL_SEC",
                queue_poll_interval_sec,
            ),
            telegram_ci_poll_interval_sec=_read_int(
                "PHOENIX_TELEGRAM_CI_POLL_INTERVAL_SEC",
                ci_poll_interval_sec,
            ),
        )

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_owner and self.github_repo and self.github_token)
