from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.bootstrap import get_orchestrator

router = APIRouter()


class SubmitTaskRequest(BaseModel):
    instruction: str = Field(min_length=3)
    priority: str = Field(default="normal")
    idempotency_key: str | None = None
    process_now: bool | None = None


@router.post("/tasks/self-improve")
def submit_task(payload: SubmitTaskRequest) -> dict[str, str]:
    orchestrator = get_orchestrator()
    result = orchestrator.submit_task(
        instruction=payload.instruction,
        priority=payload.priority,
        idempotency_key=payload.idempotency_key,
        process_now=payload.process_now,
    )
    return {"task_id": result.task_id, "status": result.status}


@router.get("/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    orchestrator = get_orchestrator()
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/tasks")
def list_tasks(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    orchestrator = get_orchestrator()
    return orchestrator.list_tasks(status=status, limit=limit)


@router.post("/tasks/{task_id}/rollback")
def rollback_task(task_id: str) -> dict:
    orchestrator = get_orchestrator()
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return orchestrator.rollback_task(task_id)

