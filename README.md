# Phoenix Agent

CLI-first self-improving agent orchestrator:
- accepts self-improve instructions,
- delegates coding to an external Codex executor,
- validates (`lint + tests + health-check`),
- pushes to feature branch, creates PR, auto-merges after green CI,
- restarts service and rolls back with `git revert` on failed post-restart health-check.

## 1. Quick start

```powershell
python -m venv .venv
.venv\\Scripts\\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set `.env` values:
- `PHOENIX_EXECUTOR_CMD` - command to run external Codex worker (self-improve only).
- `GEMINI_API_KEY`, `GEMINI_MODEL` - interactive chat model credentials.
- `TELEGRAM_BOT_TOKEN` - Telegram bot token for long-polling mode.
- `TELEGRAM_ALLOWED_CHAT_IDS` - optional CSV list of allowed chat ids.
- `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_TOKEN` - required for PR/auto-merge.

## 2. CLI usage

```powershell
phoenix submit --text "Add better retry logic"
phoenix chat
phoenix list
phoenix status --task-id <id>
phoenix logs --task-id <id>
phoenix worker-once
phoenix rollback --task-id <id>
phoenix telegram
```

## 3. Run API server

```powershell
phoenix serve --host 127.0.0.1 --port 8666
```

Endpoints:
- `POST /tasks/self-improve`
- `GET /tasks/{task_id}`
- `GET /tasks`
- `POST /tasks/{task_id}/rollback`
- `GET /health`

## 4. Phase roadmap

- Phase 1 (implemented): CLI + orchestrator pipeline.
- Phase 2: richer Web UI over existing API.
- Phase 3 (implemented): Telegram adapter mapped to same task service.

Default API port is `8666`.

## 5. Windows service

Set `PHOENIX_SERVICE_NAME` and configure service manager (NSSM or Task Scheduler).
`scripts/restart_service.ps1` is used for controlled restart.
