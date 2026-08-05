"""Contract tests for the GitHub executor adapter (issue #23)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nousergon_groomer.github_executor import (
    BLOCKED_CHAIN_MARKER,
    ExecutorAbortError,
    GitHubExecutor,
)
from nousergon_groomer.models import (
    Change,
    ChangeCondition,
    Disposition,
    DispositionKind,
    Item,
    ItemStage,
)
from nousergon_groomer.reconciler import ItemDisposition, ReconcilerResult


def _mock_response(status_code: int, json_data: object | None = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data if json_data is not None else {}
    response.text = text or str(json_data)
    return response


def _issue(item_id: str = "1", **kw) -> Item:
    """An item with no change yet — the shape ``create_pr`` acts on."""
    defaults = {
        "id": item_id,
        "stage": ItemStage.PROPOSED,
        "title": f"Issue {item_id}",
    }
    defaults.update(kw)
    return Item(**defaults)


def _pr(item_id: str = "10", **kw) -> Item:
    """An item in flight — the shape every change-scoped action acts on."""
    change = Change(
        ref=item_id,
        condition=kw.pop("condition", ChangeCondition.CLEAN),
        mergeable=kw.pop("mergeable", True),
        ci_green=kw.pop("ci_green", True),
    )
    defaults = {
        "id": item_id,
        "stage": kw.pop("stage", ItemStage.IN_FLIGHT),
        "title": f"PR {item_id}",
        "change": change,
    }
    defaults.update(kw)
    return Item(**defaults)


def _result(*dispositions: tuple[str, Disposition]) -> ReconcilerResult:
    items = [
        ItemDisposition(item_id=item_id, disposition=disposition)
        for item_id, disposition in dispositions
    ]
    acted = sum(1 for _, d in dispositions if d.kind is DispositionKind.ACT)
    blocked = sum(1 for _, d in dispositions if d.kind is DispositionKind.BLOCKED)
    terminal = sum(1 for _, d in dispositions if d.kind is DispositionKind.TERMINAL)
    undecidable = sum(1 for _, d in dispositions if d.kind is DispositionKind.UNDECIDABLE)
    return ReconcilerResult(
        items=items,
        generation=1,
        enumerated=len(items),
        acted=acted,
        blocked=blocked,
        terminal=terminal,
        undecidable=undecidable,
        skipped=0,
        admitted=0,
        admission_denied=0,
    )


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_model() -> MagicMock:
    model = MagicMock()
    model.complete.return_value = "model output"
    return model


@pytest.fixture
def executor(mock_client: MagicMock, mock_model: MagicMock) -> GitHubExecutor:
    return GitHubExecutor(
        token="test-token",
        model_provider=mock_model,
        http_client=mock_client,
    )


def test_automerge_calls_merge_api(executor: GitHubExecutor, mock_client: MagicMock) -> None:
    mock_client.put.return_value = _mock_response(200, {"merged": True})
    item = _pr("42")
    result = _result(("42", Disposition(kind=DispositionKind.ACT, action="automerge")))

    out = executor.execute(result, [item], repo="owner/repo")

    mock_client.put.assert_called_once_with(
        "/repos/owner/repo/pulls/42/merge",
        json={"merge_method": "squash"},
    )
    assert out.executed == ["42"]
    assert out.errors == []


def test_create_pr_calls_create_api(
    executor: GitHubExecutor, mock_client: MagicMock, mock_model: MagicMock
) -> None:
    mock_client.post.return_value = _mock_response(201, {"number": 99})
    item = _issue("7")
    result = _result(("7", Disposition(kind=DispositionKind.ACT, action="create_pr")))

    out = executor.execute(result, [item], repo="owner/repo")

    mock_model.complete.assert_called_once()
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert call.args[0] == "/repos/owner/repo/pulls"
    payload = call.kwargs["json"]
    assert payload["title"] == "Issue 7"
    assert payload["head"] == "feat/issue-7"
    assert payload["base"] == "main"
    assert payload["body"] == "model output"
    assert out.executed == ["7"]


def test_create_pr_without_model_uses_minimal_body(mock_client: MagicMock) -> None:
    mock_client.post.return_value = _mock_response(201, {"number": 99})
    executor = GitHubExecutor(token="test-token", http_client=mock_client)
    item = _issue("8")
    result = _result(("8", Disposition(kind=DispositionKind.ACT, action="create_pr")))

    out = executor.execute(result, [item], repo="owner/repo")

    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["body"] == ""
    assert out.executed == ["8"]


def test_fix_ci_calls_model_provider(
    executor: GitHubExecutor, mock_client: MagicMock, mock_model: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(
            200,
            {
                "title": "Fix tests",
                "head": {"ref": "fix-ci", "sha": "abc123"},
                "base": {"ref": "main"},
                "mergeable": True,
            },
        ),
        _mock_response(404),
        _mock_response(
            200,
            {
                "title": "Fix tests",
                "head": {"ref": "fix-ci", "sha": "abc123"},
                "base": {"ref": "main"},
                "mergeable": True,
            },
        ),
        _mock_response(200, {"check_runs": [{"name": "ci", "conclusion": "failure"}]}),
        _mock_response(
            200,
            {
                "title": "Fix tests",
                "head": {"ref": "fix-ci", "sha": "abc123"},
                "base": {"ref": "main"},
                "mergeable": True,
            },
        ),
        _mock_response(200, {"tree": {"sha": "tree123"}}),
    ]
    mock_client.post.return_value = _mock_response(201, {"sha": "commit456"})
    mock_client.patch.return_value = _mock_response(200, {})
    item = _pr("15", condition=ChangeCondition.CI_RED, ci_green=False)
    result = _result(("15", Disposition(kind=DispositionKind.ACT, action="fix_ci")))

    out = executor.execute(result, [item], repo="owner/repo")

    mock_model.complete.assert_called_once()
    assert "Fix failing CI" in mock_model.complete.call_args.args[0]
    assert out.executed == ["15"]


def test_resolve_conflicts_calls_model_provider(
    executor: GitHubExecutor, mock_client: MagicMock, mock_model: MagicMock
) -> None:
    mock_client.get.side_effect = [
        _mock_response(
            200,
            {
                "title": "Conflict PR",
                "head": {"ref": "feature", "sha": "abc123"},
                "base": {"ref": "main"},
                "mergeable": False,
            },
        ),
        _mock_response(
            200,
            {
                "title": "Conflict PR",
                "head": {"ref": "feature", "sha": "abc123"},
                "base": {"ref": "main"},
                "mergeable": False,
            },
        ),
        _mock_response(200, {"tree": {"sha": "tree123"}}),
    ]
    mock_client.post.return_value = _mock_response(201, {"sha": "commit456"})
    mock_client.patch.return_value = _mock_response(200, {})
    item = _pr("16", condition=ChangeCondition.CONFLICTED, mergeable=False)
    result = _result(("16", Disposition(kind=DispositionKind.ACT, action="resolve_conflicts")))

    out = executor.execute(result, [item], repo="owner/repo")

    mock_model.complete.assert_called_once()
    assert "Resolve merge conflicts" in mock_model.complete.call_args.args[0]
    assert out.executed == ["16"]


def test_advance_draft_calls_model_provider(
    executor: GitHubExecutor, mock_client: MagicMock, mock_model: MagicMock
) -> None:
    mock_model.complete.return_value = "READY: labels look good"
    mock_client.patch.return_value = _mock_response(200, {"draft": False})
    item = _pr("17", condition=ChangeCondition.DRAFT)
    result = _result(("17", Disposition(kind=DispositionKind.ACT, action="advance_draft")))

    out = executor.execute(result, [item], repo="owner/repo")

    mock_model.complete.assert_called_once()
    mock_client.patch.assert_called_once_with(
        "/repos/owner/repo/pulls/17",
        json={"draft": False},
    )
    assert out.executed == ["17"]


def test_execute_routes_all_act_handlers(
    executor: GitHubExecutor, mock_client: MagicMock, mock_model: MagicMock
) -> None:
    with patch.object(executor, "_automerge") as automerge, patch.object(
        executor, "_create_pr"
    ) as create_pr, patch.object(executor, "_fix_ci") as fix_ci, patch.object(
        executor, "_resolve_conflicts"
    ) as resolve_conflicts, patch.object(executor, "_advance_draft") as advance_draft:
        items = [
            _pr("1"),
            _issue("2"),
            _pr("3", condition=ChangeCondition.CI_RED),
            _pr("4", condition=ChangeCondition.CONFLICTED),
            _pr("5", condition=ChangeCondition.DRAFT),
        ]
        result = _result(
            ("1", Disposition(kind=DispositionKind.ACT, action="automerge")),
            ("2", Disposition(kind=DispositionKind.ACT, action="create_pr")),
            ("3", Disposition(kind=DispositionKind.ACT, action="fix_ci")),
            ("4", Disposition(kind=DispositionKind.ACT, action="resolve_conflicts")),
            ("5", Disposition(kind=DispositionKind.ACT, action="advance_draft")),
        )

        out = executor.execute(result, items, repo="owner/repo")

        automerge.assert_called_once_with(items[0], "owner/repo")
        create_pr.assert_called_once_with(items[1], "owner/repo")
        fix_ci.assert_called_once_with(items[2], "owner/repo")
        resolve_conflicts.assert_called_once_with(items[3], "owner/repo")
        advance_draft.assert_called_once_with(items[4], "owner/repo")
        assert out.executed == ["1", "2", "3", "4", "5"]


def test_dry_run_produces_no_side_effects(
    mock_client: MagicMock, mock_model: MagicMock
) -> None:
    executor = GitHubExecutor(
        token="test-token",
        model_provider=mock_model,
        dry_run=True,
        http_client=mock_client,
    )
    items = [
        _pr("1"),
        _issue("2"),
        _pr("3", condition=ChangeCondition.CI_RED),
    ]
    result = _result(
        ("1", Disposition(kind=DispositionKind.ACT, action="automerge")),
        ("2", Disposition(kind=DispositionKind.ACT, action="create_pr")),
        ("3", Disposition(kind=DispositionKind.ACT, action="fix_ci")),
    )

    with patch("builtins.print") as mock_print:
        out = executor.execute(result, items, repo="owner/repo")

    mock_client.put.assert_not_called()
    mock_client.post.assert_not_called()
    mock_client.get.assert_not_called()
    mock_client.patch.assert_not_called()
    mock_model.complete.assert_not_called()
    assert out.dry_run is True
    assert out.executed == ["1", "2", "3"]
    assert mock_print.call_count == 3


def test_blocked_disposition_posts_new_chain_comment(
    executor: GitHubExecutor, mock_client: MagicMock
) -> None:
    """A BLOCKED item with no existing marker comment gets a new one posted
    naming its full chain (alpha-engine-config#6500), not silently skipped."""
    item = _issue("1")
    mock_client.get.return_value = _mock_response(200, [])  # no existing comments
    mock_client.post.return_value = _mock_response(201, {"id": 555})
    result = _result(
        (
            "1",
            Disposition(
                kind=DispositionKind.BLOCKED,
                reason="blocked on: issue_terminal:i2 -> s3_object:s3://b/root",
                blocking_chain=["issue_terminal:i2", "s3_object:s3://b/root"],
            ),
        ),
    )

    out = executor.execute(result, [item], repo="owner/repo")

    mock_client.get.assert_called_once_with("/repos/owner/repo/issues/1/comments")
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert call.args[0] == "/repos/owner/repo/issues/1/comments"
    body = call.kwargs["json"]["body"]
    assert BLOCKED_CHAIN_MARKER in body
    # Both links of the chain are named, not just the direct dependency.
    assert "issue_terminal:i2" in body
    assert "s3_object:s3://b/root" in body
    mock_client.put.assert_not_called()
    assert out.blocked_surfaced == ["1"]
    assert out.skipped == []
    assert out.executed == []
    assert out.errors == []


def test_blocked_disposition_updates_existing_marker_comment(
    executor: GitHubExecutor, mock_client: MagicMock
) -> None:
    """A second pass over an unchanged BLOCKED item edits the marker comment
    in place rather than posting a duplicate (§5.3 idempotency)."""
    item = _issue("1")
    mock_client.get.return_value = _mock_response(
        200,
        [
            {"id": 42, "body": "unrelated comment"},
            {"id": 99, "body": f"{BLOCKED_CHAIN_MARKER}\nold chain"},
        ],
    )
    mock_client.patch.return_value = _mock_response(200, {"id": 99})
    result = _result(
        (
            "1",
            Disposition(
                kind=DispositionKind.BLOCKED,
                reason="blocked on: s3_object:s3://b/k",
                blocking_chain=["s3_object:s3://b/k"],
            ),
        ),
    )

    out = executor.execute(result, [item], repo="owner/repo")

    mock_client.patch.assert_called_once()
    call = mock_client.patch.call_args
    assert call.args[0] == "/repos/owner/repo/issues/comments/99"
    assert BLOCKED_CHAIN_MARKER in call.kwargs["json"]["body"]
    mock_client.post.assert_not_called()
    assert out.blocked_surfaced == ["1"]


def test_blocked_disposition_dry_run_writes_nothing(mock_client: MagicMock) -> None:
    executor = GitHubExecutor(token="test-token", dry_run=True, http_client=mock_client)
    item = _issue("1")
    result = _result(
        ("1", Disposition(kind=DispositionKind.BLOCKED, reason="blocked on dep")),
    )

    with patch("builtins.print") as mock_print:
        out = executor.execute(result, [item], repo="owner/repo")

    mock_client.get.assert_not_called()
    mock_client.post.assert_not_called()
    mock_client.patch.assert_not_called()
    assert out.blocked_surfaced == []
    assert out.skipped == ["1"]
    mock_print.assert_called_once()
    assert "blocked on dep" in mock_print.call_args.args[0]


def test_blocked_disposition_missing_item_recorded_as_error(
    executor: GitHubExecutor, mock_client: MagicMock
) -> None:
    """A BLOCKED disposition for an id not in ``items`` cannot be surfaced —
    recorded as an error, matching the ACT-path guard for the same case."""
    result = _result(
        ("missing", Disposition(kind=DispositionKind.BLOCKED, reason="blocked on dep")),
    )

    out = executor.execute(result, [], repo="owner/repo")

    mock_client.get.assert_not_called()
    assert out.blocked_surfaced == []
    assert len(out.errors) == 1
    assert out.errors[0].item_id == "missing"
    assert out.errors[0].action == "surface_blocked_chain"


def test_terminal_dispositions_are_skipped(executor: GitHubExecutor, mock_client: MagicMock) -> None:
    item = _pr("1", stage=ItemStage.MERGED)
    result = _result(
        ("1", Disposition(kind=DispositionKind.TERMINAL, reason="merged")),
    )

    out = executor.execute(result, [item], repo="owner/repo")

    mock_client.put.assert_not_called()
    assert out.skipped == ["1"]


def test_undecidable_dispositions_recorded_as_errors(
    executor: GitHubExecutor, mock_client: MagicMock
) -> None:
    item = _issue("1")
    result = _result(
        (
            "1",
            Disposition(
                kind=DispositionKind.UNDECIDABLE,
                reason="undecidable dependency",
            ),
        ),
    )

    out = executor.execute(result, [item], repo="owner/repo")

    mock_client.put.assert_not_called()
    assert out.skipped == []
    assert len(out.errors) == 1
    assert out.errors[0].item_id == "1"
    assert "undecidable" in out.errors[0].error


def test_fix_ci_without_model_provider_is_skipped(mock_client: MagicMock) -> None:
    executor = GitHubExecutor(token="test-token", http_client=mock_client)
    item = _pr("20", condition=ChangeCondition.CI_RED, ci_green=False)
    result = _result(("20", Disposition(kind=DispositionKind.ACT, action="fix_ci")))

    out = executor.execute(result, [item], repo="owner/repo")

    mock_client.get.assert_not_called()
    assert out.skipped == ["20"]
    assert out.executed == []


def test_per_item_api_failure_recorded_not_raised(
    executor: GitHubExecutor, mock_client: MagicMock
) -> None:
    mock_client.put.return_value = _mock_response(422, text="merge conflict")
    items = [_pr("1"), _pr("2")]
    result = _result(
        ("1", Disposition(kind=DispositionKind.ACT, action="automerge")),
        ("2", Disposition(kind=DispositionKind.ACT, action="automerge")),
    )
    mock_client.put.side_effect = [
        _mock_response(422, text="merge conflict"),
        _mock_response(200, {"merged": True}),
    ]

    out = executor.execute(result, items, repo="owner/repo")

    assert out.executed == ["2"]
    assert len(out.errors) == 1
    assert out.errors[0].item_id == "1"
    assert out.errors[0].action == "automerge"


def test_auth_error_raises_executor_abort_error(mock_client: MagicMock) -> None:
    executor = GitHubExecutor(token="bad-token", http_client=mock_client)
    mock_client.put.return_value = _mock_response(401, text="Bad credentials")
    item = _pr("1")
    result = _result(("1", Disposition(kind=DispositionKind.ACT, action="automerge")))

    with pytest.raises(ExecutorAbortError, match="authentication failed"):
        executor.execute(result, [item], repo="owner/repo")


def test_missing_token_raises_value_error(mock_client: MagicMock) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        GitHubExecutor(token="", http_client=mock_client)
