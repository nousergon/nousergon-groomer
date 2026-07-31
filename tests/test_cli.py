"""Contract tests for the CLI entry point (issue #25)."""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nousergon_groomer.cli import (
    _COL_DISPOSITION,
    _COL_ID,
    _COL_KIND,
    _COL_STATE,
    format_dry_run_table,
    main,
)
from nousergon_groomer.config import load_config, write_default_config
from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.github_executor import ExecutorResult
from nousergon_groomer.models import (
    Dependency,
    DependencyKind,
    Item,
    ItemKind,
    ItemState,
)
from nousergon_groomer.observed_gen import GenerationStore
from nousergon_groomer.reconciler import Reconciler, ReconcilerConfig, ReconcilerResult


def _minimal_config_yaml(wip_ceiling: int = 5) -> str:
    return f"wip_ceiling: {wip_ceiling}\n"


def _fixture_items() -> tuple[list[Item], ObservedWorld]:
    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://b/k")
    items = [
        Item(
            id="i1",
            kind=ItemKind.ISSUE,
            state=ItemState.OPEN_ISSUE_ACTIONABLE,
            title="Actionable issue",
        ),
        Item(
            id="p1",
            kind=ItemKind.PR,
            state=ItemState.OPEN_CLEAN_GREEN,
            title="Green PR",
            mergeable=True,
            ci_green=True,
        ),
        Item(
            id="p2",
            kind=ItemKind.PR,
            state=ItemState.OPEN_RED_CI,
            title="Red CI PR",
            ci_green=False,
        ),
        Item(
            id="i2",
            kind=ItemKind.ISSUE,
            state=ItemState.OPEN_ISSUE_ACTIONABLE,
            title="Blocked issue",
            declared_dependencies=[dep],
        ),
    ]
    world = ObservedWorld(
        s3_objects=set(),
        s3_prefixes=set(),
        terminal_items=set(),
        pipeline_runs=set(),
        absent_labels=set(),
    )
    return items, world


def _reconcile_fixture(items: list[Item], world: ObservedWorld) -> ReconcilerResult:
    return Reconciler(ReconcilerConfig(wip_ceiling=5)).reconcile(
        items, world, store=GenerationStore()
    )


def test_init_creates_valid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    output = tmp_path / "groomer.yaml"
    main(["init", "--output", str(output)])
    captured = capsys.readouterr()
    assert output.is_file()
    assert "Wrote starter config" in captured.out
    cfg = load_config(output)
    assert cfg.wip_ceiling >= 1


def test_init_default_output_message(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    main(["init"])
    captured = capsys.readouterr()
    assert (tmp_path / ".groomer.yaml").is_file()
    assert "GROOMER_GITHUB_TOKEN" in captured.out


def test_dry_run_prints_table_and_skips_executor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "groomer.yaml"
    config_path.write_text(_minimal_config_yaml(), encoding="utf-8")
    items, world = _fixture_items()
    monkeypatch.setenv("GROOMER_GITHUB_TOKEN", "test-token")

    with (
        patch("nousergon_groomer.cli.GitHubSnapshot") as mock_snapshot_cls,
        patch("nousergon_groomer.cli.GitHubExecutor") as mock_executor_cls,
    ):
        mock_snapshot_cls.return_value.fetch.return_value = (items, world)
        mock_snapshot_cls.return_value.close = MagicMock()
        main(
            [
                "run",
                "--repo",
                "nousergon/test",
                "--config",
                str(config_path),
                "--dry-run",
            ]
        )

    mock_executor_cls.assert_not_called()
    captured = capsys.readouterr()
    assert "ID" in captured.out
    assert "DISPOSITION" in captured.out
    assert "Summary:" in captured.out
    assert "ACT" in captured.out
    assert "BLOCKED" in captured.out


def test_run_calls_executor_when_not_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "groomer.yaml"
    config_path.write_text(_minimal_config_yaml(), encoding="utf-8")
    items, world = _fixture_items()
    monkeypatch.setenv("GROOMER_GITHUB_TOKEN", "test-token")

    exec_result = ExecutorResult(executed=["p2"], skipped=["i1", "p1", "i2"], errors=[], dry_run=False)

    with (
        patch("nousergon_groomer.cli.GitHubSnapshot") as mock_snapshot_cls,
        patch("nousergon_groomer.cli.GitHubExecutor") as mock_executor_cls,
    ):
        mock_snapshot_cls.return_value.fetch.return_value = (items, world)
        mock_snapshot_cls.return_value.close = MagicMock()
        mock_executor = mock_executor_cls.return_value
        mock_executor.execute.return_value = exec_result
        mock_executor.close = MagicMock()
        main(
            [
                "run",
                "--repo",
                "nousergon/test",
                "--config",
                str(config_path),
            ]
        )

    mock_executor_cls.assert_called_once()
    mock_executor.execute.assert_called_once()
    captured = capsys.readouterr()
    assert "Executed: 1" in captured.out
    assert "p2" in captured.out


def test_missing_repo_raises_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--config", "missing.yaml"])
    assert exc_info.value.code != 0


def test_missing_token_raises_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "groomer.yaml"
    config_path.write_text(_minimal_config_yaml(), encoding="utf-8")
    monkeypatch.delenv("GROOMER_GITHUB_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "run",
                "--repo",
                "nousergon/test",
                "--config",
                str(config_path),
                "--dry-run",
            ]
        )
    assert exc_info.value.code == 1


def test_missing_token_error_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    config_path = tmp_path / "groomer.yaml"
    config_path.write_text(_minimal_config_yaml(), encoding="utf-8")
    monkeypatch.delenv("GROOMER_GITHUB_TOKEN", raising=False)

    with pytest.raises(SystemExit):
        main(
            [
                "run",
                "--repo",
                "nousergon/test",
                "--config",
                str(config_path),
                "--dry-run",
            ]
        )
    captured = capsys.readouterr()
    assert "GROOMER_GITHUB_TOKEN" in captured.err
    assert "error:" in captured.err


def test_missing_config_raises_clear_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GROOMER_GITHUB_TOKEN", "test-token")
    missing = "/no/such/groomer.yaml"

    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--repo", "nousergon/test", "--config", missing, "--dry-run"])
    assert exc_info.value.code == 1


def test_missing_config_error_message(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GROOMER_GITHUB_TOKEN", "test-token")
    missing = "/no/such/groomer.yaml"

    with pytest.raises(SystemExit):
        main(["run", "--repo", "nousergon/test", "--config", missing, "--dry-run"])
    captured = capsys.readouterr()
    assert "Config file not found" in captured.err
    assert missing in captured.err


def test_dry_run_table_format_is_parseable():
    items, world = _fixture_items()
    result = _reconcile_fixture(items, world)
    table = format_dry_run_table(items, result)

    lines = [line for line in table.splitlines() if line.strip()]
    header = lines[0]
    assert header.startswith("ID")
    assert "KIND" in header
    assert "STATE" in header
    assert "DISPOSITION" in header
    assert "ACTION" in header
    assert "REASON" in header

    data_lines = [line for line in lines[1:] if not line.startswith("Summary:")]
    assert len(data_lines) == len(items)

    summary = lines[-1]
    assert re.match(
        r"Summary: \d+ ACT, \d+ BLOCKED, \d+ TERMINAL, \d+ UNDECIDABLE",
        summary,
    )

    for line in data_lines:
        item_id = line[:_COL_ID].strip()
        kind = line[_COL_ID : _COL_ID + _COL_KIND].strip()
        disposition = line[
            _COL_ID + _COL_KIND + _COL_STATE : _COL_ID + _COL_KIND + _COL_STATE + _COL_DISPOSITION
        ].strip()
        assert item_id in {"i1", "i2", "p1", "p2"}
        assert kind in {"issue", "pr"}
        assert disposition in {"ACT", "BLOCKED", "TERMINAL", "UNDECIDABLE"}


def test_dry_run_table_matches_reconciler_counts():
    items, world = _fixture_items()
    result = _reconcile_fixture(items, world)
    table = format_dry_run_table(items, result)
    summary_line = [line for line in table.splitlines() if line.startswith("Summary:")][0]
    assert f"{result.acted} ACT" in summary_line
    assert f"{result.blocked} BLOCKED" in summary_line
    assert f"{result.terminal} TERMINAL" in summary_line
    assert f"{result.undecidable} UNDECIDABLE" in summary_line


def test_write_default_config_via_init_matches_load_config(tmp_path: Path):
    path = tmp_path / "out.yaml"
    write_default_config(path)
    cfg = load_config(path)
    assert cfg.wip_ceiling == 5
