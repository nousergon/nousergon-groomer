"""Smoke test — verifies the package imports and the core types load.

This is the first test in the contract test suite (issue #11). It exists
so pytest collects at least one test even before the full contract tests
are written, and so CI is green from the first commit.
"""
from __future__ import annotations

import nousergon_groomer


def test_package_imports():
    assert nousergon_groomer.__version__ == "0.1.0"


def test_core_types_load():
    from nousergon_groomer.models import (
        Dependency,
        DependencyKind,
        Disposition,
        DispositionKind,
        Item,
        ItemKind,
        ItemState,
        ObservedGeneration,
    )

    assert ItemKind.ISSUE == "issue"
    assert ItemKind.PR == "pr"
    assert DispositionKind.ACT == "act"
    assert DependencyKind.S3_OBJECT == "s3_object"
    assert ItemState.OPEN_CLEAN_GREEN == "open_clean_green"

def test_dependency_rejects_empty_target():
    """§3.1 — an unevaluable dependency is rejected at construction."""
    from nousergon_groomer.models import Dependency, DependencyKind

    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        Dependency(kind=DependencyKind.S3_OBJECT, target="")


def test_spec_status_are_separate_types():
    """§3.3 — spec (Dependency) and status (DependencyEvaluation) are separate."""
    from nousergon_groomer.models import Dependency, DependencyEvaluation, DependencyKind

    dep = Dependency(kind=DependencyKind.S3_OBJECT, target="s3://bucket/key")
    ev = DependencyEvaluation(dependency=dep, satisfied=True)
    assert ev.satisfied is True
    assert ev.dependency is dep
    # They are different types — writing status never mutates spec
    assert type(ev) is not type(dep)
