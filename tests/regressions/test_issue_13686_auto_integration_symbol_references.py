"""Redmine #13686: a withdrawn symbol is still named by the sources that used it.

One issue, one fixed symptom, and both tests below detect only its recurrence — which is why
this is a regression pin rather than a unit test of anything (placement decision tree branch 3;
R19 review j#96492 finding 3 corrected R19's placement under ``tests/unit/``).

**The symptom, and the commit that left it.** ``044200bd`` (#13686 R18) withdrew
``_sanitized_git_dir`` and replaced it with ``_open_sandbox`` / ``_close_sandbox``, and left
five ``:meth:`_sanitized_git_dir``` references behind in ``auto_integration_live_ops.py`` — in
``#:`` comments and docstrings, where nothing executes them. It was the fourth consecutive
round of the same thing: R14 through R17 each claimed a sweep of the tracked files and each
narrowed what "sweep" meant, and each time the reviewer found what the claim had missed
(j#96435 F5, j#96441 F5, j#96447 F3, j#96453 F3, j#96461 F4). Fixed in #13686 R20.

Repeating the claim a fifth time is not a fix. The check is mechanical, so it is performed
mechanically: this module resolves every Sphinx cross-reference in the auto-integration
sources and fails on the ones that name nothing.

How each reference is resolved, stated exactly:

- ``Class.member`` — if ``Class`` is defined anywhere in this subsystem, ``member`` must be one
  of its attributes. If it is not (``...application.sublane_integration.X.y``), the reference
  points outside the subsystem and is **out of scope**: resolving it means importing modules
  this suite does not otherwise touch, and pointing at a stale name in another package is a
  weaker failure than pointing at one in the file you are reading.
- a bare private name (``_foo``) — must exist in **its own module**. That is the class of
  reference that kept going stale, and a private name is not meaningfully referenced from
  elsewhere.
- a bare public name — must exist somewhere in the subsystem, because those references cross
  modules on purpose (``:data:`STATE_PATCH_EQUIVALENT``` in the records module names a policy
  constant, and that is correct).

This is not a claim that prose is accurate — no test can make that claim. It is the narrow,
checkable part: a reference that names a symbol which does not exist.
"""
from __future__ import annotations

import importlib
import inspect
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

PACKAGE = "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff"

#: The auto-integration surface #13686 owns. Named explicitly rather than discovered, so
#: adding a module to the subsystem is a deliberate act that includes covering it here.
MODULES = (
    f"{PACKAGE}.application.auto_integration_live_ops",
    f"{PACKAGE}.application.auto_integration_ports",
    f"{PACKAGE}.application.auto_integration_actuator",
    f"{PACKAGE}.domain.auto_integration_records",
    f"{PACKAGE}.domain.auto_integration_policy",
    f"{PACKAGE}.domain.retirement_cleanup_policy",
)

#: ``:meth:`~.Foo.bar``` and friends. The leading ``~`` and ``.`` are Sphinx display sugar.
_REFERENCE = re.compile(r":(?:meth|attr|data|func|class|obj):`~?\.?([A-Za-z_][\w.]*)`")


def _members(value: type) -> set:
    # Dataclass fields with no default live only in the annotations, never in `dir()`.
    return set(dir(value)) | set(getattr(value, "__annotations__", {}))


def _names_defined_in(module: object) -> set:
    """Every name a bare reference in ``module`` could legitimately mean."""
    names = set(vars(module))
    for value in list(vars(module).values()):
        if inspect.isclass(value):
            names |= _members(value)
    return names


def _classes_in(modules) -> dict:
    found = {}
    for module in modules:
        for name, value in vars(module).items():
            if inspect.isclass(value):
                found.setdefault(name, value)
    return found


class SymbolReferenceTest(unittest.TestCase):
    """Resolve every reference in the subsystem's own sources."""

    def setUp(self) -> None:
        self.modules = [importlib.import_module(dotted) for dotted in MODULES]
        self.classes = _classes_in(self.modules)
        self.subsystem: set = set()
        for module in self.modules:
            self.subsystem |= _names_defined_in(module)

    def _stale(self) -> list:
        stale = []
        for module in self.modules:
            own = _names_defined_in(module)
            source = Path(module.__file__).read_text(encoding="utf-8")
            where = Path(module.__file__).name
            for line_number, line in enumerate(source.splitlines(), 1):
                for reference in _REFERENCE.findall(line):
                    parts = reference.split(".")
                    if len(parts) > 1:
                        owner = self.classes.get(parts[-2])
                        if owner is None:
                            continue  # outside the subsystem — see the module docstring
                        if parts[-1] not in _members(owner):
                            stale.append(f"{where}:{line_number} -> {reference}")
                        continue
                    name = parts[0]
                    if name.startswith("__"):
                        continue
                    known = own if name.startswith("_") else self.subsystem
                    if name not in known:
                        stale.append(f"{where}:{line_number} -> {reference}")
        return stale

    def test_no_reference_names_a_symbol_that_does_not_exist(self) -> None:
        self.assertEqual(self._stale(), [])

    def test_the_check_would_catch_the_defect_it_exists_for(self) -> None:
        """Not a vacuous pass: the regex must match the shape that actually went stale.

        The five surviving R18 references were ``:meth:`_sanitized_git_dir``` inside ``#:``
        comments and docstrings. If a future edit breaks the pattern, this says so rather than
        the suite quietly checking nothing.
        """
        self.assertEqual(
            _REFERENCE.findall(
                "    #: for the duration of :meth:`_open_sandbox`; see :data:`~.MERGE_MERGED`."
            ),
            ["_open_sandbox", "MERGE_MERGED"],
        )
        live_ops = importlib.import_module(MODULES[0])
        own = _names_defined_in(live_ops)
        self.assertIn("_open_sandbox", own)
        self.assertNotIn("_sanitized_git_dir", own)
        # And a `Class.member` reference resolves rather than being waved through: the class
        # is found in a *different* module of the subsystem and its members are checked.
        self.assertIn("AutoIntegrationUseCase", self.classes)
        self.assertIn("_measure", _members(self.classes["AutoIntegrationUseCase"]))


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
