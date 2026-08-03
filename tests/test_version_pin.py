"""pyproject.toml and __init__.__version__ state the same version.

Mirrors nousergon-lib's test_version_pin.py. This repo shipped without it and
drifted immediately: v0.2.0 was published to PyPI from a tree whose
``__init__.__version__`` still read ``0.1.0``. publish.yml *prints* the
imported version but never asserts it, so the mismatch reached the index
silently — anything reading ``nousergon_groomer.__version__`` to decide
compatibility saw a version that was two releases stale.
"""

from __future__ import annotations

import pathlib
import tomllib

import nousergon_groomer

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_bytes().decode())
    return data["project"]["version"]


def test_init_version_matches_pyproject():
    assert nousergon_groomer.__version__ == _pyproject_version(), (
        f"__init__.__version__ ({nousergon_groomer.__version__}) != "
        f"pyproject.toml version ({_pyproject_version()}). "
        "pyproject.toml is the single source of truth; update __init__ to match."
    )
