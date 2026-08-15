"""Redmine #15507 — the test suite stays runnable on the minimum supported Python.

`requires-python = ">=3.10"`, but the quick and integration CI lanes run a
single Python (3.12) by design; only the production publish gate runs the full
3.10–3.13 matrix. A 3.11+ test API therefore stays invisible until the moment
it blocks a release: `TestCase.enterContext` (3.11) raised AttributeError in
one setUp on 3.10, erroring every test in that class and failing the #14741
site-wiring test that drives the same fixture — discovered only when the v1.0.0
production publish ran its matrix.

This guard fails in every lane instead, on the cheapest signal available: the
source text. Two properties keep it honest, both learned from review findings:

- The ban list covers every relevant API added between the floor and the lane
  Python, not only the one that bit us — including `unittest.enterModuleContext`,
  which is module-level rather than a `TestCase` method (review j#105428
  finding_pyguardcoverage).
- The self-test drives :func:`scan_source`, the SAME function the repository
  scan uses. A self-test that reimplements the matcher proves only that the
  reimplementation works; the earlier version did exactly that and hid an
  `AttributeError` in the real scanner for two of the receiver shapes it
  claimed to cover (review j#105428 finding_pyguardselftest).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# This file lives at tests/regressions/, so the repo root is two levels up.
ROOT = Path(__file__).resolve().parents[2]
_TESTS = ROOT / "tests"

#: `unittest` APIs newer than the minimum supported Python, mapped to the
#: version that introduced them. A test using one of these runs green on the
#: single-Python lanes and errors on the minimum, so it is banned outright.
#: All four context helpers below arrived in 3.11.
_TOO_NEW_UNITTEST_APIS = {
    "enterContext": "3.11",  # TestCase.enterContext
    "enterClassContext": "3.11",  # TestCase.enterClassContext (classmethod)
    "enterAsyncContext": "3.11",  # IsolatedAsyncioTestCase.enterAsyncContext
    "enterModuleContext": "3.11",  # unittest.enterModuleContext (module-level)
}


def scan_source(source: str, *, where: str = "<source>") -> list[str]:
    """Offender diagnostics for every banned API call in ``source``.

    Matching is by CALLED NAME, in both shapes a banned API can be reached:

    - an attribute call, whatever the receiver is — ``self.x()``, ``cls.x()``,
      ``type(self).x()``, ``self.case.x()``, ``unittest.x()``;
    - a bare-name call, which is how ``from unittest import enterModuleContext``
      reaches the module-level helper.

    The receiver is rendered with :func:`ast.unparse` rather than read off a
    field that only exists on a plain name — those receiver shapes are exactly
    where a ``.id`` lookup raised instead of reporting.
    """
    diagnostics: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
            called = f"{ast.unparse(func.value)}.{name}"
        elif isinstance(func, ast.Name):
            name = func.id
            called = name
        else:
            continue
        introduced = _TOO_NEW_UNITTEST_APIS.get(name)
        if introduced is None:
            continue
        diagnostics.append(
            f"{where}:{node.lineno} {called}() requires Python {introduced}+"
        )
    return diagnostics


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
        for name, introduced in _TOO_NEW_UNITTEST_APIS.items():
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
        offenders: list[str] = []
        for path in sorted(_TESTS.rglob("test_*.py")):
            if path == Path(__file__):
                continue
            relative = path.relative_to(ROOT)
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - unreadable test file
                self.fail(f"{relative} could not be read: {exc}")
            try:
                offenders.extend(scan_source(source, where=str(relative)))
            except SyntaxError as exc:  # pragma: no cover - a broken test file
                self.fail(f"{relative} does not parse: {exc}")
        self.assertEqual(
            [],
            offenders,
            msg=(
                "these tests cannot run on the minimum supported Python; the "
                "single-Python lanes hide that until the release matrix runs:\n"
                + "\n".join(offenders)
            ),
        )


class ScannerReallyFlagsEveryBannedShapeTest(unittest.TestCase):
    """The scanner the repo scan uses must flag every API in every shape.

    A ban list is only as good as the matcher behind it, so these cases drive
    :func:`scan_source` itself — not a copy of its logic — and assert on the
    diagnostic text. An entry the matcher never reaches, or a shape that raises
    instead of reporting, fails here rather than in a release matrix.
    """

    ATTRIBUTE_RECEIVERS = ("self", "cls", "type(self)", "self.case", "unittest")

    def test_every_banned_api_is_flagged_in_every_attribute_shape(self) -> None:
        for api, introduced in _TOO_NEW_UNITTEST_APIS.items():
            for receiver in self.ATTRIBUTE_RECEIVERS:
                with self.subTest(api=api, receiver=receiver):
                    found = scan_source(
                        f"def t():\n    {receiver}.{api}(ctx)\n", where="fake.py"
                    )
                    self.assertEqual(
                        [
                            f"fake.py:2 {receiver}.{api}() "
                            f"requires Python {introduced}+"
                        ],
                        found,
                    )

    def test_every_banned_api_is_flagged_as_a_bare_name_call(self) -> None:
        # `from unittest import enterModuleContext` reaches the module-level
        # helper without any receiver at all.
        for api, introduced in _TOO_NEW_UNITTEST_APIS.items():
            with self.subTest(api=api):
                found = scan_source(
                    f"from unittest import {api}\n\ndef t():\n    {api}(ctx)\n",
                    where="fake.py",
                )
                self.assertEqual(
                    [f"fake.py:4 {api}() requires Python {introduced}+"], found
                )

    def test_allowed_apis_are_not_flagged(self) -> None:
        # The 3.10-compatible replacements must stay usable, and a name that
        # merely starts like a banned one must not be swept up.
        for source in (
            "def t():\n    self.addCleanup(p.stop)\n",
            "def t():\n    self.enterprise(ctx)\n",
            "def t():\n    with ExitStack() as stack:\n        stack.enter_context(cm)\n",
        ):
            with self.subTest(source=source):
                self.assertEqual([], scan_source(source, where="fake.py"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
