from pathlib import Path

from app.services.codex_executor import CodexExecutor


def _make_codex_script(tmp_path: Path) -> Path:
    script = tmp_path / "Program Files" / "nodejs" / "codex.ps1"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("", encoding="utf-8")
    return script


def test_build_command_unquoted_ps1_path_with_spaces(tmp_path: Path) -> None:
    script = _make_codex_script(tmp_path)
    executor = CodexExecutor(repo_path=tmp_path, executor_cmd=str(script), timeout_sec=30)

    command = executor._build_command("do work", tmp_path / "task.json")

    assert command[:6] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    assert command[6:] == ["exec", "-s", "workspace-write", "do work"]


def test_build_command_unquoted_ps1_path_with_spaces_and_args(tmp_path: Path) -> None:
    script = _make_codex_script(tmp_path)
    raw_cmd = f"{script} --model gpt-5.3-codex"
    executor = CodexExecutor(repo_path=tmp_path, executor_cmd=raw_cmd, timeout_sec=30)

    command = executor._build_command("do work", tmp_path / "task.json")

    assert command[:6] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    assert command[6:] == [
        "--model",
        "gpt-5.3-codex",
        "exec",
        "-s",
        "workspace-write",
        "do work",
    ]
