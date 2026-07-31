"""Pytest fixtures — loads JSON scenario files and provides them to tests.

This conftest proves §8.1: the entire test suite runs over recorded fixtures
with no network, no credentials, no model. Each fixture file records an item
population, an observed world, an optional reconciler config, and the
expected dispositions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nousergon_groomer.dependency_evaluator import ObservedWorld
from nousergon_groomer.models import Dependency, Item
from nousergon_groomer.reconciler import ReconcilerConfig

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def _build_items(raw_items: list[dict]) -> list[Item]:
    items = []
    for raw in raw_items:
        deps = [
            Dependency(kind=d["kind"], target=d["target"])
            for d in raw.get("declared_dependencies", [])
        ]
        items.append(
            Item(
                id=raw["id"],
                kind=raw["kind"],
                state=raw["state"],
                title=raw.get("title", ""),
                labels=raw.get("labels", []),
                declared_dependencies=deps,
                is_draft=raw.get("is_draft", False),
                human_owned=raw.get("human_owned", False),
                mergeable=raw.get("mergeable"),
                ci_green=raw.get("ci_green"),
                security_threads=raw.get("security_threads", 0),
            )
        )
    return items


def _build_world(raw_world: dict) -> ObservedWorld:
    def _set(v):
        return set(v) if v is not None else None
    return ObservedWorld(
        s3_objects=_set(raw_world.get("s3_objects")),
        s3_prefixes=_set(raw_world.get("s3_prefixes")),
        terminal_items=_set(raw_world.get("terminal_items")),
        pipeline_runs=_set(raw_world.get("pipeline_runs")),
        absent_labels=_set(raw_world.get("absent_labels")),
    )


def load_fixture(name: str) -> dict[str, Any]:
    """Load a fixture by filename (without .json) and return its parsed contents."""
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"fixture not found: {path}")
    return _load_json(path)


@pytest.fixture
def fixture_names() -> list[str]:
    """Return the names of all fixture files (without .json)."""
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))


def pytest_generate_tests(metafunc):
    """Parametrize `fixture_name` over every fixture file name."""
    if "fixture_name" in metafunc.fixturenames:
        names = sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))
        metafunc.parametrize("fixture_name", names, ids=names)


@pytest.fixture
def fixture_name(fixture_name: str) -> str:
    """The name of the fixture scenario (without .json)."""
    return fixture_name


@pytest.fixture
def fixture_scenario(fixture_name: str) -> dict[str, Any]:
    """One fixture scenario, loaded from JSON."""
    return load_fixture(fixture_name)


@pytest.fixture
def fixture_items(fixture_scenario: dict) -> list[Item]:
    return _build_items(fixture_scenario["items"])


@pytest.fixture
def fixture_world(fixture_scenario: dict) -> ObservedWorld:
    return _build_world(fixture_scenario["world"])


@pytest.fixture
def fixture_config(fixture_scenario: dict) -> ReconcilerConfig:
    cfg = fixture_scenario.get("config", {})
    return ReconcilerConfig(wip_ceiling=cfg.get("wip_ceiling", 5), generation=1)


@pytest.fixture
def fixture_expected(fixture_scenario: dict) -> dict[str, dict]:
    return fixture_scenario["expected"]
