"""Contract tests for the GitHub snapshot adapter (issue #22, config#6320)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from nousergon_groomer.github_snapshot import (
    GitHubSnapshot,
    IssueFieldConformance,
    SnapshotError,
    _change_refs_by_issue,
)
from nousergon_groomer.models import (
    ChangeCondition,
    Dependency,
    DependencyKind,
    ItemStage,
)


def _mock_response(status_code: int, json_data: object, headers: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = str(json_data)
    response.headers = headers or {}
    return response


def _issue(
    number: int,
    *,
    title: str = "Issue",
    labels: list[str] | None = None,
    body: str = "",
    total_blocked_by: int | None = None,
    sub_issues_total: int | None = None,
    field_values: list[dict] | None = None,
) -> dict:
    raw = {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in (labels or [])],
    }
    if total_blocked_by is not None:
        raw["issue_dependencies_summary"] = {
            "blocked_by": total_blocked_by,
            "total_blocked_by": total_blocked_by,
            "blocking": 0,
            "total_blocking": 0,
        }
    if sub_issues_total is not None:
        raw["sub_issues_summary"] = {
            "total": sub_issues_total,
            "completed": 0,
            "percent_completed": 0,
        }
    if field_values is not None:
        raw["issue_field_values"] = field_values
    return raw


def _native_blocker(
    number: int,
    *,
    repo_full_name: str,
    state: str = "open",
    is_pr: bool = False,
) -> dict:
    """One entry of a native ``dependencies/blocked_by`` (or ``sub_issues``)
    response — a full issue/PR-shaped object with its own ``repository``.
    """
    raw = {
        "number": number,
        "state": state,
        "title": f"blocker {number}",
        "repository": {"full_name": repo_full_name},
    }
    if is_pr:
        raw["pull_request"] = {}
    return raw


def _pr(
    number: int,
    *,
    title: str = "PR",
    draft: bool = False,
    mergeable: bool | None = True,
    ci_state: str | None = "SUCCESS",
    labels: list[str] | None = None,
    body: str = "",
    merged_at: str | None = None,
) -> dict:
    payload = {
        "number": number,
        "title": title,
        "body": body,
        "draft": draft,
        "mergeable": mergeable,
        "labels": [{"name": label} for label in (labels or [])],
        "merged_at": merged_at,
    }
    if ci_state is not None:
        payload["statusCheckRollup"] = {"state": ci_state}
    return payload


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def snapshot(mock_client: MagicMock) -> GitHubSnapshot:
    return GitHubSnapshot(token="test-token", http_client=mock_client)


def test_construct_with_token(mock_client: MagicMock) -> None:
    snap = GitHubSnapshot(token="ghp_test", http_client=mock_client)
    assert snap._token == "ghp_test"


def test_missing_token_raises_value_error(mock_client: MagicMock) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GitHubSnapshot(token="", http_client=mock_client)
    with pytest.raises(ValueError, match="non-empty"):
        GitHubSnapshot(token="   ", http_client=mock_client)


def test_fetch_calls_expected_endpoints(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(1)]),
        _mock_response(200, []),
        _mock_response(200, [_issue(2)]),
        _mock_response(200, []),
    ]

    snapshot.fetch("nousergon/nousergon-groomer")

    assert mock_client.get.call_count == 4
    paths = [call.args[0] for call in mock_client.get.call_args_list]
    assert paths == [
        "/repos/nousergon/nousergon-groomer/issues",
        "/repos/nousergon/nousergon-groomer/pulls",
        "/repos/nousergon/nousergon-groomer/issues",
        "/repos/nousergon/nousergon-groomer/pulls",
    ]
    states = [call.kwargs["params"]["state"] for call in mock_client.get.call_args_list]
    assert states == ["open", "open", "closed", "closed"]


def test_clean_green_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(10, mergeable=True, ci_state="SUCCESS")]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 10 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    pr = next(item for item in items if item.carries_change)
    assert pr.stage is ItemStage.IN_FLIGHT
    assert pr.change.condition is ChangeCondition.CLEAN
    assert pr.change.ci_green is True
    assert pr.change.mergeable is True


def test_draft_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(11, draft=True)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 11 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    pr = items[0]
    assert pr.stage is ItemStage.IN_FLIGHT
    assert pr.change.condition is ChangeCondition.DRAFT
    assert pr.change.is_draft is True


def test_dirty_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(12, mergeable=False, ci_state="SUCCESS")]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 12 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].change.condition is ChangeCondition.CONFLICTED


def test_red_ci_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(13, mergeable=True, ci_state="FAILURE")]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 13 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].change.condition is ChangeCondition.CI_RED
    assert items[0].change.ci_green is False


def test_pending_ci_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(14, mergeable=True, ci_state="PENDING")]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 14 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].change.condition is ChangeCondition.CI_PENDING
    assert items[0].change.ci_green is None


def test_issue_without_linked_pr_is_actionable(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(20, title="Fix bug")]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    issue = items[0]
    assert issue.carries_change is False
    assert issue.stage is ItemStage.PROPOSED
    assert issue.change_ref is None


def test_issue_with_linked_open_pr_is_waiting(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(21, title="Blocked issue")]),
        _mock_response(200, [_pr(22, body="Fixes #21")]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 22 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    issue = next(item for item in items if not item.carries_change)
    # One unit at one stage: the issue is IN_FLIGHT because a change exists,
    # and it names the record carrying it rather than being a second
    # population "waiting on review".
    assert issue.stage is ItemStage.IN_FLIGHT
    assert issue.change_ref == "22"


def test_issue_with_two_open_prs_carries_the_identity_conflict(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """§5.6, alpha-engine-config#6316: two open pull requests both closing
    the same issue must not silently truncate to "first wins" — the extra
    ref rides along in `Item.additional_change_refs` so the reconciler's
    identity invariant can see and flag it, rather than the adapter
    discarding the evidence the way it did before #6316.
    """
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(21, title="Blocked issue")]),
        _mock_response(200, [
            _pr(22, body="Fixes #21"),
            _pr(23, body="Also fixes #21"),
        ]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 22 — always probed
        _mock_response(200, []),  # blocked_by for PR 23 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    issue = next(item for item in items if not item.carries_change)
    assert issue.stage is ItemStage.IN_FLIGHT
    assert issue.change_ref == "22"
    assert issue.additional_change_refs == ["23"]
    assert issue.has_identity_conflict is True


def test_change_refs_by_issue_returns_every_match_not_just_the_first() -> None:
    """§5.6, alpha-engine-config#6316: pre-#6316 this truncated to the first
    match with `dict.setdefault`, silently dropping evidence that a second
    open PR also named the same issue. It must now return every match, in
    encounter order, deduplicated.
    """
    prs = [
        _pr(22, body="Fixes #21"),
        _pr(23, body="Also fixes #21"),
        _pr(24, body="Fixes #30"),
    ]
    refs = _change_refs_by_issue(prs)
    assert refs == {21: ["22", "23"], 30: ["24"]}


def test_change_refs_by_issue_single_match_unchanged() -> None:
    """The ordinary, non-duplicated case: one ref per issue, as before."""
    prs = [_pr(22, body="Fixes #21")]
    assert _change_refs_by_issue(prs) == {21: ["22"]}


def test_change_refs_by_issue_deduplicates_repeated_mentions_in_one_pr() -> None:
    """The same PR mentioning the same issue twice in title+body is one ref,
    not two — a PR cannot duplicate itself as a second in-flight change.
    """
    prs = [_pr(22, title="Fixes #21", body="See also #21")]
    assert _change_refs_by_issue(prs) == {21: ["22"]}


def test_api_404_raises_snapshot_error(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.return_value = _mock_response(404, {"message": "Not Found"})

    with pytest.raises(SnapshotError, match="404"):
        snapshot.fetch("owner/missing")


def test_api_403_raises_snapshot_error(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.return_value = _mock_response(
        403, {"message": "Forbidden"}, headers={"X-RateLimit-Remaining": "0"}
    )

    with pytest.raises(SnapshotError, match="rate limit"):
        snapshot.fetch("owner/repo")


def test_returned_items_are_valid(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(30, labels=["bug"])]),
        _mock_response(
            200,
            [_pr(31, labels=["groom-reviewed"], mergeable=True, ci_state="SUCCESS")],
        ),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),  # blocked_by for PR 31 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert len(items) == 2
    issue, pr = items
    assert issue.id == "30"
    assert issue.carries_change is False
    assert issue.labels == ["bug"]
    assert pr.id == "31"
    assert pr.carries_change is True
    assert pr.labels == ["groom-reviewed"]


def test_pr_from_list_with_null_mergeable_enriched_by_detail_endpoint(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """Regression test for config#6168: list endpoint mergeable=null → per-PR fetch → dirty.

    GitHub's LIST endpoint returns ``mergeable: null`` for every PR.
    The single-PR endpoint returns the real value (e.g. ``mergeable: False``
    and ``mergeable_state: "dirty"`` for a conflicting PR).  After enrichment
    the PR must land in OPEN_DIRTY so the conflict-resolution pass fires.
    """
    list_pr = {
        "number": 99,
        "title": "conflicting PR from list",
        "body": "",
        "draft": False,
        "mergeable": None,
        "mergeable_state": None,
        "labels": [],
    }
    detail_pr = {
        "number": 99,
        "title": "conflicting PR detail",
        "body": "",
        "draft": False,
        "mergeable": False,
        "mergeable_state": "dirty",
        "labels": [],
    }

    # Ordering matches the fetch() call sequence:
    #   1. open issues (paginated)
    #   2. open PRs (paginated)
    #   3. closed issues (paginated)
    #   4. closed PRs (paginated)
    #   5+. per-PR detail GETs (one per PR with mergeable=None)
    mock_client.get.side_effect = [
        _mock_response(200, []),               # open issues
        _mock_response(200, [list_pr]),         # open PRs (list, mergeable=null)
        _mock_response(200, []),               # closed issues
        _mock_response(200, []),               # closed PRs
        _mock_response(200, detail_pr),         # per-PR detail GET
        _mock_response(200, []),               # blocked_by for PR 99 — always probed
    ]

    items, _world = snapshot.fetch("owner/repo")
    pr = items[0]
    assert pr.carries_change is True
    assert pr.change.condition is ChangeCondition.CONFLICTED, (
        "Expected CONFLICTED for mergeable=False / mergeable_state=dirty, "
        f"got {pr.change.condition}"
    )
    assert pr.change.mergeable is False


def test_terminal_items_from_closed_and_merged(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    closed_issue = _issue(40)
    merged_pr = _pr(41, merged_at="2026-01-01T00:00:00Z")

    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, [closed_issue]),
        _mock_response(200, [merged_pr]),
    ]

    _items, world = snapshot.fetch("owner/repo")
    # Bare numbers are preserved for backward compatibility; qualified
    # (owner/name#number) twins are added alongside for every closed item
    # so same-repo ISSUE_TERMINAL/PR_TERMINAL declarations using the
    # qualified convention (config#6320) resolve without a blocked_by probe.
    assert world.terminal_items == {
        "40", "41", "owner/repo#40", "owner/repo#41",
    }


def test_pr_enrichment_failure_logs_warning_and_falls_back(
    snapshot: GitHubSnapshot, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression test for the S110 fix (groomer-PR34): a failing per-PR detail
    fetch must degrade gracefully (null mergeability preserved) AND surface a
    warning in the logs — never a silent bare except-pass.
    """
    list_pr = {
        "number": 98,
        "title": "PR whose detail fetch fails",
        "body": "",
        "draft": False,
        "mergeable": None,
        "mergeable_state": None,
        "labels": [],
    }

    mock_client.get.side_effect = [
        _mock_response(200, []),               # open issues
        _mock_response(200, [list_pr]),        # open PRs (list, mergeable=null)
        _mock_response(200, []),               # closed issues
        _mock_response(200, []),               # closed PRs
        MagicMock(                              # per-PR detail GET — explodes
            status_code=500,
            json=lambda: (_ for _ in ()).throw(
                RuntimeError("detail fetch exploded")
            ),
        ),
        _mock_response(200, []),               # blocked_by for PR 98 — always probed
    ]

    with caplog.at_level(logging.WARNING):
        items, _world = snapshot.fetch("owner/repo")

    pr = items[0]
    assert pr.carries_change is True
    # Fallback preserved: list data (null mergeability) is what survives.
    assert pr.change.mergeable is None
    # The degradation is observable, not silently swallowed.
    assert any(
        "Per-PR mergeability enrichment failed" in record.getMessage()
        and "owner/repo#98" in record.getMessage()
        for record in caplog.records
    )


# --- config#6170: CI state comes from check-runs + statuses, never a REST rollup ---


class _CIStubClient:
    """Stub whose per-PR / check-runs / status responses are scripted by path."""

    def __init__(self, routes):
        self.routes = routes
        self.seen: list[str] = []

    def get(self, path, params=None):  # noqa: D102
        self.seen.append(path)
        for suffix, payload in self.routes.items():
            if path.endswith(suffix):
                return _StubResponse(payload)
        return _StubResponse([] if path.endswith("s") else {})

    def close(self):  # noqa: D102
        pass


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _snapshot_with(routes):
    snap = GitHubSnapshot(token="t", http_client=_CIStubClient(routes))
    return snap


def test_failing_check_run_yields_ci_red_not_pending():
    """A FAILURE conclusion must produce ci_green=False, not None (config#6170).

    Before the fix `_parse_ci_from_rollup` read `statusCheckRollup` from a REST
    payload that never carries it, so every PR landed in OPEN_PENDING_CI and the
    ci_red bucket was permanently empty.
    """
    snap = _snapshot_with({
        "/check-runs": {"check_runs": [
            {"status": "completed", "conclusion": "failure"},
            {"status": "completed", "conclusion": "success"},
        ]},
        "/status": {"total_count": 0, "state": "pending"},
    })
    assert snap._fetch_ci_state("o/r", "abc123") is False


def test_statuses_api_alone_cannot_mask_a_failing_check_run():
    """The union matters and fails in the safe direction.

    Measured on alpha-engine-config#5314 (2026-08-03): /commits/{sha}/status
    reported `success` with total_count=1 while /check-runs reported 4 failures.
    A fix reading only the Statuses API would call that PR green.
    """
    snap = _snapshot_with({
        "/check-runs": {"check_runs": [
            {"status": "completed", "conclusion": "failure"},
        ]},
        "/status": {"total_count": 1, "state": "success"},
    })
    assert snap._fetch_ci_state("o/r", "abc123") is False


def test_all_passing_yields_green():
    snap = _snapshot_with({
        "/check-runs": {"check_runs": [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"},
        ]},
        "/status": {"total_count": 0},
    })
    assert snap._fetch_ci_state("o/r", "abc123") is True


def test_in_progress_run_is_pending_not_green():
    snap = _snapshot_with({
        "/check-runs": {"check_runs": [
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ]},
        "/status": {"total_count": 0},
    })
    assert snap._fetch_ci_state("o/r", "abc123") is None


def test_no_checks_reported_is_pending_not_green():
    """Nothing reported is unobserved, not healthy (groom-sweep-policy §6.2)."""
    snap = _snapshot_with({"/check-runs": {"check_runs": []}, "/status": {"total_count": 0}})
    assert snap._fetch_ci_state("o/r", "abc123") is None


def test_failing_commit_status_is_red_even_with_green_check_runs():
    snap = _snapshot_with({
        "/check-runs": {"check_runs": [{"status": "completed", "conclusion": "success"}]},
        "/status": {"total_count": 2, "state": "failure"},
    })
    assert snap._fetch_ci_state("o/r", "abc123") is False


def test_check_runs_fetch_failure_degrades_to_pending_and_logs(caplog):
    class _Boom:
        def get(self, path, params=None):
            raise RuntimeError("boom")

        def close(self):
            pass

    snap = GitHubSnapshot(token="t", http_client=_Boom())
    with caplog.at_level("WARNING"):
        assert snap._fetch_ci_state("o/r", "deadbeef") is None
    assert "check-runs fetch failed" in caplog.text


# --- config#6320: native issue dependencies, sub-issues, issue fields ---


def test_native_dependency_populates_declared_dependencies(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """Deliverable 1: a blocked_by edge becomes an ISSUE_TERMINAL Dependency,
    replacing what a private harness would otherwise regex-parse from a body.
    """
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(50, total_blocked_by=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(
            200, [_native_blocker(5, repo_full_name="owner/repo")]
        ),  # blocked_by for issue 50
    ]

    items, _world = snapshot.fetch("owner/repo")
    issue = items[0]
    assert issue.declared_dependencies == [
        Dependency(kind=DependencyKind.ISSUE_TERMINAL, target="owner/repo#5")
    ]


def test_native_dependency_cross_repo_identity_from_response_body(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """Regression guard for the config#6320 gotcha: ``GET .../issues/{n}``
    silently follows a transfer, so repo attribution for a dependency's
    target MUST come from the blocked_by entry's own ``repository.full_name``
    — never from the repo the caller requested, and never from the number
    alone (which collides across repos). Here the blocker's number (5)
    matches nothing meaningful in the requested repo; only the qualifying
    ``repository.full_name`` in the response distinguishes it.
    """
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(51, total_blocked_by=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(
            200, [_native_blocker(5, repo_full_name="other-owner/other-repo")]
        ),
    ]

    items, _world = snapshot.fetch("requested-owner/requested-repo")
    issue = items[0]
    dep = issue.declared_dependencies[0]
    # The qualified target names the repo the RESPONSE reported, not the one
    # requested — a test built from the requested repo alone (e.g.
    # "requested-owner/requested-repo#5") would pass even if the adapter
    # silently ignored a cross-repo transfer.
    assert dep.target == "other-owner/other-repo#5"
    assert "requested-owner/requested-repo#5" != dep.target


def test_native_dependency_pr_blocker_kind_is_pr_terminal(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(52, total_blocked_by=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(
            200, [_native_blocker(6, repo_full_name="owner/repo", is_pr=True)]
        ),
    ]

    items, _world = snapshot.fetch("owner/repo")
    dep = items[0].declared_dependencies[0]
    assert dep.kind is DependencyKind.PR_TERMINAL
    assert dep.target == "owner/repo#6"


def test_closed_native_blocker_merged_into_terminal_items(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """A blocker's terminal-ness is read straight off the blocked_by
    response's own ``state`` field — including for a blocker living in a
    DIFFERENT repo than the one fetched, without a second fetch of that
    repo's closed items.
    """
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(53, total_blocked_by=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(
            200,
            [_native_blocker(7, repo_full_name="other-owner/other-repo", state="closed")],
        ),
    ]

    _items, world = snapshot.fetch("owner/repo")
    assert "other-owner/other-repo#7" in world.terminal_items


def test_zero_blocked_by_summary_skips_the_probe(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(54, total_blocked_by=0)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert mock_client.get.call_count == 4
    assert items[0].declared_dependencies == []


def test_malformed_native_dependency_entry_logged_and_skipped(
    snapshot: GitHubSnapshot, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(55, total_blocked_by=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, [{"number": 8, "state": "open"}]),  # no repository key
    ]

    with caplog.at_level(logging.WARNING):
        items, _world = snapshot.fetch("owner/repo")

    assert items[0].declared_dependencies == []
    assert any(
        "malformed native dependency entry" in record.getMessage()
        for record in caplog.records
    )


def test_sub_issues_populate_sub_issue_ids(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(60, sub_issues_total=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(
            200, [_native_blocker(61, repo_full_name="owner/repo")]
        ),  # sub_issues for issue 60
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].sub_issue_ids == ["owner/repo#61"]


def test_sub_issues_not_probed_when_summary_zero(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(62, sub_issues_total=0)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert mock_client.get.call_count == 4
    assert items[0].sub_issue_ids == []


def test_custom_fields_surfaced_generically(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """Deliverable 2 (read side): field values are passed through keyed by
    name, with no interpretation of what any particular field means — that
    is the §3.1 write-boundary chokepoint's job (issue #6309), not this
    adapter's.
    """
    mock_client.get.side_effect = [
        _mock_response(
            200,
            [
                _issue(
                    63,
                    field_values=[
                        {
                            "issue_field_id": 1,
                            "issue_field_name": "Priority",
                            "data_type": "single_select",
                            "value": "High",
                        }
                    ],
                )
            ],
        ),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].custom_fields == {"Priority": "High"}


def test_blocked_by_fetch_failure_degrades_to_no_dependencies_and_logs(
    snapshot: GitHubSnapshot, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(56, total_blocked_by=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(500, {"message": "boom"}),  # blocked_by fetch fails
    ]

    with caplog.at_level(logging.WARNING):
        items, _world = snapshot.fetch("owner/repo")

    assert items[0].declared_dependencies == []
    assert any(
        "blocked_by fetch failed" in record.getMessage() for record in caplog.records
    )


def test_sub_issues_fetch_failure_degrades_to_empty_and_logs(
    snapshot: GitHubSnapshot, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(64, sub_issues_total=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(500, {"message": "boom"}),  # sub_issues fetch fails
    ]

    with caplog.at_level(logging.WARNING):
        items, _world = snapshot.fetch("owner/repo")

    assert items[0].sub_issue_ids == []
    assert any(
        "sub_issues fetch failed" in record.getMessage() for record in caplog.records
    )


def test_malformed_sub_issue_entry_logged_and_skipped(
    snapshot: GitHubSnapshot, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(65, sub_issues_total=1)]),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, []),
        _mock_response(200, [{"number": 66}]),  # no repository key
    ]

    with caplog.at_level(logging.WARNING):
        items, _world = snapshot.fetch("owner/repo")

    assert items[0].sub_issue_ids == []
    assert any(
        "sub-issue entry" in record.getMessage() for record in caplog.records
    )


def test_fetch_issue_field_conformance_reports_usage_against_cap(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    """Deliverable 2 (budgeting) / closes-when: a conformance row reports
    issue-field slots used against the 25 cap."""
    mock_client.get.side_effect = [
        _mock_response(
            200,
            [
                {"id": 1, "name": "Priority"},
                {"id": 2, "name": "Start date"},
                {"id": 3, "name": "Target date"},
                {"id": 4, "name": "Effort"},
            ],
        ),
    ]

    conformance = snapshot.fetch_issue_field_conformance("nousergon")
    assert conformance == IssueFieldConformance(
        org="nousergon",
        cap=25,
        field_names=["Effort", "Priority", "Start date", "Target date"],
    )
    assert conformance.used == 4
    assert conformance.free == 21
    assert conformance.conformance_row() == (
        "issue-fields[nousergon]: 4/25 used (21 free) — "
        "Effort, Priority, Start date, Target date"
    )
