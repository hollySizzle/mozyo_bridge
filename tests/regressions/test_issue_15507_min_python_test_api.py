"""Redmine #15507 — the test suite stays runnable on the minimum supported Python.

`requires-python = ">=3.10"`, but the quick and integration CI lanes run a
single Python (3.12) by design; only the production publish gate runs the full
3.10–3.13 matrix. A 3.11+ test API therefore stays invisible until the moment
it blocks a release: `TestCase.enterContext` (3.11+) raised AttributeError in
one setUp on 3.10, erroring every test in that class and failing the #14741
site-wiring test that drives the same fixture — discovered only when the v1.0.0
production publish ran its matrix.

This guard fails in every lane instead, on the cheapest signal available: the
source text. Adding an API here means it must also be listed with the version
that introduced it, so the reason a form is banned stays legible.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

# This file lives at tests/regressions/, so the repo root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
_TESTS = ROOT / "tests"

#: `unittest` APIs newer than the minimum supported Python, mapped to the
#: version that introduced them. A test using one of these runs green on the
#: single-Python lanes and errors on the minimum, so it is banned outright.
_TOO_NEW_TESTCASE_APIS = {
    "enterContext": "3.11",
}


def _min_supported_python() -> tuple[int, int]:
    """The `requires-python` floor, read from pyproject rather than hardcoded."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("requires-python"):
            spec = line.split("=", 1)[1].strip().strip('"').strip("'")
            floor = spec.removeprefix(">=").strip()
            major, minor = floor.split(".")[:2]
            return (int(major), int(minor))
    raise AssertionError("pyproject.toml declares no requires-python floor")


class MinimumPythonTestApiTest(unittest.TestCase):
    def test_floor_is_below_the_apis_this_guard_bans(self) -> None:
        # If the project ever raises its floor past one of these versions, the
        # corresponding entry is obsolete and must be dropped rather than left
        # banning a form the whole matrix can now run.
        floor = _min_supported_python()
        for name, introduced in _TOO_NEW_TESTCASE_APIS.items():
            major, minor = (int(part) for part in introduced.split("."))
            self.assertLess(
                floor,
                (major, minor),
                msg=(
                    f"the supported floor {floor} now includes {name} "
                    f"({introduced}+); drop it from the ban list"
                ),
            )

    def test_no_test_uses_an_api_newer_than_the_floor(self) -> None:
        offenders = []
        for path in sorted(_TESTS.rglob("test_*.py")):
            if path == Path(__file__):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:  # pragma: no cover - a broken test file
                self.fail(f"{path.relative_to(ROOT)} does not parse: {exc}")
            for node in ast.walk(tree):
                # Match `self.<api>(...)` / `case.<api>(...)`: an attribute call
                # on a name, which is how a TestCase method is reached.
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if not isinstance(func.value, ast.Name):
                    continue
                introduced = _TOO_NEW_TESTCASE_APIS.get(func.attr)
                if introduced is None:
                    continue
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} "
                    f"{func.value.id}.{func.attr}() requires Python {introduced}+"
                )
        self.assertEqual(
            [],
            offenders,
            msg=(
                "these tests cannot run on the minimum supported Python; the "
                "single-Python lanes hide that until the release matrix runs:\n"
                + "\n".join(offenders)
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
