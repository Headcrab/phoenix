from __future__ import annotations

from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI

from app.api.routes_tasks import router as tasks_router
from app.bootstrap import get_orchestrator, get_settings

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    orchestrator = get_orchestrator()
    scheduler.add_job(
        orchestrator.process_next_queued,
        "interval",
        seconds=settings.queue_poll_interval_sec,
    )
    scheduler.add_job(
        orchestrator.sync_waiting_prs,
        "interval",
        seconds=settings.ci_poll_interval_sec,
    )
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Phoenix Agent", version="0.1.0", lifespan=lifespan)
app.include_router(tasks_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "phoenix-agent"}
