"""Contract tests for the GitHub snapshot adapter (issue #22)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from nousergon_groomer.github_snapshot import GitHubSnapshot, SnapshotError
from nousergon_groomer.models import ItemKind, ItemState


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
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": label} for label in (labels or [])],
    }


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
    ]

    items, _world = snapshot.fetch("owner/repo")
    pr = next(item for item in items if item.kind is ItemKind.PR)
    assert pr.state is ItemState.OPEN_CLEAN_GREEN
    assert pr.ci_green is True
    assert pr.mergeable is True


def test_draft_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(11, draft=True)]),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    pr = items[0]
    assert pr.state is ItemState.OPEN_DRAFT
    assert pr.is_draft is True


def test_dirty_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(12, mergeable=False, ci_state="SUCCESS")]),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].state is ItemState.OPEN_DIRTY


def test_red_ci_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(13, mergeable=True, ci_state="FAILURE")]),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].state is ItemState.OPEN_RED_CI
    assert items[0].ci_green is False


def test_pending_ci_pr_state(snapshot: GitHubSnapshot, mock_client: MagicMock) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, []),
        _mock_response(200, [_pr(14, mergeable=True, ci_state="PENDING")]),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert items[0].state is ItemState.OPEN_PENDING_CI
    assert items[0].ci_green is None


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
    assert issue.kind is ItemKind.ISSUE
    assert issue.state is ItemState.OPEN_ISSUE_ACTIONABLE


def test_issue_with_linked_open_pr_is_waiting(
    snapshot: GitHubSnapshot, mock_client: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(200, [_issue(21, title="Blocked issue")]),
        _mock_response(200, [_pr(22, body="Fixes #21")]),
        _mock_response(200, []),
        _mock_response(200, []),
    ]

    items, _world = snapshot.fetch("owner/repo")
    issue = next(item for item in items if item.kind is ItemKind.ISSUE)
    assert issue.state is ItemState.OPEN_ISSUE_WAITING


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
    ]

    items, _world = snapshot.fetch("owner/repo")
    assert len(items) == 2
    issue, pr = items
    assert issue.id == "30"
    assert issue.kind is ItemKind.ISSUE
    assert issue.labels == ["bug"]
    assert pr.id == "31"
    assert pr.kind is ItemKind.PR
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
    ]

    items, _world = snapshot.fetch("owner/repo")
    pr = items[0]
    assert pr.kind is ItemKind.PR
    assert pr.state is ItemState.OPEN_DIRTY, (
        f"Expected OPEN_DIRTY for mergeable=False / mergeable_state=dirty, "
        f"got {pr.state}"
    )
    assert pr.mergeable is False


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
    assert world.terminal_items == {"40", "41"}


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
    ]

    with caplog.at_level(logging.WARNING):
        items, _world = snapshot.fetch("owner/repo")

    pr = items[0]
    assert pr.kind is ItemKind.PR
    # Fallback preserved: list data (null mergeability) is what survives.
    assert pr.mergeable is None
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
