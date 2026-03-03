from pathlib import Path

from app.db.repository import TaskRepository


def test_repository_idempotency(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "phoenix.db")
    first = repo.create_task("do a", "normal", "k1")
    second = repo.create_task("do b", "high", "k1")
    assert first["id"] == second["id"]
    assert second["instruction"] == "do a"


def test_repository_events(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "phoenix.db")
    task = repo.create_task("do x", "normal", None)
    repo.append_event(task["id"], "event-1")
    events = repo.get_events(task["id"])
    assert events
    assert events[0]["task_id"] == task["id"]

