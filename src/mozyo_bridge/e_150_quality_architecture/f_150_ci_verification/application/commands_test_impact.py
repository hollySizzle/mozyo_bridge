"""Command handler for ``mozyo-bridge tests resolve`` (Redmine #12752).

Thin glue: resolve the changed paths (explicit args or git-derived via the same
``--staged`` / ``--all-changed`` selection the docs impact gate uses), call the
pure resolver, and render text / JSON / a runner-ready target list. All mapping
logic lives in the pure
:mod:`mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_impact`.

The three output formats cover the "usable from CI and local" acceptance:

- ``text`` — human review of the per-path mapping and any fallback reason;
- ``json`` — machine consumption (the full :class:`ImpactPlan` dict);
- ``targets`` — newline-separated ``python -m unittest`` arguments for direct
  piping, e.g.
  ``mozyo-bridge tests resolve --staged --format targets | xargs python -m unittest``;
  a ``full`` recommendation prints ``discover -s tests`` (a bare directory is not
  a valid unittest argument) so the pipe actually runs the whole suite and CI
  never silently runs an empty (fail-open) set.
"""

from __future__ import annotations

import argparse
import json as _json
import sys

from mozyo_bridge.docs_tools.impact import git_changed_paths
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_impact import (
    TESTS_ROOT,
    ImpactPlan,
    resolve_impact_for_repo,
)
from mozyo_bridge.shared.paths import resolve_repo_root


def _repo_root(args: argparse.Namespace):
    return resolve_repo_root(getattr(args, "repo", None))


def _collect_paths(args: argparse.Namespace, repo_root) -> list[str]:
    explicit = list(getattr(args, "paths", []) or [])
    if explicit:
        return explicit
    return git_changed_paths(
        repo_root,
        staged=bool(getattr(args, "staged", False)),
        all_changed=bool(getattr(args, "all_changed", False)),
    )


def _plan_targets(plan: ImpactPlan) -> list[str]:
    """Runner-ready ``python -m unittest`` arguments.

    On a focused recommendation the selected test files are already valid
    ``unittest`` arguments. On a fail-closed full recommendation a bare
    directory is **not** a valid ``unittest`` argument (``python -m unittest
    tests`` runs nothing), so emit the ``discover -s <root>`` form that actually
    walks the full suite.
    """
    if plan.recommendation == "full":
        roots = plan.fallback.roots if plan.fallback else (TESTS_ROOT,)
        root = roots[0] if roots else TESTS_ROOT
        return ["discover", "-s", root]
    return list(plan.selected_tests)


def cmd_tests_resolve(args: argparse.Namespace) -> int:
    """Resolve changed source paths to focused test targets (read-only)."""
    repo_root = _repo_root(args)
    paths = _collect_paths(args, repo_root)
    plan = resolve_impact_for_repo(repo_root, paths)

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        print(_json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if fmt == "targets":
        for target in _plan_targets(plan):
            print(target)
        return 0

    _render_text(plan)
    return 0


def _render_text(plan: ImpactPlan) -> None:
    print(f"recommendation: {plan.recommendation}")
    if plan.fallback is not None:
        print(f"  fallback[{plan.fallback.kind}]: {plan.fallback.reason}")
    for note in plan.notes:
        print(f"  note: {note}")

    print(f"selected_tests: {len(plan.selected_tests)}")
    for test in plan.selected_tests:
        print(f"  - {test}")

    if not plan.resolutions:
        return
    print("per-path:")
    for res in plan.resolutions:
        print(f"  [{res.path}] -> {res.status}")
        for test in res.direct_tests:
            print(f"      direct: {test}")
        for test in res.neighbor_tests:
            print(f"      neighbor: {test}")
        if res.fallback is not None:
            roots = ", ".join(res.fallback.roots) or "-"
            print(
                f"      fallback[{res.fallback.kind}]: {res.fallback.reason} "
                f"(roots: {roots})"
            )
        for note in res.notes:
            print(f"      note: {note}")


__all__ = ("cmd_tests_resolve",)
