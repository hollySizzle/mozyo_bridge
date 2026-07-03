"""Module-to-test impact resolver (Redmine #12752).

Maps changed source paths to the focused test targets that exercise them, so
implementers and CI stop defaulting to a blanket ``unittest discover -s tests``
every time. The repo's bounded-context layout is the mapping key: source lives
at ``src/mozyo_bridge/e_<order>_<epic>/f_<order>_<feature>/<layer>/<module>.py``
and its tests mirror that at ``tests/<type>/e_<order>_<epic>/f_<order>_<feature>/
test_<module>.py`` (see ``vibes/docs/specs/bounded-context-map.md`` and
``vibes/docs/logics/tests-placement-discovery-policy.md``). From a changed
source path we derive:

- **direct tests** — ``test_<module>.py`` in the mirror feature/epic location;
- **neighbor tests** — the rest of the test files in the same bounded context
  (same feature first, then the rest of the epic).

The contract is **fail-closed, never fail-open**. When a changed path has no
direct test, or cannot be mapped to a bounded context at all, the resolver does
not silently return "nothing to run" (which would let a regression through a
focused CI lane). Instead it attaches a structured :class:`Fallback` with a
machine-readable ``kind`` (``neighbor`` / ``full``) and a human reason, and the
aggregate :class:`ImpactPlan` escalates its recommendation to the full suite the
moment any changed path is unmapped.

Redmine #13078 refines the classification without relaxing that contract: a
cataloged documentation path (``vibes/docs/**/*.md``) resolves to the *docs
validation lane* (:data:`DOCS_LANE`) instead of escalating the test suite, and
a package ``__init__.py`` maps to its bounded context (an exports/wiring change
runs the package's tests) instead of being unmapped. True unknowns — other doc
surfaces, config/build/CI files, non-layout sources — still escalate to full.

This module is pure: :func:`resolve_impact` takes the changed paths plus the
already-listed test files and computes a plan with no I/O. :func:`list_test_files`
is the only filesystem read, and :func:`resolve_impact_for_repo` wires the two
together for the CLI. Keeping the resolution pure makes the bounded-context
mapping unit-testable against synthetic trees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SRC_PREFIX = "src/mozyo_bridge/"
TESTS_ROOT = "tests"

# Importable bounded-context package segments: ``e_<order>_<slug>`` /
# ``f_<order>_<slug>`` (Redmine #12622 numbered layout). The ``e_``/``f_``
# prefix keeps the leading Redmine order number Python-identifier-safe.
_EPIC_RE = re.compile(r"^e_\d+_[a-z0-9_]+$")
_FEATURE_RE = re.compile(r"^f_\d+_[a-z0-9_]+$")
_DDD_LAYERS = frozenset({"domain", "application", "infrastructure", "shared"})

# Resolution status per changed path.
RESOLVED = "resolved"  # bounded context known, at least one direct test found
NEIGHBOR_FALLBACK = "neighbor_fallback"  # context known, no direct test
STEM_RESOLVED = "stem_resolved"  # non-numbered source, matched by file stem only
TEST_CHANGED = "test_changed"  # the changed path is itself a test file
DOCS_LANE = "docs_lane"  # documentation path -> docs validation lane (#13078)
UNMAPPED = "unmapped"  # cannot be mapped to any test -> full-suite fallback

# Fallback kinds (machine-readable; never fail-open).
FALLBACK_NEIGHBOR = "neighbor"
FALLBACK_FULL = "full"

#: Documentation trees whose changes are verified by the docs lane
#: (``mozyo-bridge docs validate`` / ``docs audit-impact``), not by escalating
#: the TEST suite to full (Redmine #13078). Deliberately narrow: distributed
#: doc surfaces (``skills/**``, ``plugins/**``, ``.mozyo-bridge/rules/**``,
#: ``README.md``) carry content-parity tests, so they stay full-escalating.
_DOCS_LANE_PREFIXES: tuple[str, ...] = ("vibes/docs/",)


def _to_posix(path: str) -> str:
    return str(path).replace("\\", "/").strip().lstrip("./")


@dataclass(frozen=True)
class SourceTarget:
    """A changed path parsed against the bounded-context layout."""

    path: str
    kind: str  # "numbered_source" | "flat_source" | "test" | "other"
    epic: str | None = None
    feature: str | None = None
    layer: str | None = None
    module_stem: str | None = None


@dataclass(frozen=True)
class Fallback:
    """Structured, reasoned guidance for a path that lacks a direct mapping.

    ``kind`` is machine-readable (:data:`FALLBACK_NEIGHBOR` /
    :data:`FALLBACK_FULL`); ``roots`` are repo-relative test roots/files a runner
    can take directly (``python -m unittest discover -s <root>`` or
    ``pytest <root>``). ``reason`` explains why the focused mapping was
    insufficient so the escalation is never silent.
    """

    kind: str
    reason: str
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestImpact:
    """Per-source-path resolution result."""

    path: str
    status: str
    direct_tests: tuple[str, ...] = ()
    neighbor_tests: tuple[str, ...] = ()
    fallback: Fallback | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "status": self.status,
            "direct_tests": list(self.direct_tests),
            "neighbor_tests": list(self.neighbor_tests),
            "notes": list(self.notes),
        }
        if self.fallback is not None:
            payload["fallback"] = {
                "kind": self.fallback.kind,
                "reason": self.fallback.reason,
                "roots": list(self.fallback.roots),
            }
        else:
            payload["fallback"] = None
        return payload


@dataclass(frozen=True)
class ImpactPlan:
    """Aggregate resolution across all changed paths.

    ``selected_tests`` is the deduplicated union of direct + neighbor tests for
    the focused run. ``recommendation`` is ``"selected"`` when every changed path
    mapped to a bounded context, or ``"full"`` the moment any path is unmapped —
    fail-closed: an unknown impact escalates to the whole suite rather than
    quietly narrowing it. ``fallback`` carries the aggregate reason when the
    recommendation is ``"full"``.
    """

    resolutions: tuple[TestImpact, ...]
    selected_tests: tuple[str, ...]
    recommendation: str  # "selected" | "full"
    fallback: Fallback | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_unmapped(self) -> bool:
        return any(r.status == UNMAPPED for r in self.resolutions)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "recommendation": self.recommendation,
            "selected_tests": list(self.selected_tests),
            "resolutions": [r.as_dict() for r in self.resolutions],
            "notes": list(self.notes),
        }
        if self.fallback is not None:
            payload["fallback"] = {
                "kind": self.fallback.kind,
                "reason": self.fallback.reason,
                "roots": list(self.fallback.roots),
            }
        else:
            payload["fallback"] = None
        return payload


def parse_source_target(path: str) -> SourceTarget:
    """Classify a repo-relative path against the bounded-context layout."""
    posix = _to_posix(path)

    if posix.startswith(f"{TESTS_ROOT}/") and Path(posix).name.startswith("test_"):
        return SourceTarget(path=posix, kind="test", module_stem=Path(posix).stem)

    if posix.endswith(".md") and posix.startswith(_DOCS_LANE_PREFIXES):
        # Cataloged documentation: its verification lane is the docs tooling,
        # never a full test-suite escalation (#13078). The classification is
        # prefix-narrow so any other doc surface stays conservative.
        return SourceTarget(path=posix, kind="docs")

    if posix.startswith(SRC_PREFIX) and posix.endswith(".py"):
        rel = posix[len(SRC_PREFIX) :]
        parts = rel.split("/")
        name = parts[-1]
        if name == "__init__.py":
            # A package-marker change is an exports/wiring change: map it to
            # its package's bounded context (the affected import/export
            # surface) instead of escalating to full (#13078). Outside the
            # numbered layout no context can be derived, so it falls through
            # to the flat handling, which fail-closes to full when no stem
            # test exists.
            pkg_parts = parts[:-1]
            pkg_stem = pkg_parts[-1] if pkg_parts else "mozyo_bridge"
            epic = (
                pkg_parts[0]
                if pkg_parts and _EPIC_RE.match(pkg_parts[0])
                else None
            )
            if epic is None:
                return SourceTarget(
                    path=posix, kind="flat_source", module_stem=pkg_stem
                )
            feature = (
                pkg_parts[1]
                if len(pkg_parts) >= 2 and _FEATURE_RE.match(pkg_parts[1])
                else None
            )
            layer = next((p for p in pkg_parts if p in _DDD_LAYERS), None)
            return SourceTarget(
                path=posix,
                kind="numbered_source",
                epic=epic,
                feature=feature,
                layer=layer,
                module_stem=pkg_stem,
            )
        stem = Path(name).stem
        epic = parts[0] if parts and _EPIC_RE.match(parts[0]) else None
        feature = None
        layer = None
        if epic is not None:
            if len(parts) >= 2 and _FEATURE_RE.match(parts[1]):
                feature = parts[1]
            if len(parts) >= 2 and parts[-2] in _DDD_LAYERS:
                layer = parts[-2]
            return SourceTarget(
                path=posix,
                kind="numbered_source",
                epic=epic,
                feature=feature,
                layer=layer,
                module_stem=stem,
            )
        return SourceTarget(path=posix, kind="flat_source", module_stem=stem)

    return SourceTarget(path=posix, kind="other")


def list_test_files(repo_root: Path | str) -> tuple[str, ...]:
    """Return every ``test_*.py`` under ``tests/`` as sorted repo-relative posix paths."""
    root = Path(repo_root)
    tests_dir = root / TESTS_ROOT
    if not tests_dir.is_dir():
        return ()
    found: list[str] = []
    for path in tests_dir.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        found.append(path.relative_to(root).as_posix())
    return tuple(sorted(found))


def _segments(test_path: str) -> set[str]:
    return set(Path(test_path).parts)


def _resolve_numbered(target: SourceTarget, test_files: tuple[str, ...]) -> TestImpact:
    # A ``numbered_source`` target always carries a parsed epic (see
    # parse_source_target). Make that precondition explicit so the invariant is
    # enforced and the type narrows from ``str | None`` to ``str`` for the
    # context-string construction below.
    assert target.epic is not None
    direct_name = f"test_{target.module_stem}.py"
    direct: list[str] = []
    feature_neighbors: list[str] = []
    epic_neighbors: list[str] = []

    for test in test_files:
        segs = _segments(test)
        if target.epic not in segs:
            continue
        in_feature = target.feature is not None and target.feature in segs
        if Path(test).name == direct_name:
            # A direct stem match counts only when it sits in the right context:
            # in the feature dir when we know the feature, otherwise the epic.
            if target.feature is None or in_feature:
                direct.append(test)
                continue
        if in_feature:
            feature_neighbors.append(test)
        else:
            epic_neighbors.append(test)

    # Bounded-context neighbors stay focused: the same feature is the finest
    # bounded context, so prefer it. Only widen to the rest of the epic when the
    # feature is unknown (epic-only source) or the feature dir holds no other
    # tests — otherwise a one-module change would pull in the whole epic.
    notes: list[str] = []
    if target.feature is not None and feature_neighbors:
        neighbors = tuple(dict.fromkeys(feature_neighbors))
    elif target.feature is not None:
        neighbors = tuple(dict.fromkeys(epic_neighbors))
        if neighbors:
            notes.append(
                f"no other tests in {target.feature}; widened neighbors to "
                f"the {target.epic} bounded context"
            )
    else:
        neighbors = tuple(dict.fromkeys(epic_neighbors))
    direct_t = tuple(dict.fromkeys(direct))

    if direct_t:
        return TestImpact(
            path=target.path,
            status=RESOLVED,
            direct_tests=direct_t,
            neighbor_tests=neighbors,
            notes=tuple(notes),
        )

    # Context known, but no direct test for this module: do not fail open.
    roots = _existing_context_roots(target, test_files)
    context = target.epic + (f"/{target.feature}" if target.feature else "")

    # A "neighbor" fallback that can offer neither a neighbor test nor a runnable
    # root would hand the runner an empty set — which is fail-open. The context
    # holds no tests at all, so we cannot focus: escalate this path to the full
    # suite so the aggregate plan escalates too.
    if not neighbors and not roots:
        reason = (
            f"bounded context {context} has no test files for module "
            f"'{target.module_stem}'; run the full suite"
        )
        return TestImpact(
            path=target.path,
            status=UNMAPPED,
            fallback=Fallback(kind=FALLBACK_FULL, reason=reason, roots=(TESTS_ROOT,)),
            notes=("known context but no tests present",),
        )

    reason = (
        f"no direct {direct_name} for module '{target.module_stem}' "
        f"in {context}; run bounded-context neighbor tests"
    )
    return TestImpact(
        path=target.path,
        status=NEIGHBOR_FALLBACK,
        direct_tests=(),
        neighbor_tests=neighbors,
        fallback=Fallback(kind=FALLBACK_NEIGHBOR, reason=reason, roots=roots),
        notes=(f"no {direct_name} found in mapped context",),
    )


def _existing_context_roots(
    target: SourceTarget, test_files: tuple[str, ...]
) -> tuple[str, ...]:
    """Distinct ``tests/<type>/<epic>[/<feature>]`` dirs that actually hold tests."""
    roots: list[str] = []
    for test in test_files:
        parts = Path(test).parts
        if target.epic not in parts:
            continue
        idx = parts.index(target.epic)
        if target.feature is not None and target.feature in parts:
            fidx = parts.index(target.feature)
            root = "/".join(parts[: fidx + 1])
        else:
            root = "/".join(parts[: idx + 1])
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _resolve_flat(target: SourceTarget, test_files: tuple[str, ...]) -> TestImpact:
    """Non-numbered source (facade / unmigrated): match by file stem anywhere."""
    direct_name = f"test_{target.module_stem}.py"
    direct = tuple(t for t in test_files if Path(t).name == direct_name)
    if direct:
        return TestImpact(
            path=target.path,
            status=STEM_RESOLVED,
            direct_tests=direct,
            notes=(
                "matched by module stem only; path is outside the numbered "
                "e_*/f_* bounded-context layout",
            ),
        )
    reason = (
        f"source '{target.path}' is outside the numbered bounded-context layout "
        f"and no test_{target.module_stem}.py exists; run the full suite"
    )
    return TestImpact(
        path=target.path,
        status=UNMAPPED,
        fallback=Fallback(kind=FALLBACK_FULL, reason=reason, roots=(TESTS_ROOT,)),
        notes=("unmapped flat source",),
    )


def _resolve_one(target: SourceTarget, test_files: tuple[str, ...]) -> TestImpact:
    if target.kind == "test":
        # A changed test is its own direct target.
        return TestImpact(
            path=target.path,
            status=TEST_CHANGED,
            direct_tests=(target.path,),
            notes=("changed path is a test module",),
        )
    if target.kind == "docs":
        # Cataloged documentation is verified by the docs lane, not by the test
        # suite (#13078): it selects no tests and never escalates the plan to
        # full by itself. The aggregate plan surfaces the docs-lane commands.
        return TestImpact(
            path=target.path,
            status=DOCS_LANE,
            notes=(
                "documentation path; verify via the docs lane "
                "(mozyo-bridge docs validate / docs audit-impact), not by "
                "escalating the test suite",
            ),
        )
    if target.kind == "numbered_source":
        return _resolve_numbered(target, test_files)
    if target.kind == "flat_source":
        return _resolve_flat(target, test_files)

    reason = (
        f"path '{target.path}' is not a recognized source or test module; "
        f"run the full suite"
    )
    return TestImpact(
        path=target.path,
        status=UNMAPPED,
        fallback=Fallback(kind=FALLBACK_FULL, reason=reason, roots=(TESTS_ROOT,)),
        notes=("non-source path",),
    )


def resolve_impact(paths: list[str], *, test_files: tuple[str, ...]) -> ImpactPlan:
    """Resolve changed ``paths`` to a focused test plan (pure; no I/O).

    ``test_files`` is the repo's known ``test_*.py`` set (see
    :func:`list_test_files`). The plan's ``selected_tests`` is the deduplicated,
    order-preserving union of direct then neighbor tests; the recommendation
    escalates to ``"full"`` whenever any path is :data:`UNMAPPED`.
    """
    resolutions = tuple(_resolve_one(parse_source_target(p), test_files) for p in paths)

    selected: list[str] = []
    for res in resolutions:
        for test in res.direct_tests:
            if test not in selected:
                selected.append(test)
    for res in resolutions:
        for test in res.neighbor_tests:
            if test not in selected:
                selected.append(test)

    if not resolutions:
        return ImpactPlan(
            resolutions=(),
            selected_tests=(),
            recommendation="full",
            fallback=Fallback(
                kind=FALLBACK_FULL,
                reason="no changed paths provided; run the full suite",
                roots=(TESTS_ROOT,),
            ),
            notes=("empty change set",),
        )

    # Docs-lane paths never escalate the test plan by themselves, but their
    # verification duty must not go silent (#13078): the plan carries the
    # docs-lane commands as a machine-readable note.
    docs_lane = [r.path for r in resolutions if r.status == DOCS_LANE]
    notes: list[str] = []
    if docs_lane:
        notes.append(
            "docs-lane path(s) changed; also run the docs lane: "
            "`mozyo-bridge docs validate --repo .` and "
            "`mozyo-bridge docs audit-impact --staged --check-generated`"
        )

    unmapped = [r.path for r in resolutions if r.status == UNMAPPED]
    if unmapped:
        reason = (
            "unmapped changed path(s) -> impact unknown, escalating to full suite: "
            + ", ".join(unmapped)
        )
        return ImpactPlan(
            resolutions=resolutions,
            selected_tests=tuple(selected),
            recommendation="full",
            fallback=Fallback(kind=FALLBACK_FULL, reason=reason, roots=(TESTS_ROOT,)),
            notes=tuple(notes),
        )

    # Backstop: if the focused resolution somehow yields nothing runnable, never
    # report "selected" with an empty set (which a runner reads as fail-open).
    # Escalate to the full suite instead. A docs-only change set lands here
    # deliberately: the docs lane verifies the content, and the full suite (the
    # docs-parity tests live there) stays the fail-closed test-side backstop.
    if not selected:
        docs_only = bool(docs_lane) and len(docs_lane) == len(resolutions)
        reason = (
            "only documentation paths changed; no focused test targets — run "
            "the docs lane, with the full suite (docs-parity tests) as the "
            "fail-closed backstop"
            if docs_only
            else "focused resolution produced no runnable test targets; "
            "escalating to full suite"
        )
        return ImpactPlan(
            resolutions=resolutions,
            selected_tests=(),
            recommendation="full",
            fallback=Fallback(
                kind=FALLBACK_FULL,
                reason=reason,
                roots=(TESTS_ROOT,),
            ),
            notes=tuple(notes),
        )

    return ImpactPlan(
        resolutions=resolutions,
        selected_tests=tuple(selected),
        recommendation="selected",
        notes=tuple(notes),
    )


def resolve_impact_for_repo(repo_root: Path | str, paths: list[str]) -> ImpactPlan:
    """Filesystem-backed convenience: list the repo's tests, then resolve."""
    return resolve_impact(paths, test_files=list_test_files(repo_root))
