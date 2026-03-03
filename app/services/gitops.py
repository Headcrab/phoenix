from __future__ import annotations

import re

import requests

from app.core.config import Settings
from app.services.shell import run_command
from app.services.types import MergeCheckResult, PullRequestResult


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    text = text.strip("-")
    return text or "task"


class GitOps:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._repo_path = settings.repo_path
        self._session = requests.Session()
        if settings.github_enabled:
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {settings.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )

    def ensure_repo(self) -> None:
        result = run_command(["git", "rev-parse", "--is-inside-work-tree"], self._repo_path, 30)
        if not result.ok or result.stdout.strip() != "true":
            raise RuntimeError("Current path is not a git repository")

    def _has_remote(self) -> bool:
        result = run_command(
            ["git", "remote", "get-url", self._settings.remote_name],
            self._repo_path,
            30,
        )
        return result.ok

    def _has_local_branch(self, branch: str) -> bool:
        result = run_command(
            ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
            self._repo_path,
            30,
        )
        return result.ok

    def create_task_branch(self, task_id: str, instruction: str) -> str:
        slug = _slugify(instruction)[:48]
        branch = f"agent/{task_id[:8]}-{slug}"
        if self._has_local_branch(self._settings.main_branch):
            checkout = run_command(
                ["git", "checkout", self._settings.main_branch],
                self._repo_path,
                30,
            )
            if not checkout.ok:
                raise RuntimeError(
                    checkout.stderr or checkout.stdout or "Failed to checkout main branch"
                )
            if self._has_remote():
                run_command(
                    ["git", "pull", self._settings.remote_name, self._settings.main_branch],
                    self._repo_path,
                    60,
                )
        result = run_command(["git", "checkout", "-B", branch], self._repo_path, 30)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "Failed to create branch")
        return branch

    def has_changes(self) -> bool:
        result = run_command(["git", "status", "--porcelain"], self._repo_path, 30)
        if not result.ok:
            raise RuntimeError(result.stderr or "Cannot read git status")
        return bool(result.stdout.strip())

    def commit_all(self, message: str) -> str:
        add_result = run_command(["git", "add", "-A"], self._repo_path, 30)
        if not add_result.ok:
            raise RuntimeError(add_result.stderr or "git add failed")
        commit_result = run_command(["git", "commit", "-m", message], self._repo_path, 30)
        if not commit_result.ok:
            raise RuntimeError(commit_result.stderr or commit_result.stdout or "git commit failed")
        sha_result = run_command(["git", "rev-parse", "HEAD"], self._repo_path, 30)
        if not sha_result.ok:
            raise RuntimeError(sha_result.stderr or "Cannot read commit SHA")
        return sha_result.stdout.strip()

    def push_branch(self, branch: str) -> None:
        if not self._has_remote():
            raise RuntimeError(
                f"Git remote '{self._settings.remote_name}' is not configured. Cannot push branch."
            )
        result = run_command(
            ["git", "push", "-u", self._settings.remote_name, branch],
            self._repo_path,
            90,
        )
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "git push failed")

    def create_pull_request(self, branch: str, title: str, body: str) -> PullRequestResult:
        if not self._settings.github_enabled:
            return PullRequestResult(
                created=False,
                number=None,
                url=None,
                details="GitHub integration is disabled",
            )
        url = (
            f"https://api.github.com/repos/"
            f"{self._settings.github_owner}/{self._settings.github_repo}/pulls"
        )
        resp = self._session.post(
            url,
            json={
                "title": title,
                "head": branch,
                "base": self._settings.main_branch,
                "body": body,
            },
            timeout=30,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"PR creation failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return PullRequestResult(
            created=True,
            number=data["number"],
            url=data["html_url"],
            details="PR created",
        )

    def check_and_maybe_merge(self, pr_number: int) -> MergeCheckResult:
        if not self._settings.github_enabled:
            return MergeCheckResult(
                merged=False,
                pending=False,
                failed=False,
                message="GitHub integration is disabled",
            )
        pr_url = (
            f"https://api.github.com/repos/"
            f"{self._settings.github_owner}/{self._settings.github_repo}/pulls/{pr_number}"
        )
        pr_resp = self._session.get(pr_url, timeout=30)
        if pr_resp.status_code >= 300:
            raise RuntimeError(f"Cannot load PR {pr_number}: {pr_resp.status_code} {pr_resp.text}")
        pr_data = pr_resp.json()
        head_sha = pr_data["head"]["sha"]
        status_url = (
            f"https://api.github.com/repos/"
            f"{self._settings.github_owner}/{self._settings.github_repo}/commits/{head_sha}/status"
        )
        status_resp = self._session.get(status_url, timeout=30)
        if status_resp.status_code >= 300:
            raise RuntimeError(
                f"Cannot load commit status for PR {pr_number}: "
                f"{status_resp.status_code} {status_resp.text}"
            )
        state = status_resp.json().get("state")
        if state in {"failure", "error"}:
            return MergeCheckResult(
                merged=False,
                pending=False,
                failed=True,
                message=f"CI failed with state={state}",
            )
        if state != "success":
            return MergeCheckResult(
                merged=False,
                pending=True,
                failed=False,
                message=f"CI pending with state={state}",
            )
        if not self._settings.auto_merge:
            return MergeCheckResult(
                merged=False,
                pending=False,
                failed=False,
                message="CI passed, auto-merge disabled",
            )
        merge_url = (
            f"https://api.github.com/repos/"
            f"{self._settings.github_owner}/{self._settings.github_repo}/pulls/{pr_number}/merge"
        )
        merge_resp = self._session.put(
            merge_url,
            json={"merge_method": "squash"},
            timeout=30,
        )
        if merge_resp.status_code >= 300:
            return MergeCheckResult(
                merged=False,
                pending=False,
                failed=True,
                message=f"Merge failed: {merge_resp.status_code} {merge_resp.text}",
            )
        return MergeCheckResult(
            merged=True,
            pending=False,
            failed=False,
            message="PR merged",
        )

    def checkout_main_and_pull(self) -> None:
        if not self._has_local_branch(self._settings.main_branch):
            return
        checkout = run_command(["git", "checkout", self._settings.main_branch], self._repo_path, 30)
        if not checkout.ok:
            raise RuntimeError(checkout.stderr or checkout.stdout or "git checkout main failed")
        if self._has_remote():
            result = run_command(
                ["git", "pull", self._settings.remote_name, self._settings.main_branch],
                self._repo_path,
                60,
            )
            if not result.ok:
                raise RuntimeError(result.stderr or result.stdout or "git pull failed")

    def revert_head_and_push(self, task_id: str) -> str:
        message = f"revert(agent): rollback after failed restart [task:{task_id}]"
        result = run_command(["git", "revert", "--no-edit", "HEAD"], self._repo_path, 60)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "git revert failed")
        amend = run_command(["git", "commit", "--amend", "-m", message], self._repo_path, 30)
        if not amend.ok:
            raise RuntimeError(amend.stderr or amend.stdout or "Cannot update revert message")
        if self._has_remote():
            push = run_command(
                ["git", "push", self._settings.remote_name, self._settings.main_branch],
                self._repo_path,
                90,
            )
            if not push.ok:
                raise RuntimeError(push.stderr or push.stdout or "Cannot push rollback")
        sha_result = run_command(["git", "rev-parse", "HEAD"], self._repo_path, 30)
        if not sha_result.ok:
            raise RuntimeError(sha_result.stderr or "Cannot read rollback SHA")
        return sha_result.stdout.strip()
