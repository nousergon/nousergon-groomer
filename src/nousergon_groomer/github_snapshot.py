"""GitHub snapshot adapter — fetch live state → Item[] + ObservedWorld.

Fetches open issues and PRs from the GitHub REST API via ``httpx`` and maps
them into the groomer core's :class:`Item` and :class:`ObservedWorld` types.
The token is supplied by the caller (typically read from an env var outside
this module); no credentials are read here.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .dependency_evaluator import ObservedWorld
from .models import Item, ItemKind, ItemState

_ISSUE_REF = re.compile(r"#(\d+)")
_API_BASE = "https://api.github.com"

logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    """Raised when the GitHub API returns an error or the request fails."""


def _parse_ci_from_rollup(data: dict[str, Any]) -> Optional[bool]:
    """Derive CI green/red/pending from ``statusCheckRollup`` when present."""
    rollup = data.get("statusCheckRollup")
    if rollup is None:
        return None
    state = rollup.get("state") if isinstance(rollup, dict) else rollup
    if state == "SUCCESS":
        return True
    if state in ("FAILURE", "ERROR"):
        return False
    if state in (
        "PENDING",
        "EXPECTED",
        "WAITING",
        "QUEUED",
        "REQUESTED",
        "IN_PROGRESS",
    ):
        return None
    return None


def _issues_referenced_by_prs(prs: list[dict[str, Any]]) -> set[int]:
    """Collect issue numbers referenced in open PR titles and bodies."""
    refs: set[int] = set()
    for pr in prs:
        text = f"{pr.get('title', '')}\n{pr.get('body') or ''}"
        refs.update(int(match) for match in _ISSUE_REF.findall(text))
    return refs


def _label_names(raw: dict[str, Any]) -> list[str]:
    return [label["name"] for label in raw.get("labels", []) if "name" in label]


def _derive_issue_state(
    issue: dict[str, Any], issues_with_open_pr: set[int]
) -> ItemState:
    if issue["number"] in issues_with_open_pr:
        return ItemState.OPEN_ISSUE_WAITING
    return ItemState.OPEN_ISSUE_ACTIONABLE


def _derive_pr_state(pr: dict[str, Any], ci_green: Optional[bool]) -> ItemState:
    if pr.get("draft"):
        return ItemState.OPEN_DRAFT
    mergeable = pr.get("mergeable")
    mergeable_state = pr.get("mergeable_state")
    if mergeable is False or mergeable_state == "dirty":
        return ItemState.OPEN_DIRTY
    if ci_green is False:
        return ItemState.OPEN_RED_CI
    if ci_green is None:
        return ItemState.OPEN_PENDING_CI
    if ci_green is True and (mergeable is True or mergeable_state == "clean"):
        return ItemState.OPEN_CLEAN_GREEN
    return ItemState.OPEN_PENDING_CI


def _issue_to_item(
    raw: dict[str, Any],
    *,
    kind: ItemKind,
    state: ItemState,
    is_draft: bool = False,
    mergeable: Optional[bool] = None,
    ci_green: Optional[bool] = None,
) -> Item:
    return Item(
        id=str(raw["number"]),
        kind=kind,
        state=state,
        title=raw.get("title") or "",
        labels=_label_names(raw),
        is_draft=is_draft,
        mergeable=mergeable,
        ci_green=ci_green,
    )


class GitHubSnapshot:
    """Fetch live GitHub backlog state and record it as core domain objects."""

    def __init__(self, token: str, http_client: Any | None = None) -> None:
        if token is None or not str(token).strip():
            raise ValueError("GitHub token must be a non-empty string")
        self._token = str(token).strip()
        self._owns_client = http_client is None
        if http_client is None:
            try:
                import httpx
            except ImportError as exc:
                raise ImportError(
                    "httpx is required for GitHubSnapshot. "
                    "Install with: pip install nousergon-groomer[github]"
                ) from exc
            http_client = httpx.Client(
                base_url=_API_BASE,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
        self._client = http_client

    def fetch(self, repo: str) -> tuple[list[Item], ObservedWorld]:
        """Fetch open items and terminal ids for ``repo`` (``owner/name``)."""
        open_issues_raw = self._get_paginated(f"/repos/{repo}/issues", {"state": "open"})
        open_prs_raw = self._get_paginated(f"/repos/{repo}/pulls", {"state": "open"})
        closed_issues_raw = self._get_paginated(
            f"/repos/{repo}/issues", {"state": "closed"}
        )
        closed_prs_raw = self._get_paginated(f"/repos/{repo}/pulls", {"state": "closed"})

        # Enrich PR list data with per-PR endpoint fields.
        # GitHub's LIST /repos/{repo}/pulls endpoint does not compute mergeability
        # (mergeable / mergeable_state are always null) and omits statusCheckRollup.
        # The single GET /repos/{repo}/pulls/{number} endpoint returns both, so we
        # re-fetch each PR individually when the list data is missing these fields
        # (config#6168).
        for pr in open_prs_raw:
            if pr.get("mergeable") is None:
                try:
                    detail_resp = self._get_response(
                        f"/repos/{repo}/pulls/{pr['number']}", None
                    )
                    detail = detail_resp.json()
                    if not isinstance(detail, dict):
                        continue
                    pr["mergeable"] = detail.get("mergeable")
                    pr["mergeable_state"] = detail.get("mergeable_state")
                    if "statusCheckRollup" in detail:
                        pr["statusCheckRollup"] = detail["statusCheckRollup"]
                except Exception as exc:
                    # Per-PR fetch is best-effort; fall back to (null) list data
                    # and surface the degradation in the logs instead of
                    # silently dropping the mergeability signal (S110).
                    logger.warning(
                        "Per-PR mergeability enrichment failed for %s#%s: %s",
                        repo,
                        pr["number"],
                        exc,
                    )

        open_issues = [issue for issue in open_issues_raw if "pull_request" not in issue]
        issues_with_open_pr = _issues_referenced_by_prs(open_prs_raw)

        items: list[Item] = []
        for issue in open_issues:
            state = _derive_issue_state(issue, issues_with_open_pr)
            items.append(_issue_to_item(issue, kind=ItemKind.ISSUE, state=state))

        for pr in open_prs_raw:
            ci_green = _parse_ci_from_rollup(pr)
            state = _derive_pr_state(pr, ci_green)
            items.append(
                _issue_to_item(
                    pr,
                    kind=ItemKind.PR,
                    state=state,
                    is_draft=bool(pr.get("draft")),
                    mergeable=pr.get("mergeable"),
                    ci_green=ci_green,
                )
            )

        terminal_items: set[str] = set()
        for issue in closed_issues_raw:
            if "pull_request" not in issue:
                terminal_items.add(str(issue["number"]))
        for pr in closed_prs_raw:
            terminal_items.add(str(pr["number"]))

        world = ObservedWorld(terminal_items=terminal_items)
        return items, world

    def _get_paginated(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results: list[Any] = []
        next_path: str | None = path
        next_params = params
        while next_path is not None:
            response = self._get_response(next_path, next_params)
            batch = response.json()
            if not isinstance(batch, list):
                raise SnapshotError(f"Expected JSON array from {next_path}, got {type(batch)}")
            results.extend(batch)
            next_path, next_params = self._parse_next_link(response.headers.get("Link"))
        return results

    def _get_response(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for GitHubSnapshot. "
                "Install with: pip install nousergon-groomer[github]"
            ) from exc
        try:
            response = self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise SnapshotError(f"GitHub API timeout for {path}") from exc
        except httpx.HTTPError as exc:
            raise SnapshotError(f"GitHub API request failed for {path}: {exc}") from exc

        if response.status_code == 403:
            remaining = response.headers.get("X-RateLimit-Remaining", "")
            if remaining == "0" or "rate limit" in response.text.lower():
                raise SnapshotError("GitHub API rate limit exceeded")
        if response.status_code != 200:
            raise SnapshotError(
                f"GitHub API returned {response.status_code} for {path}"
            )
        return response

    @staticmethod
    def _parse_next_link(link_header: str | None) -> tuple[str | None, dict[str, Any] | None]:
        if not link_header:
            return None, None
        for part in link_header.split(","):
            section = part.strip()
            if 'rel="next"' not in section:
                continue
            url = section[section.find("<") + 1 : section.find(">")]
            if url.startswith(_API_BASE):
                url = url[len(_API_BASE) :]
            return url, None
        return None, None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GitHubSnapshot:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
