"""Enforce the shared-kernel freeze (Redmine #12640).

`src/mozyo_bridge/shared/**` is a frozen foundational kernel. These tests are
the mechanical guardrail behind the freeze policy
(`vibes/docs/logics/shared-kernel-freeze.md`):

- the kernel module set may not grow (no new utility-dump module), and
- every kernel module must keep the one-directional dependency invariant: it
  must not import any bounded-context package, so all contexts can depend on
  `shared` without creating an import cycle.

A new module under `shared/` or an upward import from a kernel module fails here
before review, forcing the new concern into a bounded context / `core/state`
instead (or an explicitly justified freeze exception).
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SHARED_DIR = ROOT / "src" / "mozyo_bridge" / "shared"

# The frozen kernel set. Growing this set is a deliberate freeze exception that
# must be justified in the review gate and in shared-kernel-freeze.md, not a
# silent addition.
FROZEN_SHARED_MODULES = frozenset(
    {
        "__init__.py",
        "errors.py",
        "name_compat.py",
        "paths.py",
    }
)

# A kernel module may import the standard library and (internally) other
# `mozyo_bridge.shared.*` modules only. Importing any of these bounded-context
# roots would invert the dependency direction and risk an import cycle.
FORBIDDEN_IMPORT_ROOTS = (
    "mozyo_bridge.application",
    "mozyo_bridge.core",
    "mozyo_bridge.domain",
    "mozyo_bridge.infrastructure",
    "mozyo_bridge.scaffold",
    "mozyo_bridge.docs_tools",
)


def _is_forbidden(module_name: str) -> bool:
    """True if ``module_name`` targets a bounded-context package or e_*/f_* root."""
    if module_name is None:
        return False
    for root in FORBIDDEN_IMPORT_ROOTS:
        if module_name == root or module_name.startswith(root + "."):
            return True
    # Numbered Epic packages: mozyo_bridge.e_<order>_<slug>... / f_<order>_...
    if module_name.startswith("mozyo_bridge.e_") or module_name.startswith("mozyo_bridge.f_"):
        return True
    return False


def _imported_modules(source: str) -> list[str]:
    """Return fully-qualified module targets of import statements in ``source``."""
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Only absolute imports carry a bounded-context root; relative
            # imports (level > 0) stay inside the shared package.
            if node.level == 0 and node.module:
                names.append(node.module)
    return names


class SharedKernelFreezeTest(unittest.TestCase):
    def test_shared_directory_exists(self) -> None:
        self.assertTrue(
            SHARED_DIR.is_dir(),
            f"shared kernel directory missing: {SHARED_DIR}",
        )

    def test_shared_module_set_is_frozen(self) -> None:
        actual = {path.name for path in SHARED_DIR.glob("*.py")}
        self.assertEqual(
            FROZEN_SHARED_MODULES,
            actual,
            "src/mozyo_bridge/shared/** is frozen (Redmine #12640): the kernel "
            "module set may not change. New value/path/error/compat concerns "
            "belong in a bounded context or core/state, not shared. To add or "
            "remove a kernel module, update FROZEN_SHARED_MODULES together with "
            "vibes/docs/logics/shared-kernel-freeze.md and justify it in review.",
        )

    def test_kernel_modules_do_not_import_bounded_contexts(self) -> None:
        offenders: dict[str, list[str]] = {}
        for path in sorted(SHARED_DIR.glob("*.py")):
            imported = _imported_modules(path.read_text(encoding="utf-8"))
            forbidden = sorted({name for name in imported if _is_forbidden(name)})
            if forbidden:
                offenders[path.name] = forbidden
        self.assertEqual(
            {},
            offenders,
            "shared kernel modules must not import bounded-context packages "
            "(one-directional dependency invariant, Redmine #12640): "
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
