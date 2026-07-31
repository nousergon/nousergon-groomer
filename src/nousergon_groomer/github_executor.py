"""GitHub executor adapter — ReconcilerResult ACT dispositions → GitHub API actions.

Takes :class:`ReconcilerResult` ACT dispositions and performs them via the GitHub
REST API. Mechanical actions (``automerge``, ``create_pr``) use the API directly;
judgment actions (``fix_ci``, ``resolve_conflicts``, ``advance_draft``) delegate
to an optional :class:`ModelProvider`.

Designed as a base class: the private layer subclasses and overrides action
handlers (e.g. ``_automerge``) for fleet-specific gates.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from .model_provider import ModelProvider
from .models import DispositionKind, Item, ItemKind
from .reconciler import ReconcilerResult

_API_BASE = "https://api.github.com"

_JUDGMENT_ACTIONS = frozenset({"fix_ci", "resolve_conflicts", "advance_draft"})

__all__ = [
    "ExecutorAbortError",
    "ExecutorError",
    "ExecutorResult",
    "GitHubExecutor",
]


class ExecutorError(BaseModel):
    """Record of a failed per-item execution."""

    item_id: str
    action: str
    error: str


class ExecutorResult(BaseModel):
    """Outcome of one executor pass."""

    executed: list[str]
    skipped: list[str]
    errors: list[ExecutorError]
    dry_run: bool


class ExecutorAbortError(Exception):
    """Raised on unrecoverable failures (API auth errors, etc.)."""


class GitHubExecutor:
    """Execute ReconcilerResult ACT dispositions via the GitHub API."""

    def __init__(
        self,
        token: str,
        model_provider: Optional[ModelProvider] = None,
        dry_run: bool = False,
        http_client: Any | None = None,
        *,
        model: str = "default",
        default_base_branch: str = "main",
    ) -> None:
        if token is None or not str(token).strip():
            raise ValueError("GitHub token must be a non-empty string")
        self._token = str(token).strip()
        self._model_provider = model_provider
        self._dry_run = dry_run
        self._model = model
        self._default_base_branch = default_base_branch
        self._owns_client = http_client is None
        if http_client is None:
            try:
                import httpx
            except ImportError as exc:
                raise ImportError(
                    "httpx is required for GitHubExecutor. "
                    "Install with: pip install nousergon-groomer[github]"
                ) from exc
            http_client = httpx.Client(
                base_url=_API_BASE,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=60.0,
            )
        self._client = http_client

    def execute(
        self,
        result: ReconcilerResult,
        items: list[Item],
        *,
        repo: str,
    ) -> ExecutorResult:
        """Perform ACT dispositions from ``result`` against ``items`` in ``repo``."""
        item_by_id = {item.id: item for item in items}
        executed: list[str] = []
        skipped: list[str] = []
        errors: list[ExecutorError] = []

        for item_disp in result.items:
            disposition = item_disp.disposition
            kind = disposition.kind
            item_id = item_disp.item_id

            if kind is DispositionKind.TERMINAL or kind is DispositionKind.BLOCKED:
                skipped.append(item_id)
                continue

            if kind is DispositionKind.UNDECIDABLE:
                errors.append(
                    ExecutorError(
                        item_id=item_id,
                        action="",
                        error=disposition.reason or "undecidable disposition",
                    )
                )
                continue

            if kind is not DispositionKind.ACT:
                skipped.append(item_id)
                continue

            action = disposition.action or ""
            item = item_by_id.get(item_id)
            if item is None:
                errors.append(
                    ExecutorError(
                        item_id=item_id,
                        action=action,
                        error=f"item {item_id!r} not found in items list",
                    )
                )
                continue

            if action in _JUDGMENT_ACTIONS and self._model_provider is None:
                skipped.append(item_id)
                continue

            if self._dry_run:
                print(f"[dry-run] {item_id}: would execute {action}")
                executed.append(item_id)
                continue

            try:
                self._dispatch_action(action, item, repo)
                executed.append(item_id)
            except ExecutorAbortError:
                raise
            except Exception as exc:
                errors.append(
                    ExecutorError(
                        item_id=item_id,
                        action=action,
                        error=str(exc),
                    )
                )

        return ExecutorResult(
            executed=executed,
            skipped=skipped,
            errors=errors,
            dry_run=self._dry_run,
        )

    def _dispatch_action(self, action: str, item: Item, repo: str) -> None:
        handlers = {
            "automerge": self._automerge,
            "create_pr": self._create_pr,
            "fix_ci": self._fix_ci,
            "resolve_conflicts": self._resolve_conflicts,
            "advance_draft": self._advance_draft,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError(f"unknown ACT action: {action!r}")
        handler(item, repo)

    def _automerge(self, item: Item, repo: str) -> None:
        if item.kind is not ItemKind.PR:
            raise ValueError(f"automerge requires a PR item, got {item.kind.value}")
        response = self._client.put(
            f"/repos/{repo}/pulls/{item.id}/merge",
            json={"merge_method": "squash"},
        )
        self._raise_for_response(response, f"merge PR #{item.id}")

    def _create_pr(self, item: Item, repo: str) -> None:
        if item.kind is not ItemKind.ISSUE:
            raise ValueError(f"create_pr requires an issue item, got {item.kind.value}")
        body = ""
        if self._model_provider is not None:
            prompt = (
                f"Write a pull request description for GitHub issue #{item.id} "
                f"titled {item.title!r}. Return only the markdown body."
            )
            body = self._model_provider.complete(prompt, model=self._model)
        title = item.title or f"Issue #{item.id}"
        head = f"feat/issue-{item.id}"
        response = self._client.post(
            f"/repos/{repo}/pulls",
            json={
                "title": title,
                "head": head,
                "base": self._default_base_branch,
                "body": body,
            },
        )
        self._raise_for_response(response, f"create PR for issue #{item.id}")

    def _fix_ci(self, item: Item, repo: str) -> None:
        if item.kind is not ItemKind.PR:
            raise ValueError(f"fix_ci requires a PR item, got {item.kind.value}")
        context = self._fetch_pr_context(item, repo)
        ci_log = self._fetch_ci_log(item, repo)
        prompt = (
            f"Fix failing CI for PR #{item.id} in {repo}.\n\n"
            f"PR context:\n{context}\n\nCI log:\n{ci_log}\n\n"
            "Return a concise commit message describing the fix."
        )
        if self._model_provider is None:
            raise RuntimeError("fix_ci requires a model provider")
        fix_message = self._model_provider.complete(prompt, model=self._model)
        self._push_commit(item, repo, fix_message)

    def _resolve_conflicts(self, item: Item, repo: str) -> None:
        if item.kind is not ItemKind.PR:
            raise ValueError(f"resolve_conflicts requires a PR item, got {item.kind.value}")
        context = self._fetch_pr_context(item, repo)
        prompt = (
            f"Resolve merge conflicts for PR #{item.id} in {repo}.\n\n"
            f"PR context:\n{context}\n\n"
            "Return a concise commit message describing the resolution."
        )
        if self._model_provider is None:
            raise RuntimeError("resolve_conflicts requires a model provider")
        resolution_message = self._model_provider.complete(prompt, model=self._model)
        self._push_commit(item, repo, resolution_message)

    def _advance_draft(self, item: Item, repo: str) -> None:
        if item.kind is not ItemKind.PR:
            raise ValueError(f"advance_draft requires a PR item, got {item.kind.value}")
        prompt = (
            f"Review draft PR #{item.id} titled {item.title!r} with labels "
            f"{item.labels}. Recommend label hygiene or whether to mark ready. "
            "Return READY or DRAFT followed by a brief reason."
        )
        if self._model_provider is None:
            raise RuntimeError("advance_draft requires a model provider")
        advice = self._model_provider.complete(prompt, model=self._model)
        if advice.strip().upper().startswith("READY"):
            response = self._client.patch(
                f"/repos/{repo}/pulls/{item.id}",
                json={"draft": False},
            )
            self._raise_for_response(response, f"mark PR #{item.id} ready for review")

    def _fetch_pr_context(self, item: Item, repo: str) -> str:
        response = self._client.get(f"/repos/{repo}/pulls/{item.id}")
        self._raise_for_response(response, f"fetch PR #{item.id}")
        data = response.json()
        return (
            f"title={data.get('title')!r}, head={data.get('head', {}).get('ref')!r}, "
            f"base={data.get('base', {}).get('ref')!r}, mergeable={data.get('mergeable')!r}"
        )

    def _fetch_ci_log(self, item: Item, repo: str) -> str:
        response = self._client.get(
            f"/repos/{repo}/commits/{item.id}/check-runs",
        )
        if response.status_code == 404:
            head_response = self._client.get(f"/repos/{repo}/pulls/{item.id}")
            self._raise_for_response(head_response, f"fetch PR #{item.id} for CI")
            head_sha = head_response.json().get("head", {}).get("sha", item.id)
            response = self._client.get(f"/repos/{repo}/commits/{head_sha}/check-runs")
        self._raise_for_response(response, f"fetch CI checks for PR #{item.id}")
        data = response.json()
        runs = data.get("check_runs", [])
        if not runs:
            return "no check runs reported"
        lines = []
        for run in runs:
            name = run.get("name", "unknown")
            conclusion = run.get("conclusion") or run.get("status", "unknown")
            output = run.get("output", {}) or {}
            summary = output.get("summary") or output.get("title") or ""
            lines.append(f"- {name}: {conclusion} {summary}".strip())
        return "\n".join(lines)

    def _push_commit(self, item: Item, repo: str, message: str) -> None:
        """Record a fix commit on the PR head branch (subclasses may override)."""
        pr_response = self._client.get(f"/repos/{repo}/pulls/{item.id}")
        self._raise_for_response(pr_response, f"fetch PR #{item.id} for commit")
        pr_data = pr_response.json()
        head = pr_data.get("head") or {}
        ref = head.get("ref")
        sha = head.get("sha")
        if not ref or not sha:
            raise ValueError(f"PR #{item.id} missing head ref or sha")
        response = self._client.post(
            f"/repos/{repo}/git/commits",
            json={
                "message": message.strip() or f"fix: PR #{item.id}",
                "tree": self._head_tree_sha(repo, sha),
                "parents": [sha],
            },
        )
        self._raise_for_response(response, f"create commit for PR #{item.id}")
        commit_sha = response.json()["sha"]
        update_response = self._client.patch(
            f"/repos/{repo}/git/refs/heads/{ref}",
            json={"sha": commit_sha},
        )
        self._raise_for_response(update_response, f"update branch {ref} for PR #{item.id}")

    def _head_tree_sha(self, repo: str, commit_sha: str) -> str:
        response = self._client.get(f"/repos/{repo}/git/commits/{commit_sha}")
        self._raise_for_response(response, f"fetch commit {commit_sha}")
        tree_sha = response.json().get("tree", {}).get("sha")
        if not tree_sha:
            raise ValueError(f"commit {commit_sha} missing tree sha")
        return tree_sha

    def _raise_for_response(self, response: Any, operation: str) -> None:
        if response.status_code == 401:
            raise ExecutorAbortError(
                f"GitHub authentication failed while trying to {operation}"
            )
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"GitHub API returned {response.status_code} while trying to "
                f"{operation}: {getattr(response, 'text', '')}"
            )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubExecutor:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
