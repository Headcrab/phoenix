from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from app.bootstrap import get_orchestrator


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def cmd_submit(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    result = orchestrator.submit_task(
        instruction=args.text,
        priority=args.priority,
        idempotency_key=args.idempotency_key,
        process_now=args.process_now,
    )
    _print_json({"task_id": result.task_id, "status": result.status})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    task = orchestrator.get_task(args.task_id)
    if not task:
        print("Task not found", file=sys.stderr)
        return 1
    _print_json(task)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    tasks = orchestrator.list_tasks(status=args.status, limit=args.limit)
    _print_json(tasks)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    task = orchestrator.get_task(args.task_id)
    if not task:
        print("Task not found", file=sys.stderr)
        return 1
    _print_json(task.get("events", []))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    task = orchestrator.rollback_task(args.task_id)
    _print_json(task)
    return 0


def cmd_worker_once(_: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    orchestrator.process_next_queued()
    orchestrator.sync_waiting_prs()
    print("Worker cycle complete")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phoenix", description="Phoenix self-improving agent")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="Create a self-improve task")
    submit.add_argument("--text", required=True, help="Instruction text")
    submit.add_argument("--priority", default="normal", choices=["low", "normal", "high"])
    submit.add_argument("--idempotency-key")
    submit.add_argument("--process-now", action=argparse.BooleanOptionalAction, default=None)
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status", help="Get task details")
    status.add_argument("--task-id", required=True)
    status.set_defaults(func=cmd_status)

    listing = sub.add_parser("list", help="List tasks")
    listing.add_argument("--status")
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    logs = sub.add_parser("logs", help="Get task logs")
    logs.add_argument("--task-id", required=True)
    logs.set_defaults(func=cmd_logs)

    rollback = sub.add_parser("rollback", help="Rollback latest merged change")
    rollback.add_argument("--task-id", required=True)
    rollback.set_defaults(func=cmd_rollback)

    worker = sub.add_parser("worker-once", help="Run one worker iteration")
    worker.set_defaults(func=cmd_worker_once)

    serve = sub.add_parser("serve", help="Run API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

