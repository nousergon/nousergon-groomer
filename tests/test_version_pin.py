"""pyproject.toml and __init__.__version__ state the same version.

Mirrors nousergon-lib's test_version_pin.py. This repo shipped without it and
drifted immediately: v0.2.0 was published to PyPI from a tree whose
``__init__.__version__`` still read ``0.1.0``. publish.yml *prints* the
imported version but never asserts it, so the mismatch reached the index
silently — anything reading ``nousergon_groomer.__version__`` to decide
compatibility saw a version two releases stale.

The drift persisted because ``test_smoke.py`` asserted the version equalled a
hardcoded ``"0.1.0"``: bumping ``__init__`` would have failed CI, so only
pyproject.toml moved. That literal is gone; this module owns the real
invariant.
"""

from __future__ import annotations

import pathlib
import re
import sys

import nousergon_groomer

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"


def _pyproject_version() -> str:
    """Read ``[project] version`` without requiring a TOML parser.

    ``tomllib`` is stdlib only on Python 3.11+ and this package supports 3.9
    (``target-version = "py39"``), so importing it unconditionally breaks
    collection on the two oldest supported interpreters — which is exactly
    what happened the first time this test was written, on a laptop running
    3.14. Use ``tomllib`` where it exists and fall back to a line match
    scoped to the ``[project]`` table, which is sufficient for one key that
    the release workflow also reads.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib

        return str(tomllib.loads(text)["project"]["version"])

    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    raise AssertionError(f"no [project] version found in {_PYPROJECT}")


def test_init_version_matches_pyproject():
    assert nousergon_groomer.__version__ == _pyproject_version(), (
        f"__init__.__version__ ({nousergon_groomer.__version__}) != "
        f"pyproject.toml version ({_pyproject_version()}). "
        "pyproject.toml is the single source of truth; update __init__ to match."
    )
