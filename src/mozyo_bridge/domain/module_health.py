"""Module-health metrics and the oversized-module gate (Redmine #12321).

This is the pure, dependency-free core for the ``mozyo-bridge health`` family.
It makes module health measurable instead of subjective: it counts per-module
physical lines, an approximate complexity signal, and the top-level symbol
count, and it enforces a ``max_module_lines`` threshold that behaves like
PyLint's ``too-many-lines`` check — chosen as the "equivalent" gate the
#12321 dispatch (j#62637) authorized, so the package keeps its stdlib-only
runtime instead of taking a heavyweight lint dependency.

The gate is deliberately allowlist-driven. Existing oversized modules are *not*
split in this issue (an explicit non-goal); they are recorded in an allowlist
with a per-file ``reason`` / ``owner_issue`` / ``resolution_version`` and a
``lines`` baseline. The gate then enforces two things the issue asks for:

- a **new** oversized module (over threshold, not in the allowlist) fails; and
- an **existing** allowlisted module that *grows* past its recorded baseline
  fails (``lines > baseline``), so the known debt can only shrink, never creep.

PyLint's ``# pylint: disable=too-many-lines`` cannot carry an owner issue, a
reason, or a resolution version, which is exactly the governance metadata this
allowlist records — another reason the native gate is the better fit here.

Everything in this module is pure (filesystem reads only, no argparse, no
process exit); the CLI handlers in
:mod:`mozyo_bridge.application.commands_module_health` render and exit.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Optional

import yaml

# The default oversized-module threshold. 1000 is PyLint's own
# ``max-module-lines`` default, and it captures the modules #12321 named as
# oversized (e.g. ``presentation_grouping.py``; ``cockpit_ui.py`` was split
# under US #12323 and is no longer oversized). The value is overridable via the
# allowlist config's ``max_module_lines`` key.
DEFAULT_MAX_MODULE_LINES = 1000

# Default analysis scope: the runtime package only. ``tests/`` is intentionally
# out of the initial gate (documented as a future expansion) to keep the gate
# pragmatic and focused per the dispatch.
DEFAULT_INCLUDE = ("src/mozyo_bridge",)

# Default location of the allowlist / config document, repo-relative.
DEFAULT_CONFIG_RELPATH = "module_health.yaml"

# AST node types that contribute a decision point to the approximate
# cyclomatic-complexity signal. This is a coarse health signal, not a precise
# McCabe number; it is labelled "approximate" everywhere it surfaces.
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.AsyncWith,
    ast.IfExp,
    ast.Assert,
    ast.comprehension,
)


class ModuleHealthError(Exception):
    """Raised on an unreadable / malformed allowlist config (fail closed)."""


@dataclass(frozen=True)
class ModuleMetrics:
    """Measured health metrics for a single module."""

    path: str  # repo-relative POSIX path
    lines: int
    top_level_symbols: int
    complexity: int  # approximate; sum of decision points + def/class count

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "lines": self.lines,
            "top_level_symbols": self.top_level_symbols,
            "complexity": self.complexity,
        }


@dataclass(frozen=True)
class AllowlistEntry:
    """A recorded oversized module: why it is allowed and when it resolves."""

    path: str
    lines: int  # baseline line count; growth past this fails the gate
    reason: str
    owner_issue: str
    resolution_version: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "lines": self.lines,
            "reason": self.reason,
            "owner_issue": self.owner_issue,
            "resolution_version": self.resolution_version,
        }


@dataclass(frozen=True)
class ModuleHealthConfig:
    """Parsed ``module_health.yaml``: threshold, scope, and the allowlist."""

    max_module_lines: int = DEFAULT_MAX_MODULE_LINES
    include: tuple[str, ...] = DEFAULT_INCLUDE
    allowlist: tuple[AllowlistEntry, ...] = ()

    @property
    def allowlist_by_path(self) -> dict[str, AllowlistEntry]:
        return {entry.path: entry for entry in self.allowlist}


@dataclass(frozen=True)
class Violation:
    """A gate failure (or non-fatal warning) about one module."""

    kind: str  # see KIND_* below
    path: str
    message: str
    fatal: bool = True

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "path": self.path,
            "message": self.message,
            "fatal": self.fatal,
        }


# Violation kinds.
KIND_NEW_OVERSIZED = "new_oversized"  # over threshold, not allowlisted -> fail
KIND_GROWTH = "growth"  # allowlisted but grew past baseline -> fail
KIND_DANGLING = "dangling_allowlist"  # allowlist path not found on disk -> fail
KIND_BASELINE_BELOW_THRESHOLD = "baseline_below_threshold"  # bad config -> fail
KIND_RESOLVED = "resolved"  # allowlisted file now under threshold -> warn
KIND_SHRUNK = "shrunk"  # allowlisted file shrank below baseline -> warn


@dataclass
class GateResult:
    """The outcome of evaluating the gate over the configured scope."""

    metrics: list[ModuleMetrics] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)

    @property
    def fatal_violations(self) -> list[Violation]:
        return [v for v in self.violations if v.fatal]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if not v.fatal]

    @property
    def ok(self) -> bool:
        return not self.fatal_violations

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "metrics": [m.as_dict() for m in self.metrics],
            "violations": [v.as_dict() for v in self.violations],
        }


def count_lines(text: str) -> int:
    """Physical line count, matching how ``too-many-lines`` counts a module.

    Uses ``splitlines()`` so a trailing newline does not inflate the count and a
    file with no trailing newline still counts its last line. An empty file is 0.
    """
    return len(text.splitlines())


def module_metrics(text: str, path: str) -> ModuleMetrics:
    """Compute :class:`ModuleMetrics` for one module's source ``text``.

    ``lines`` is the physical line count. ``top_level_symbols`` counts the names
    a reader sees at module scope: top-level ``def`` / ``async def`` / ``class``
    plus module-level assignment targets (``X = ...`` / ``X: T = ...``).
    ``complexity`` is an *approximate* signal: the count of decision-point nodes
    (if/for/while/except/with/ternary/assert/comprehension and boolean operands)
    plus every function and class definition, across the whole module.

    A file that does not parse (syntax error) still yields line-based metrics;
    its symbol and complexity counts fall back to 0 rather than raising, so the
    line gate never depends on the file being importable.
    """
    lines = count_lines(text)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ModuleMetrics(path=path, lines=lines, top_level_symbols=0, complexity=0)

    top_level_symbols = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level_symbols += 1
        elif isinstance(node, ast.Assign):
            top_level_symbols += sum(len(_assigned_names(t)) for t in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            top_level_symbols += len(_assigned_names(node.target))

    complexity = 0
    for node in ast.walk(tree):
        if isinstance(node, _BRANCH_NODES):
            complexity += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            # Each extra boolean operand beyond the first is a branch.
            complexity += max(0, len(node.values) - 1)

    return ModuleMetrics(
        path=path,
        lines=lines,
        top_level_symbols=top_level_symbols,
        complexity=complexity,
    )


def _assigned_names(target: ast.expr) -> list[str]:
    """Flatten the simple ``Name`` targets of a module-level assignment."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in target.elts:
            names.extend(_assigned_names(element))
        return names
    return []


def iter_python_files(repo_root: Path, include: Iterable[str]) -> list[Path]:
    """Return the sorted ``*.py`` files under each ``include`` root/pattern.

    Each ``include`` entry is resolved relative to ``repo_root``. A directory
    entry is walked recursively for ``*.py``; ``__pycache__`` is skipped. The
    result is de-duplicated and sorted for deterministic reporting.
    """
    seen: set[Path] = set()
    for entry in include:
        base = (repo_root / entry).resolve()
        if base.is_dir():
            for candidate in base.rglob("*.py"):
                if "__pycache__" in candidate.parts:
                    continue
                seen.add(candidate)
        elif base.is_file() and base.suffix == ".py":
            seen.add(base)
    return sorted(seen)


def _relpath(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_config(
    config_path: Path, *, missing_ok: bool = True
) -> ModuleHealthConfig:
    """Parse a ``module_health.yaml`` document into a :class:`ModuleHealthConfig`.

    Fails closed (:class:`ModuleHealthError`) on an unreadable file, a non-mapping
    document, a wrong-typed field, or an allowlist entry missing a required key —
    a broken allowlist must never silently weaken the gate. A *missing* file
    resolves to the defaults when ``missing_ok`` (the gate can run on a repo that
    has not authored an allowlist yet); pass ``missing_ok=False`` to require it.
    """
    if not config_path.exists():
        if missing_ok:
            return ModuleHealthConfig()
        raise ModuleHealthError(f"module-health config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ModuleHealthError(f"cannot read {config_path}: {exc}") from exc

    if raw is None:
        return ModuleHealthConfig()
    if not isinstance(raw, dict):
        raise ModuleHealthError(f"{config_path}: top level must be a mapping")

    max_lines = raw.get("max_module_lines", DEFAULT_MAX_MODULE_LINES)
    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines <= 0:
        raise ModuleHealthError(
            f"{config_path}: max_module_lines must be a positive integer"
        )

    include_raw = raw.get("include", list(DEFAULT_INCLUDE))
    if not isinstance(include_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in include_raw
    ):
        raise ModuleHealthError(
            f"{config_path}: include must be a list of non-empty path strings"
        )
    include = tuple(include_raw) if include_raw else DEFAULT_INCLUDE

    allowlist_raw = raw.get("allowlist", [])
    if not isinstance(allowlist_raw, list):
        raise ModuleHealthError(f"{config_path}: allowlist must be a list")

    entries: list[AllowlistEntry] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(allowlist_raw):
        if not isinstance(item, dict):
            raise ModuleHealthError(
                f"{config_path}: allowlist[{index}] must be a mapping"
            )
        entry = _parse_entry(item, config_path, index)
        if entry.path in seen_paths:
            raise ModuleHealthError(
                f"{config_path}: duplicate allowlist entry for {entry.path}"
            )
        seen_paths.add(entry.path)
        entries.append(entry)

    return ModuleHealthConfig(
        max_module_lines=max_lines,
        include=include,
        allowlist=tuple(entries),
    )


_REQUIRED_ENTRY_FIELDS = ("path", "lines", "reason", "owner_issue", "resolution_version")


def _parse_entry(item: dict, config_path: Path, index: int) -> AllowlistEntry:
    for field_name in _REQUIRED_ENTRY_FIELDS:
        if field_name not in item:
            raise ModuleHealthError(
                f"{config_path}: allowlist[{index}] missing `{field_name}`"
            )
    path = item["path"]
    lines = item["lines"]
    if not isinstance(path, str) or not path.strip():
        raise ModuleHealthError(
            f"{config_path}: allowlist[{index}].path must be a non-empty string"
        )
    if not isinstance(lines, int) or isinstance(lines, bool) or lines <= 0:
        raise ModuleHealthError(
            f"{config_path}: allowlist[{index}].lines must be a positive integer"
        )
    for field_name in ("reason", "owner_issue", "resolution_version"):
        value = item[field_name]
        if not isinstance(value, str) or not value.strip():
            raise ModuleHealthError(
                f"{config_path}: allowlist[{index}].{field_name} must be a non-empty string"
            )
    return AllowlistEntry(
        path=Path(path).as_posix(),
        lines=lines,
        reason=item["reason"].strip(),
        owner_issue=item["owner_issue"].strip(),
        resolution_version=item["resolution_version"].strip(),
    )


def evaluate(repo_root: Path, config: ModuleHealthConfig) -> GateResult:
    """Measure every in-scope module and apply the oversized-module gate.

    Returns a :class:`GateResult` carrying the per-module metrics and any
    violations. Fatal violations: a new oversized module (over threshold, not
    allowlisted), an allowlisted module grown past its baseline, an allowlist
    entry whose file is missing, and an allowlist baseline that is not actually
    over the threshold (a misconfigured entry). Non-fatal warnings: an
    allowlisted module that is now under the threshold (entry can be removed) or
    that shrank below its baseline (baseline can be tightened).
    """
    result = GateResult()
    files = iter_python_files(repo_root, config.include)
    allowlist = config.allowlist_by_path
    measured_paths: set[str] = set()

    for file_path in files:
        rel = _relpath(file_path, repo_root)
        text = file_path.read_text(encoding="utf-8")
        metrics = module_metrics(text, rel)
        result.metrics.append(metrics)
        measured_paths.add(rel)

        entry = allowlist.get(rel)
        if metrics.lines > config.max_module_lines:
            if entry is None:
                result.violations.append(
                    Violation(
                        kind=KIND_NEW_OVERSIZED,
                        path=rel,
                        message=(
                            f"{rel} has {metrics.lines} lines (> "
                            f"{config.max_module_lines}); add an allowlist entry "
                            f"with reason/owner_issue/resolution_version or reduce it"
                        ),
                    )
                )
            elif metrics.lines > entry.lines:
                result.violations.append(
                    Violation(
                        kind=KIND_GROWTH,
                        path=rel,
                        message=(
                            f"{rel} grew to {metrics.lines} lines, past its "
                            f"allowlist baseline of {entry.lines}; reduce it or "
                            f"justify and raise the baseline"
                        ),
                    )
                )
            elif metrics.lines < entry.lines:
                result.violations.append(
                    Violation(
                        kind=KIND_SHRUNK,
                        path=rel,
                        message=(
                            f"{rel} shrank to {metrics.lines} lines (baseline "
                            f"{entry.lines}); tighten the allowlist baseline"
                        ),
                        fatal=False,
                    )
                )
        elif entry is not None:
            result.violations.append(
                Violation(
                    kind=KIND_RESOLVED,
                    path=rel,
                    message=(
                        f"{rel} is now {metrics.lines} lines (<= "
                        f"{config.max_module_lines}); remove its allowlist entry"
                    ),
                    fatal=False,
                )
            )

    result.metrics.sort(key=lambda m: m.lines, reverse=True)

    # Allowlist hygiene independent of the per-file scan above.
    for entry in config.allowlist:
        if entry.lines <= config.max_module_lines:
            result.violations.append(
                Violation(
                    kind=KIND_BASELINE_BELOW_THRESHOLD,
                    path=entry.path,
                    message=(
                        f"allowlist baseline for {entry.path} is {entry.lines}, "
                        f"not over the {config.max_module_lines} threshold; the "
                        f"entry is unnecessary"
                    ),
                )
            )
        if entry.path not in measured_paths and not (repo_root / entry.path).is_file():
            result.violations.append(
                Violation(
                    kind=KIND_DANGLING,
                    path=entry.path,
                    message=(
                        f"allowlisted path {entry.path} was not found in scope; "
                        f"remove the stale entry or fix its path"
                    ),
                )
            )

    return result


def default_config_path(repo_root: Path, override: Optional[str] = None) -> Path:
    """Resolve the config path: an explicit override, else the repo default."""
    if override:
        return Path(override).expanduser()
    return repo_root / DEFAULT_CONFIG_RELPATH


def filter_oversized(metrics: Iterable[ModuleMetrics], threshold: int) -> list[ModuleMetrics]:
    """Convenience: the subset of ``metrics`` whose line count exceeds ``threshold``."""
    return [m for m in metrics if m.lines > threshold]


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    """Whether ``path`` matches any ``fnmatch`` ``patterns`` (reporting helper)."""
    return any(fnmatch(path, pattern) for pattern in patterns)
