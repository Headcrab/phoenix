from __future__ import annotations

import argparse
import json
import sys
import threading

import uvicorn

from app.bootstrap import get_gemini_chat_service, get_orchestrator, get_settings


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_text)


class ChatTaskRuntime:
    FINAL_STATUSES = {
        "executor_failed",
        "validation_failed",
        "git_failed",
        "restart_failed",
        "rolled_back",
        "completed",
    }

    def __init__(self, orchestrator):
        self._orchestrator = orchestrator
        self._stop_event = threading.Event()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._tracked_tasks: set[str] = set()
        self._last_event_id: dict[str, int] = {}
        self._last_status: dict[str, str] = {}
        self._last_progress: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self._worker_thread.start()
        self._watch_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._worker_thread.join(timeout=1.0)
        self._watch_thread.join(timeout=1.0)

    def track(self, task_id: str) -> None:
        with self._lock:
            self._tracked_tasks.add(task_id)

    def list_tracked(self) -> list[str]:
        with self._lock:
            return sorted(self._tracked_tasks)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._orchestrator.process_next_queued()
                self._orchestrator.sync_waiting_prs()
            except Exception as exc:  # noqa: BLE001
                _safe_print(f"sys> ошибка worker: {exc}")
            self._stop_event.wait(1.5)

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                tracked = list(self._tracked_tasks)

            for task_id in tracked:
                task = self._orchestrator.get_task(task_id)
                if not task:
                    continue

                status = str(task.get("status", "unknown"))
                if status != self._last_status.get(task_id):
                    self._emit_progress(
                        task_id,
                        self._status_progress(status),
                        self._status_label(status),
                    )
                    self._last_status[task_id] = status

                last_seen = self._last_event_id.get(task_id, 0)
                events = task.get("events") or []
                new_events = [ev for ev in events if int(ev.get("id", 0)) > last_seen]
                for ev in sorted(new_events, key=lambda x: int(x.get("id", 0))):
                    ev_id = int(ev.get("id", 0))
                    message = str(ev.get("message", ""))
                    progress = self._milestone_progress(message)
                    if progress is not None:
                        self._emit_progress(task_id, progress, self._milestone_label(message))
                    elif self._needs_user_input(message):
                        question = message.removeprefix("codex>").strip()
                        _safe_print(f"task[{task_id}] нужен ваш ответ: {question}")
                    if ev_id > last_seen:
                        last_seen = ev_id
                self._last_event_id[task_id] = last_seen

                if status in self.FINAL_STATUSES:
                    with self._lock:
                        self._tracked_tasks.discard(task_id)

            self._stop_event.wait(1.0)

    def _emit_progress(self, task_id: str, progress: int, text: str) -> None:
        previous = self._last_progress.get(task_id, -1)
        if progress <= previous:
            return
        self._last_progress[task_id] = progress
        _safe_print(f"task[{task_id}] {progress}% - {text}")

    @staticmethod
    def _status_progress(status: str) -> int:
        mapping = {
            "queued": 5,
            "running": 15,
            "waiting_ci": 80,
            "completed": 100,
            "executor_failed": 100,
            "validation_failed": 100,
            "git_failed": 100,
            "restart_failed": 100,
            "rolled_back": 100,
        }
        return mapping.get(status, 0)

    @staticmethod
    def _status_label(status: str) -> str:
        mapping = {
            "queued": "задача в очереди",
            "running": "агент выполняет задачу",
            "waiting_ci": "ожидание CI/merge",
            "completed": "задача завершена успешно",
            "executor_failed": "ошибка на этапе Codex",
            "validation_failed": "не прошли проверки",
            "git_failed": "ошибка git/PR этапа",
            "restart_failed": "ошибка перезапуска",
            "rolled_back": "выполнен откат",
        }
        return mapping.get(status, status)

    @staticmethod
    def _milestone_progress(message: str) -> int | None:
        if message.startswith("Starting executor"):
            return 20
        if message.startswith("Executor: Executor finished successfully"):
            return 45
        if message.startswith("Validation report:"):
            return 60
        if message.startswith("Using branch"):
            return 65
        if message.startswith("Committed "):
            return 72
        if message.startswith("Pushed branch"):
            return 76
        if message.startswith("PR created:"):
            return 85
        if message == "Task completed successfully":
            return 100
        if "rolling back" in message.lower():
            return 90
        return None

    @staticmethod
    def _milestone_label(message: str) -> str:
        if message.startswith("Starting executor"):
            return "запускаю Codex"
        if message.startswith("Executor: Executor finished successfully"):
            return "Codex завершил генерацию изменений"
        if message.startswith("Validation report:"):
            return "проверка lint/tests/health"
        if message.startswith("Using branch"):
            return "подготовлена рабочая ветка"
        if message.startswith("Committed "):
            return "изменения закоммичены"
        if message.startswith("Pushed branch"):
            return "ветка отправлена в remote"
        if message.startswith("PR created:"):
            return "PR создан, ожидание CI"
        if message == "Task completed successfully":
            return "задача завершена"
        if "rolling back" in message.lower():
            return "выполняется откат"
        return "выполняется задача"

    @staticmethod
    def _needs_user_input(message: str) -> bool:
        if not message.startswith("codex>"):
            return False
        lowered = message.lower()
        markers = ["?", "need input", "please provide", "уточни", "нужно уточнить", "choose"]
        return any(marker in lowered for marker in markers)


def _subagent_summary(
    orchestrator,
    limit: int = 50,
    active_only: bool = True,
) -> list[dict[str, object]]:
    rows = orchestrator.list_subagents(limit=limit, active_only=active_only)
    active_task_statuses = {"queued", "running", "waiting_ci"}
    result: list[dict[str, object]] = []
    for row in rows:
        task_id = str(row.get("task_id", ""))
        task = orchestrator.get_task(task_id) if task_id else None
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


def cmd_active(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    summary = _subagent_summary(orchestrator=orchestrator, limit=args.limit, active_only=True)
    _print_json(summary)
    return 0


def cmd_subagents(args: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    summary = _subagent_summary(
        orchestrator=orchestrator,
        limit=args.limit,
        active_only=not args.all,
    )
    _print_json(summary)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
    return 0


def _print_chat_help() -> None:
    print("Чат работает на естественном языке.")
    print("Служебные команды:")
    print("  /help  - показать это сообщение")
    print("  /exit  - выйти из чата")


def _pick_task_id(
    explicit_task_id: str | None,
    active_summary: list[dict[str, object]],
    tracked_task_ids: list[str],
) -> str | None:
    if explicit_task_id:
        return explicit_task_id
    for item in active_summary:
        task_id = item.get("task_id")
        if task_id:
            return str(task_id)
    if tracked_task_ids:
        return tracked_task_ids[-1]
    return None


def cmd_chat(_: argparse.Namespace) -> int:
    orchestrator = get_orchestrator()
    gemini = get_gemini_chat_service()
    if not gemini.configured:
        print("Gemini не настроен. Укажи GEMINI_API_KEY и GEMINI_MODEL в .env.", file=sys.stderr)
        return 1
    history: list[dict[str, str]] = []
    runtime = ChatTaskRuntime(orchestrator)
    runtime.start()
    print("Phoenix Chat (Gemini). Пиши свободно, агент сам решит действие.")
    print("Служебные: /help, /exit")
    try:
        while True:
            try:
                user_input = input("you> ").strip()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print()
                return 0
            if not user_input:
                continue
            if user_input in {"/exit", "/quit"}:
                return 0
            if user_input == "/help":
                _print_chat_help()
                continue

            active_summary = _subagent_summary(
                orchestrator=orchestrator,
                limit=50,
                active_only=True,
            )
            tracked_task_ids = runtime.list_tracked()
            try:
                decision = gemini.route_intent(
                    user_text=user_input,
                    active_subagents=active_summary,
                    tracked_task_ids=tracked_task_ids,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"Gemini error: {exc}", file=sys.stderr)
                continue
            if gemini.last_notice:
                _safe_print(f"note> {gemini.last_notice}")
                gemini.last_notice = ""

            if decision.action == "self_improve":
                instruction = (decision.instruction or user_input).strip()
                result = orchestrator.submit_task(
                    instruction=instruction,
                    priority="normal",
                    process_now=False,
                )
                runtime.track(result.task_id)
                _safe_print(
                    "ai> Принял. Поставил задачу в очередь: "
                    f"task_id={result.task_id}, статус={result.status}."
                )
                continue

            if decision.action == "show_active":
                if active_summary:
                    _print_json(active_summary)
                else:
                    _safe_print("ai> Сейчас агент свободен, активных задач нет.")
                continue

            if decision.action == "show_subagents":
                _print_json(
                    _subagent_summary(
                        orchestrator=orchestrator,
                        limit=100,
                        active_only=False,
                    )
                )
                continue

            if decision.action in {"show_status", "show_logs"}:
                task_id = _pick_task_id(decision.task_id, active_summary, tracked_task_ids)
                if not task_id:
                    _safe_print("ai> Не вижу активной задачи. Уточни `task_id`.")
                    continue
                task = orchestrator.get_task(task_id)
                if not task:
                    _safe_print(f"ai> Задача `{task_id}` не найдена.")
                    continue
                if decision.action == "show_status":
                    _print_json(task)
                else:
                    _print_json(task.get("events", []))
                continue

            if decision.action == "list_tasks":
                _print_json(orchestrator.list_tasks(limit=20))
                continue

            answer = decision.reply
            if not answer:
                try:
                    answer = gemini.chat(history=history, user_text=user_input)
                except Exception as exc:  # noqa: BLE001
                    print(f"Gemini error: {exc}", file=sys.stderr)
                    continue
            _safe_print(f"ai> {answer}")
            history.append({"role": "user", "text": user_input})
            history.append({"role": "assistant", "text": answer})
    finally:
        runtime.stop()


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

    active = sub.add_parser("active", help="Show currently active subagents")
    active.add_argument("--limit", type=int, default=50)
    active.set_defaults(func=cmd_active)

    subagents = sub.add_parser("subagents", help="Show subagent registry")
    subagents.add_argument("--limit", type=int, default=100)
    subagents.add_argument("--all", action="store_true")
    subagents.set_defaults(func=cmd_subagents)

    serve = sub.add_parser("serve", help="Run API server")
    settings = get_settings()
    serve.add_argument("--host", default=settings.api_host)
    serve.add_argument("--port", type=int, default=settings.api_port)
    serve.set_defaults(func=cmd_serve)

    chat = sub.add_parser("chat", help="Interactive chat via Gemini")
    chat.set_defaults(func=cmd_chat)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
