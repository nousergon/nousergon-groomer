"""CLI entry point — ``groomer run`` and ``groomer init``."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from .config import GroomerConfig, load_config, write_default_config
from .github_executor import ExecutorResult, GitHubExecutor
from .github_snapshot import GitHubSnapshot
from .model_provider import ModelProvider, OpenAICompatibleProvider
from .models import Item
from .observed_gen import GenerationStore
from .reconciler import Reconciler, ReconcilerConfig, ReconcilerResult

_COL_ID = 12
_COL_STAGE = 12
_COL_CHANGE = 14
_COL_DISPOSITION = 13
_COL_ACTION = 16


def _read_token(token_env: str) -> str:
    token = os.environ.get(token_env)
    if not token or not str(token).strip():
        raise ValueError(
            f"GitHub token not found: set the {token_env!r} environment variable "
            f"or pass --token-env to choose a different variable name"
        )
    return str(token).strip()


def _load_groomer_config(config_path: Path) -> GroomerConfig:
    if not config_path.is_file():
        raise ValueError(
            f"Config file not found: {config_path}. "
            f"Run `groomer init --output {config_path}` to create a starter config."
        )
    return load_config(config_path)


def _build_model_provider(config: GroomerConfig) -> Optional[ModelProvider]:
    if config.model is None:
        return None
    api_key = os.environ.get(config.model.api_key_env)
    if not api_key or not str(api_key).strip():
        raise ValueError(
            f"Model API key not found: set the {config.model.api_key_env!r} "
            f"environment variable (configured in model.api_key_env)"
        )
    return OpenAICompatibleProvider(
        base_url=config.model.base_url,
        api_key=str(api_key).strip(),
        model=config.model.model,
    )


def format_dry_run_table(
    items: list[Item],
    result: ReconcilerResult,
) -> str:
    """Render reconciler dispositions as a human-readable table.

    STAGE is the item's **effective** stage as the reconciler resolved it — a
    recorded ``proposed`` shows as ``ready`` once nothing blocks it — so the
    table shows where each item is rather than what a harness wrote down.
    """
    item_by_id = {item.id: item for item in items}
    header = (
        f"{'ID':<{_COL_ID}}"
        f"{'STAGE':<{_COL_STAGE}}"
        f"{'CHANGE':<{_COL_CHANGE}}"
        f"{'DISPOSITION':<{_COL_DISPOSITION}}"
        f"{'ACTION':<{_COL_ACTION}}"
        "REASON"
    )
    lines = [header]
    for item_disp in result.items:
        item = item_by_id.get(item_disp.item_id)
        stage = item_disp.stage.value
        if item is None:
            change = "?"
        elif item.change is not None:
            change = item.change.condition.value
        else:
            change = "—"
        disposition = item_disp.disposition
        action = disposition.action if disposition.action else "—"
        reason = disposition.reason if disposition.reason else "—"
        lines.append(
            f"{item_disp.item_id:<{_COL_ID}}"
            f"{stage:<{_COL_STAGE}}"
            f"{change:<{_COL_CHANGE}}"
            f"{disposition.kind.name:<{_COL_DISPOSITION}}"
            f"{action:<{_COL_ACTION}}"
            f"{reason}"
        )
    lines.append("")
    lines.append(
        "Summary: "
        f"{result.acted} ACT, "
        f"{result.blocked} BLOCKED, "
        f"{result.terminal} TERMINAL, "
        f"{result.undecidable} UNDECIDABLE"
    )
    return "\n".join(lines)


def print_executor_summary(result: ExecutorResult) -> None:
    """Print a concise summary of executor outcomes."""
    print(f"Executed: {len(result.executed)}")
    if result.executed:
        print(f"  {', '.join(result.executed)}")
    print(f"Skipped: {len(result.skipped)}")
    if result.skipped:
        print(f"  {', '.join(result.skipped)}")
    print(f"Errors: {len(result.errors)}")
    for err in result.errors:
        print(f"  {err.item_id} ({err.action or 'unknown'}): {err.error}")


def cmd_init(output: Path) -> None:
    """Write a starter config file."""
    write_default_config(output)
    print(f"Wrote starter config to {output}")
    print("Edit the config to match your lanes and gates, then set:")
    print("  - GROOMER_GITHUB_TOKEN (or your --token-env variable)")
    print("  - model.api_key_env if you configure a model provider")


def cmd_run(
    *,
    repo: str,
    config_path: Path,
    dry_run: bool,
    token_env: str,
) -> None:
    """Run one groomer pass: snapshot → reconcile → (optionally) execute."""
    token = _read_token(token_env)
    config = _load_groomer_config(config_path)

    snapshot = GitHubSnapshot(token)
    try:
        items, world = snapshot.fetch(repo)
    finally:
        snapshot.close()

    reconciler = Reconciler(ReconcilerConfig(wip_ceiling=config.wip_ceiling))
    result = reconciler.reconcile(items, world, GenerationStore())

    if dry_run:
        print(format_dry_run_table(items, result))
        return

    model_provider = _build_model_provider(config)
    model_name = config.model.model if config.model is not None else "default"
    executor = GitHubExecutor(token, model_provider, model=model_name)
    try:
        exec_result = executor.execute(result, items, repo=repo)
    finally:
        executor.close()
    print_executor_summary(exec_result)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="groomer",
        description="Deterministic backlog groomer — snapshot, reconcile, execute.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Write a starter config file")
    init_parser.add_argument(
        "--output",
        default=".groomer.yaml",
        help="Path for the generated config (default: .groomer.yaml)",
    )

    run_parser = subparsers.add_parser("run", help="Run one groomer pass")
    run_parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository (owner/name)",
    )
    run_parser.add_argument(
        "--config",
        default=None,
        help="Config file path (default: .groomer.yaml)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print dispositions without executing actions",
    )
    run_parser.add_argument(
        "--token-env",
        default="GROOMER_GITHUB_TOKEN",
        help="Environment variable holding the GitHub token (default: GROOMER_GITHUB_TOKEN)",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            cmd_init(Path(args.output))
            return
        if args.command == "run":
            config_path = Path(args.config) if args.config else Path(".groomer.yaml")
            cmd_run(
                repo=args.repo,
                config_path=config_path,
                dry_run=args.dry_run,
                token_env=args.token_env,
            )
            return
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
