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

from pydantic import BaseModel

from .dependency_evaluator import ObservedWorld
from .models import Dependency, DependencyKind, Item, ItemKind, ItemState

_ISSUE_REF = re.compile(r"#(\d+)")
_API_BASE = "https://api.github.com"

#: Org-wide cap on issue fields (config#6320, verified against the live
#: ``nousergon`` org 2026-08-03: 4 of 25 slots in use). A shared, capped
#: resource — the conformance row exists so a caller budgets against
#: ``.free`` instead of guessing.
_ISSUE_FIELD_CAP = 25

logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    """Raised when the GitHub API returns an error or the request fails."""


class IssueFieldConformance(BaseModel):
    """§10 conformance row: org-level issue-field slots used against the cap.

    ``GET /orgs/{org}/issue-fields`` is the single source of truth for what
    exists; this model is read-only and creates nothing. Any caller weighing
    whether to add a new field reads ``.free`` here first (config#6320: 21
    slots were free of 25 as of 2026-08-03, with #6310/#6311 also wanting
    some for ``max_residence`` and human-owner/due-date).
    """

    org: str
    cap: int
    field_names: list[str]

    @property
    def used(self) -> int:
        return len(self.field_names)

    @property
    def free(self) -> int:
        return self.cap - self.used

    def conformance_row(self) -> str:
        names = ", ".join(self.field_names) if self.field_names else "(none)"
        return (
            f"issue-fields[{self.org}]: {self.used}/{self.cap} used "
            f"({self.free} free) — {names}"
        )


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


def _qualify(repo: str, number: int | str) -> str:
    """Fleet-unique, cross-repo-safe id: ``owner/name#number``."""
    return f"{repo}#{number}"


def _dependency_from_native(raw: dict[str, Any]) -> tuple[Dependency, bool, str]:
    """Build a ``Dependency`` + terminal flag from one native ``blocked_by``
    entry (config#6320, clause ``SCM-9.1-issue-identity-is-the-global-id``).

    Repo attribution is read from ``raw["repository"]["full_name"]`` in the
    response body — **never** inferred from the requested URL or the bare
    ``number`` alone. ``GET /repos/{o}/{r}/issues/{n}`` silently follows a
    transfer (requesting ``nous-ergon-ops#356`` returned
    ``alpha-engine-config#6128`` with no redirect indication in the payload),
    so a number is not a stable repo-scoped identity; only the response's own
    ``repository.full_name`` (backed by the global ``id``) is.

    Returns ``(dependency, is_terminal, qualified_id)`` where ``is_terminal``
    is ``True`` iff the blocker's own ``state`` is ``"closed"`` — read
    directly off this response, never by a second lookup, so a cross-repo
    blocker's terminal-ness is known without fetching its home repo.
    """
    repository = raw.get("repository") or {}
    full_name = repository.get("full_name")
    number = raw.get("number")
    if not full_name or number is None:
        raise SnapshotError(
            f"native dependency entry missing repository.full_name or number: {raw!r}"
        )
    qid = _qualify(full_name, number)
    kind = DependencyKind.PR_TERMINAL if "pull_request" in raw else DependencyKind.ISSUE_TERMINAL
    is_terminal = raw.get("state") == "closed"
    return Dependency(kind=kind, target=qid), is_terminal, qid


def _custom_fields_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """Surface ``issue_field_values`` generically, keyed by field name.

    GitHub returns this inline on every issue/PR-shaped object once issue
    fields are GA (an empty list when the item carries no values) — no
    extra fetch is needed. This package assigns no meaning to any field
    name; interpreting a specific one is the §3.1 write-boundary chokepoint's
    job (issue #6309), not the adapter's.
    """
    values = raw.get("issue_field_values") or []
    fields: dict[str, Any] = {}
    for entry in values:
        name = entry.get("issue_field_name") or entry.get("field_name")
        if not name:
            continue
        fields[name] = entry.get("value")
    return fields


def _should_probe_dependencies(raw: dict[str, Any], *, kind: ItemKind) -> bool:
    """Whether ``fetch()`` should call the ``blocked_by`` endpoint for this item.

    Issues carry ``issue_dependencies_summary`` inline on the list/get
    response once GA (measured 2026-08-04); a summary reporting zero means
    the extra call is skippable. **PR-shaped issue objects never carry this
    summary** — measured on both ``GET /pulls`` (list) and ``GET
    /pulls/{n}`` (the per-PR detail endpoint this module already calls for
    mergeability) — so there is no cheap zero-check for PRs and every open
    PR is probed unconditionally. This mirrors the existing per-PR
    mergeability enrichment's cost shape (one extra call per open PR), not a
    new order of magnitude.

    An item with no ``issue_dependencies_summary`` key at all (older
    fixtures, or a GitHub response predating the GA primitive) is treated as
    "not probed" rather than "assume zero and call anyway" — the safer
    default when the surface's presence itself is uncertain.
    """
    if kind is ItemKind.PR:
        return True
    summary = raw.get("issue_dependencies_summary")
    if not summary:
        return False
    return bool(summary.get("total_blocked_by"))


def _should_probe_sub_issues(raw: dict[str, Any]) -> bool:
    """Whether ``fetch()`` should enumerate this issue's sub-issue children.

    Gated on the inline ``sub_issues_summary.total`` count (free — no extra
    call to check). Sub-issues are not probed for PRs: PR-shaped issue
    objects never carry ``sub_issues_summary`` (measured 2026-08-04), and no
    consumer currently models a PR as an epic.
    """
    summary = raw.get("sub_issues_summary")
    if not summary:
        return False
    return bool(summary.get("total"))


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
    declared_dependencies: Optional[list[Dependency]] = None,
    custom_fields: Optional[dict[str, Any]] = None,
    sub_issue_ids: Optional[list[str]] = None,
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
        declared_dependencies=declared_dependencies or [],
        custom_fields=custom_fields or {},
        sub_issue_ids=sub_issue_ids or [],
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
                    head_sha = (detail.get("head") or {}).get("sha")
                    if head_sha:
                        pr["_ci_green"] = self._fetch_ci_state(repo, head_sha)
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

        # Populated while walking the open issues/PRs below with any blocker
        # this fetch happened to observe as closed — including cross-repo
        # blockers, whose terminal-ness comes straight off the blocked_by
        # response (§3.1 deliverable 1) rather than from a second fetch of
        # the blocker's own repo (config#6320).
        native_terminal_ids: set[str] = set()

        def _native_dependencies(raw: dict[str, Any], *, kind: ItemKind) -> list[Dependency]:
            if not _should_probe_dependencies(raw, kind=kind):
                return []
            number = raw["number"]
            try:
                blockers = self._get_paginated(
                    f"/repos/{repo}/issues/{number}/dependencies/blocked_by"
                )
            except SnapshotError as exc:
                logger.warning(
                    "blocked_by fetch failed for %s#%s: %s", repo, number, exc
                )
                return []
            deps: list[Dependency] = []
            for blocker in blockers:
                try:
                    dep, is_terminal, qid = _dependency_from_native(blocker)
                except SnapshotError as exc:
                    logger.warning(
                        "malformed native dependency entry for %s#%s: %s",
                        repo, number, exc,
                    )
                    continue
                deps.append(dep)
                if is_terminal:
                    native_terminal_ids.add(qid)
            return deps

        def _native_sub_issue_ids(raw: dict[str, Any]) -> list[str]:
            if not _should_probe_sub_issues(raw):
                return []
            number = raw["number"]
            try:
                children = self._get_paginated(f"/repos/{repo}/issues/{number}/sub_issues")
            except SnapshotError as exc:
                logger.warning(
                    "sub_issues fetch failed for %s#%s: %s", repo, number, exc
                )
                return []
            ids: list[str] = []
            for child in children:
                child_repo = (child.get("repository") or {}).get("full_name")
                child_number = child.get("number")
                if not child_repo or child_number is None:
                    logger.warning(
                        "sub-issue entry for %s#%s missing repository.full_name "
                        "or number: %r", repo, number, child,
                    )
                    continue
                ids.append(_qualify(child_repo, child_number))
            return ids

        items: list[Item] = []
        for issue in open_issues:
            state = _derive_issue_state(issue, issues_with_open_pr)
            items.append(
                _issue_to_item(
                    issue,
                    kind=ItemKind.ISSUE,
                    state=state,
                    declared_dependencies=_native_dependencies(issue, kind=ItemKind.ISSUE),
                    custom_fields=_custom_fields_from_raw(issue),
                    sub_issue_ids=_native_sub_issue_ids(issue),
                )
            )

        for pr in open_prs_raw:
            ci_green = pr["_ci_green"] if "_ci_green" in pr else _parse_ci_from_rollup(pr)
            state = _derive_pr_state(pr, ci_green)
            items.append(
                _issue_to_item(
                    pr,
                    kind=ItemKind.PR,
                    state=state,
                    is_draft=bool(pr.get("draft")),
                    mergeable=pr.get("mergeable"),
                    ci_green=ci_green,
                    declared_dependencies=_native_dependencies(pr, kind=ItemKind.PR),
                    custom_fields=_custom_fields_from_raw(pr),
                )
            )

        # Bare numbers preserve backward compatibility for same-repo-scoped
        # callers; qualified (owner/name#number) twins are added for every
        # closed item so declared ISSUE_TERMINAL/PR_TERMINAL dependencies —
        # whether sourced natively above or from a private harness's own
        # qualified declarations — resolve without requiring every item to
        # have been individually probed via blocked_by. native_terminal_ids
        # extends this to blockers living in a DIFFERENT repo than the one
        # requested (config#6320 deliverable 1).
        terminal_items: set[str] = set()
        for issue in closed_issues_raw:
            if "pull_request" not in issue:
                num = str(issue["number"])
                terminal_items.add(num)
                terminal_items.add(_qualify(repo, num))
        for pr in closed_prs_raw:
            num = str(pr["number"])
            terminal_items.add(num)
            terminal_items.add(_qualify(repo, num))
        terminal_items |= native_terminal_ids

        world = ObservedWorld(terminal_items=terminal_items)
        return items, world

    def fetch_issue_field_conformance(self, org: str) -> IssueFieldConformance:
        """§10 conformance row: issue-field slots used against the 25 cap.

        Reads ``GET /orgs/{org}/issue-fields`` — the same org-wide, shared
        25-slot resource #6310/#6311 also want to consume (config#6320
        deliverable 2). Callers budget a new field against ``.free``, never
        against their own guess of what already exists.
        """
        raw = self._get_paginated(f"/orgs/{org}/issue-fields")
        names = sorted(f["name"] for f in raw if "name" in f)
        return IssueFieldConformance(org=org, cap=_ISSUE_FIELD_CAP, field_names=names)

    def _fetch_ci_state(self, repo: str, sha: str) -> Optional[bool]:
        """Derive CI green/red/pending for ``sha`` from the REST API.

        **There is no ``statusCheckRollup`` in REST.** It is a GraphQL field, so
        the per-PR REST endpoint never carries it (measured 2026-08-03: the only
        matching key on ``GET /repos/{repo}/pulls/{n}`` is ``statuses_url``).
        Reading it from a REST payload always yields ``None``, which sent every
        non-dirty PR to ``OPEN_PENDING_CI`` and left the ``ci_red`` bucket
        permanently empty (config#6170).

        The rollup GitHub shows in its UI is the **union of two REST surfaces**,
        and both are required:

        - ``/commits/{sha}/check-runs`` — the Checks API (GitHub Actions).
        - ``/commits/{sha}/status`` — the legacy Statuses API, still used by
          some third-party integrations.

        Consulting only the second is worse than consulting neither: measured
        2026-08-03 on ``alpha-engine-config#5314``, ``/status`` reported
        ``success`` while ``/check-runs`` reported **4 failures** against 4
        successes. A fix built on the Statuses API alone would report that PR
        green and would fail in the unsafe direction.

        Returns ``True`` (all conclusive checks passed), ``False`` (at least one
        failed), or ``None`` (still running, or nothing has reported).
        """
        failing = {"failure", "timed_out", "cancelled", "action_required", "stale"}
        passing = {"success", "neutral", "skipped"}
        saw_pending = False
        saw_any = False

        try:
            runs = self._get_response(f"/repos/{repo}/commits/{sha}/check-runs", None).json()
        except Exception as exc:
            logger.warning("check-runs fetch failed for %s@%s: %s", repo, sha[:8], exc)
            return None
        for run in (runs or {}).get("check_runs", []) or []:
            saw_any = True
            if run.get("status") != "completed":
                saw_pending = True
                continue
            conclusion = run.get("conclusion")
            if conclusion in failing:
                return False
            if conclusion not in passing:
                saw_pending = True

        try:
            combined = self._get_response(f"/repos/{repo}/commits/{sha}/status", None).json()
        except Exception as exc:
            logger.warning("commit status fetch failed for %s@%s: %s", repo, sha[:8], exc)
            combined = {}
        if (combined or {}).get("total_count"):
            saw_any = True
            state = combined.get("state")
            if state == "failure" or state == "error":
                return False
            if state == "pending":
                saw_pending = True

        if not saw_any or saw_pending:
            return None
        return True

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
