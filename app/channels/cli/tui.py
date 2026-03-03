from __future__ import annotations

import json
import sys
import threading
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from app.bootstrap import get_gemini_chat_service, get_orchestrator


class TaskRuntime:
    FINAL_STATUSES = {
        "executor_failed",
        "validation_failed",
        "git_failed",
        "restart_failed",
        "rolled_back",
        "completed",
    }

    def __init__(
        self,
        orchestrator,
        on_progress,
        on_need_input,
        on_task_final,
    ):
        self._orchestrator = orchestrator
        self._on_progress = on_progress
        self._on_need_input = on_need_input
        self._on_task_final = on_task_final
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
                self._on_progress("sys", 0, f"ошибка worker: {exc}")
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
                        self._on_need_input(task_id, question)
                    if ev_id > last_seen:
                        last_seen = ev_id
                self._last_event_id[task_id] = last_seen

                if status in self.FINAL_STATUSES:
                    self._on_task_final(task)
                    with self._lock:
                        self._tracked_tasks.discard(task_id)

            self._stop_event.wait(1.0)

    def _emit_progress(self, task_id: str, progress: int, text: str) -> None:
        previous = self._last_progress.get(task_id, -1)
        if progress <= previous:
            return
        self._last_progress[task_id] = progress
        self._on_progress(task_id, progress, text)

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
            "completed": "задача завершена",
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
            return "подготовлена ветка"
        if message.startswith("Committed "):
            return "изменения закоммичены"
        if message.startswith("Pushed branch"):
            return "ветка отправлена в remote"
        if message.startswith("PR created:"):
            return "PR создан, ждем CI"
        if message == "Task completed successfully":
            return "задача завершена"
        return "выполняется задача"

    @staticmethod
    def _needs_user_input(message: str) -> bool:
        if not message.startswith("codex>"):
            return False
        lowered = message.lower()
        markers = ["?", "need input", "please provide", "уточни", "нужно уточнить", "choose"]
        return any(marker in lowered for marker in markers)


class PhoenixTui:
    def __init__(self):
        self._orchestrator = get_orchestrator()
        self._gemini = get_gemini_chat_service()
        self._history: list[dict[str, str]] = []
        self._tasks: list[dict[str, Any]] = []
        self._selected = 0
        self._expanded: set[str] = set()
        self._chat_lines: list[str] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        self._tasks_control = FormattedTextControl(self._render_tasks)
        self._chat_control = FormattedTextControl(self._render_chat)
        self._input = TextArea(
            height=1,
            prompt="you> ",
            multiline=False,
            wrap_lines=False,
            accept_handler=self._on_submit,
        )
        self._kb = self._build_keybindings()
        self._app = Application(
            layout=self._build_layout(),
            key_bindings=self._kb,
            full_screen=True,
            style=Style.from_dict(
                {
                    "frame.label": "bold",
                    "status": "reverse",
                }
            ),
        )
        self._runtime = TaskRuntime(
            orchestrator=self._orchestrator,
            on_progress=self._on_progress,
            on_need_input=self._on_need_input,
            on_task_final=self._on_task_final,
        )
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)

    def run(self) -> int:
        if not self._gemini.configured:
            print(
                "Gemini не настроен. Укажи GEMINI_API_KEY и GEMINI_MODEL в .env.",
                file=sys.stderr,
            )
            return 1
        self._append_chat(
            "sys",
            "TUI запущен. Ввод всегда активен. Alt+Up/Alt+Down выбрать задачу, "
            "Alt+Enter развернуть. Esc или Ctrl+C для выхода.",
        )
        self._runtime.start()
        self._refresh_thread.start()
        try:
            self._app.run()
        finally:
            self._stop_event.set()
            self._runtime.stop()
        return 0

    def _build_layout(self) -> Layout:
        body = HSplit(
            [
                VSplit(
                    [
                        Frame(
                            Window(self._tasks_control, wrap_lines=False),
                            title="Задачи",
                        ),
                        Frame(
                            Window(self._chat_control, wrap_lines=True),
                            title="Диалог",
                        ),
                    ]
                ),
                Frame(self._input, title="Сообщение"),
            ]
        )
        return Layout(container=body, focused_element=self._input)

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("escape")
        def _exit(event) -> None:
            event.app.exit()

        @kb.add("escape", "up")
        def _prev_task(event) -> None:
            with self._lock:
                if self._selected > 0:
                    self._selected -= 1
            event.app.invalidate()

        @kb.add("escape", "down")
        def _next_task(event) -> None:
            with self._lock:
                if self._selected < max(len(self._tasks) - 1, 0):
                    self._selected += 1
            event.app.invalidate()

        @kb.add("escape", "enter")
        def _toggle_expand(event) -> None:
            with self._lock:
                if not self._tasks:
                    return
                task_id = str(self._tasks[self._selected].get("id"))
                if task_id in self._expanded:
                    self._expanded.remove(task_id)
                else:
                    self._expanded.add(task_id)
            event.app.invalidate()

        return kb

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                tasks = self._orchestrator.list_tasks(limit=30)
                with self._lock:
                    self._tasks = tasks
                    if self._selected >= len(self._tasks):
                        self._selected = max(len(self._tasks) - 1, 0)
                self._app.invalidate()
            except Exception as exc:  # noqa: BLE001
                self._append_chat("sys", f"Ошибка обновления задач: {exc}")
            self._stop_event.wait(1.0)

    def _render_tasks(self) -> str:
        with self._lock:
            tasks = list(self._tasks)
            selected = self._selected
            expanded = set(self._expanded)
        if not tasks:
            return "Нет задач."
        lines = ["Текущие и недавние задачи:"]
        for idx, task in enumerate(tasks):
            task_id = str(task.get("id", ""))
            is_selected = idx == selected
            marker = ">" if is_selected else " "
            status = str(task.get("status", "unknown"))
            title = str(task.get("instruction", "")).strip().replace("\n", " ")
            if len(title) > 52:
                title = f"{title[:49]}..."
            opener = "▼" if task_id in expanded else "▸"
            lines.append(f"{marker} {opener} {task_id[:8]} [{status}] {title}")
            if task_id in expanded:
                lines.append(f"    branch: {task.get('branch_name') or '-'}")
                lines.append(f"    updated: {task.get('updated_at') or '-'}")
                if task.get("last_error"):
                    lines.append(f"    error: {task.get('last_error')}")
                full = self._orchestrator.get_task(task_id)
                events = (full or {}).get("events") or []
                for event in events[:6]:
                    lines.append(f"    - {event.get('message')}")
        return "\n".join(lines)

    def _render_chat(self) -> str:
        with self._lock:
            lines = list(self._chat_lines[-500:])
        if not lines:
            return "Диалог пуст."
        return "\n".join(lines)

    def _append_chat(self, role: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chat_lines.append(f"{role}> {text}")
        self._app.invalidate()

    def _tracked_ids(self) -> list[str]:
        return self._runtime.list_tracked()

    def _active_summary(self) -> list[dict[str, Any]]:
        rows = self._orchestrator.list_subagents(limit=50, active_only=True)
        active_task_statuses = {"queued", "running", "waiting_ci"}
        result: list[dict[str, Any]] = []
        for row in rows:
            task_id = str(row.get("task_id", ""))
            task = self._orchestrator.get_task(task_id) if task_id else None
            task_status = task.get("status") if task else None
            if task_status not in active_task_statuses:
                continue
            result.append(
                {
                    "subagent_id": row.get("id"),
                    "kind": row.get("kind"),
                    "status": row.get("status"),
                    "activity": row.get("activity"),
                    "task_id": task_id,
                    "task_status": task_status,
                }
            )
        return result

    @staticmethod
    def _pick_task_id(
        explicit_task_id: str | None,
        active_summary: list[dict[str, Any]],
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

    def _on_submit(self, buffer) -> bool:
        text = buffer.text.strip()
        buffer.text = ""
        if not text:
            return False
        if text in {"/exit", "/quit"}:
            self._app.exit()
            return False
        if text == "/help":
            self._append_chat("sys", "Пиши обычным языком. Служебные: /help, /exit.")
            return False
        self._append_chat("you", text)
        self._handle_user_message(text)
        return False

    def _handle_user_message(self, user_text: str) -> None:
        active_summary = self._active_summary()
        tracked_task_ids = self._tracked_ids()
        try:
            decision = self._gemini.route_intent(
                user_text=user_text,
                active_subagents=active_summary,
                tracked_task_ids=tracked_task_ids,
            )
        except Exception as exc:  # noqa: BLE001
            self._append_chat("sys", f"Ошибка Gemini: {exc}")
            return
        if self._gemini.last_notice:
            self._append_chat("sys", self._gemini.last_notice)
            self._gemini.last_notice = ""

        if decision.action == "self_improve":
            instruction = (decision.instruction or user_text).strip()
            result = self._orchestrator.submit_task(
                instruction=instruction,
                priority="normal",
                process_now=False,
            )
            self._runtime.track(result.task_id)
            self._append_chat(
                "ai",
                f"Принял. Поставил задачу в очередь: {result.task_id} ({result.status}).",
            )
            return

        if decision.action == "show_active":
            self._append_chat("ai", json.dumps(active_summary, ensure_ascii=False, indent=2))
            return

        if decision.action == "show_subagents":
            rows = self._orchestrator.list_subagents(limit=100, active_only=False)
            self._append_chat("ai", json.dumps(rows, ensure_ascii=False, indent=2))
            return

        if decision.action in {"show_status", "show_logs"}:
            task_id = self._pick_task_id(decision.task_id, active_summary, tracked_task_ids)
            if not task_id:
                self._append_chat("ai", "Не вижу активной задачи. Уточните task_id.")
                return
            task = self._orchestrator.get_task(task_id)
            if not task:
                self._append_chat("ai", f"Задача {task_id} не найдена.")
                return
            if decision.action == "show_status":
                self._append_chat("ai", json.dumps(task, ensure_ascii=False, indent=2))
            else:
                self._append_chat(
                    "ai",
                    json.dumps(task.get("events", []), ensure_ascii=False, indent=2),
                )
            return

        if decision.action == "list_tasks":
            self._append_chat(
                "ai",
                json.dumps(self._orchestrator.list_tasks(limit=20), ensure_ascii=False, indent=2),
            )
            return

        answer = decision.reply
        if not answer:
            try:
                answer = self._gemini.chat(history=self._history, user_text=user_text)
            except Exception as exc:  # noqa: BLE001
                self._append_chat("sys", f"Ошибка Gemini: {exc}")
                return
        self._append_chat("ai", answer)
        self._history.append({"role": "user", "text": user_text})
        self._history.append({"role": "assistant", "text": answer})

    def _on_progress(self, task_id: str, progress: int, text: str) -> None:
        self._append_chat("sys", f"task[{task_id}] {progress}% - {text}")

    def _on_need_input(self, task_id: str, question: str) -> None:
        self._append_chat("sys", f"task[{task_id}] нужен ваш ответ: {question}")

    def _on_task_final(self, task: dict[str, Any]) -> None:
        summary = self._gemini.summarize_task_result(task)
        self._append_chat("ai", summary)


def run_tui() -> int:
    tui = PhoenixTui()
    return tui.run()
