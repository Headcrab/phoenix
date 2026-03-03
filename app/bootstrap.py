from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.repository import TaskRepository
from app.services.codex_executor import CodexExecutor
from app.services.gemini_chat import GeminiChatService
from app.services.gitops import GitOps
from app.services.lifecycle import LifecycleManager
from app.services.orchestrator import Orchestrator
from app.services.validator import Validator


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_repository() -> TaskRepository:
    settings = get_settings()
    return TaskRepository(settings.db_path)


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    settings = get_settings()
    configure_logging()
    return Orchestrator(
        settings=settings,
        repository=get_repository(),
        executor=CodexExecutor(
            repo_path=settings.repo_path,
            executor_cmd=settings.executor_cmd,
            timeout_sec=settings.executor_timeout_sec,
        ),
        validator=Validator(
            repo_path=settings.repo_path,
            timeout_sec=settings.quality_gate_timeout_sec,
        ),
        gitops=GitOps(settings=settings),
        lifecycle=LifecycleManager(settings=settings),
    )


@lru_cache(maxsize=1)
def get_gemini_chat_service() -> GeminiChatService:
    settings = get_settings()
    return GeminiChatService(
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        timeout_sec=settings.gemini_timeout_sec,
    )
