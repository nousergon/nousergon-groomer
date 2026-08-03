"""nousergon-groomer — deterministic backlog groomer core.

The fixture-runnable control plane for the autonomous backlog-and-PR
maintenance loop. Implements the properties specified in
``nous-ergon-ops/policies/groom-sweep-policy.md`` (the "groom-sweep policy"):

- §3   — blocked-ness is derived from declared dependencies, never asserted
- §3.1 — dependencies are validated at the write boundary
- §3.3 — spec (declaration) and status (evaluated disposition) are separate
- §3.4 — dependencies compose transitively
- §4   — admission is bounded by a WIP ceiling
- §4.3 — a PR is opened only for an unblocked issue
- §5.1 — one total reconciliation function over one enumeration
- §5.4 — the control loop is deterministic; the model is at the leaf
- §5.5 — observed-generation skips unchanged items
- §8.1 — the core runs on a laptop against fixtures, no credentials

This package contains NO network calls, NO GitHub client, NO model calls.
It is pure logic over recorded state. The operational harness (dispatch,
merge execution, PAT) lives in the private layer.
"""
from __future__ import annotations

from .config import (
    GateFamily,
    GroomerConfig,
    LaneConfig,
    ModelConfig,
    load_config,
    write_default_config,
)
from .disposition import compute_disposition
from .github_executor import (
    ExecutorAbortError,
    ExecutorError,
    ExecutorResult,
    GitHubExecutor,
)
from .github_snapshot import GitHubSnapshot, SnapshotError
from .model_provider import ModelError, ModelProvider, OpenAICompatibleProvider
from .models import (
    Dependency,
    DependencyEvaluation,
    DependencyKind,
    Disposition,
    DispositionKind,
    Item,
    ItemKind,
    ItemState,
    ObservedGeneration,
)
from .reconciler import Reconciler, ReconcilerConfig, ReconcilerResult

__all__ = [
    "GateFamily",
    "GroomerConfig",
    "LaneConfig",
    "ModelConfig",
    "load_config",
    "write_default_config",
    "Dependency",
    "DependencyEvaluation",
    "DependencyKind",
    "Disposition",
    "DispositionKind",
    "ExecutorAbortError",
    "ExecutorError",
    "ExecutorResult",
    "GitHubExecutor",
    "GitHubSnapshot",
    "Item",
    "ItemKind",
    "ItemState",
    "ModelError",
    "ModelProvider",
    "ObservedGeneration",
    "OpenAICompatibleProvider",
    "SnapshotError",
    "compute_disposition",
    "Reconciler",
    "ReconcilerConfig",
    "ReconcilerResult",
]

__version__ = "0.3.0"
