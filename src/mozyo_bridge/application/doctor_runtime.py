"""Runtime fingerprint for mozyo-bridge (Redmine #12612).

`mozyo-bridge doctor runtime` proves *which executable surface* is under test
before a delivery-rail dogfood / pre-smoke / release-adjacent verification trusts
it. It exists because a stale installed CLI can report the same ``__version__``
as the source tree while silently lacking a gate-critical behavior (the
originating case: pipx ``mozyo-bridge 0.9.0`` lacked #12597
``standard_target_admission`` that ``origin/main`` source ``0.9.0`` has, so the
#12608 dogfood failed on a runtime that *looked* current — Redmine #12612).

Where `doctor` answers "is the environment healthy" and `doctor`'s cli section
already flags *path* drift (running install vs repo-local ``src/``), this command
adds the missing layer the version string and path comparison cannot give:
**feature probes**. It probes the *active* loaded package live (does it actually
expose the behavior) and the repo-local source *textually* (does the checkout
ship the behavior), then fails clearly when the versions match but the probes
differ — exactly the silent-drift class that version output cannot distinguish.

Read-only. This module never installs, reinstalls, writes, tags, publishes, or
contacts a network/ticket system. It only reports the fingerprint and the next
command an operator should run (use the repo-local CLI during dogfooding).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import mozyo_bridge
from mozyo_bridge import __version__
from mozyo_bridge.application.doctor import (
    REPO_LOCAL_INVOCATION,
    _read_source_version,
    doctor_target,
)

# Gate-critical feature probes. Each entry maps a probe key to the textual
# marker that proves the *source tree* ships the behavior. The markers are
# **definition-anchored** (the function `def` and the quoted CLI-flag literal as
# it appears in `add_argument`), not bare symbol mentions, so an import / call /
# prose reference does not satisfy the probe — only the real definition does.
# They are deliberately not file paths so the probe survives the in-flight
# features/<epic_slug>/ layout migration. The *active* surface is probed live
# instead — see `_active_feature_probes` — because a live import / parser walk is
# the authoritative answer to "does this runtime actually have the behavior".
#
# This diagnostic module itself carries these marker literals (in the dict below
# and in `_probe_active_no_target_activation`), so scanning it would let the
# source probe self-satisfy even if the real implementation were deleted
# (Redmine #12612 review j#65856). `_source_feature_probes` therefore skips the
# diagnostic module by name — see SOURCE_PROBE_SCAN_EXCLUDE — and the markers are
# definition-anchored so the module's own string literals could not match anyway.
SOURCE_PROBE_MARKERS = {
    # Redmine #12597: inactive-split admission/activation policy. Anchored on the
    # resolver definition in `domain/handoff.py`, not a mention/import/call.
    "standard_target_admission": "def resolve_standard_target_admission_policy",
    # Redmine #12597: the queue-enter opt-out flag on `handoff send`. Anchored on
    # the quoted flag literal as it appears in the `add_argument` registration.
    "no_target_activation": '"--no-target-activation"',
}

# The diagnostic module's own filename, excluded from the source scan so its
# marker definitions cannot self-satisfy the source probe (Redmine #12612 j#65856).
SOURCE_PROBE_SCAN_EXCLUDE = "doctor_runtime.py"

# Drift relation / status vocabulary. `drifted` is intentionally one of
# doctor's BAD_SECTION_STATUSES so a gate consuming either surface treats a
# probe mismatch as needs-attention.
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_DRIFTED = "drifted"


def _git_anchor(root: Path) -> dict[str, Any]:
    """Best-effort git anchor (branch / short HEAD / dirty) for ``root``.

    Tolerant like the rest of doctor: a non-repo path, a missing git binary, or
    any git error yields ``{"is_repo": False}`` rather than raising.
    """
    anchor: dict[str, Any] = {"root": str(root), "is_repo": False}

    def _git(*git_args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *git_args],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return (result.stdout or "").strip()

    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return anchor
    anchor["is_repo"] = True
    anchor["branch"] = _git("branch", "--show-current") or None
    anchor["head"] = _git("rev-parse", "--short", "HEAD") or None
    status = _git("status", "--porcelain")
    # `status` is "" when clean, a non-empty listing when dirty, None on error.
    anchor["dirty"] = bool(status) if status is not None else None
    return anchor


def classify_surface(package_path: str, source_pkg: Path) -> str:
    """Classify the active package path into an executable-surface label.

    `source_tree` when the running package *is* the checkout's
    ``src/mozyo_bridge`` (editable install or ``PYTHONPATH=src``) or otherwise
    lives under a ``src`` tree; `pipx` / `site_packages` for the common installed
    layouts; `unknown` when none match.
    """
    try:
        resolved = Path(package_path).resolve()
    except OSError:
        resolved = Path(package_path)
    if resolved == source_pkg.resolve():
        return "source_tree"
    text = str(resolved)
    if f"{os.sep}pipx{os.sep}" in text:
        return "pipx"
    if f"{os.sep}site-packages{os.sep}" in text or f"{os.sep}dist-packages{os.sep}" in text:
        return "site_packages"
    if f"{os.sep}src{os.sep}mozyo_bridge" in text:
        return "source_tree"
    return "unknown"


def _walk_subparser(
    parser: argparse.ArgumentParser, *names: str
) -> argparse.ArgumentParser | None:
    """Walk a chain of subcommand names down an argparse parser tree."""
    current: argparse.ArgumentParser = parser
    for name in names:
        action = next(
            (a for a in current._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )
        if action is None or name not in action.choices:
            return None
        current = action.choices[name]
    return current


def _probe_active_standard_target_admission() -> bool:
    """Live probe: does the loaded package expose the #12597 admission policy."""
    try:
        module = importlib.import_module("mozyo_bridge.domain.handoff")
    except Exception:
        return False
    return hasattr(module, "resolve_standard_target_admission_policy")


def _probe_active_no_target_activation() -> bool:
    """Live probe: does the assembled `handoff send` CLI carry the flag."""
    try:
        from mozyo_bridge.application.cli import build_parser

        parser = build_parser()
    except Exception:
        return False
    send = _walk_subparser(parser, "handoff", "send")
    if send is None:
        return False
    options = {opt for action in send._actions for opt in action.option_strings}
    return "--no-target-activation" in options


def _active_feature_probes() -> dict[str, bool]:
    """Probe the *active* loaded package for each gate-critical behavior."""
    return {
        "standard_target_admission": _probe_active_standard_target_admission(),
        "no_target_activation": _probe_active_no_target_activation(),
    }


def _source_feature_probes(source_pkg: Path) -> dict[str, bool] | None:
    """Textually probe the repo-local source tree for each behavior.

    Returns None when ``source_pkg`` is not a directory (no repo-local source to
    compare against). Scans ``*.py`` for each marker and short-circuits once all
    markers are found, so a hit is O(files-until-found), not the whole tree.

    The diagnostic module itself (``SOURCE_PROBE_SCAN_EXCLUDE``) is skipped: it
    carries the marker literals, so scanning it would self-satisfy the source
    probe even if the real implementation were gone (Redmine #12612 j#65856).
    """
    if not source_pkg.is_dir():
        return None
    found = {key: False for key in SOURCE_PROBE_MARKERS}
    remaining = set(SOURCE_PROBE_MARKERS)
    for path in source_pkg.rglob("*.py"):
        if not remaining:
            break
        if path.name == SOURCE_PROBE_SCAN_EXCLUDE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for key in list(remaining):
            if SOURCE_PROBE_MARKERS[key] in text:
                found[key] = True
                remaining.discard(key)
    return found


def evaluate_fingerprint(
    active: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Pure decision over an active-surface and source-tree fingerprint.

    Returns ``{"status", "ok", "relation", "probe_mismatch", "summary"}``.

    The headline failure (``drifted``) is a gate-critical behavior present in the
    source tree but absent from the active surface *while the version strings
    match* — the silent drift that #12612 exists to surface. A probe gap with
    differing versions, or a path drift with matching probes, is a softer
    ``warning`` (still non-ok so a gate cannot pass on it unnoticed).
    """
    if not source.get("present"):
        return {
            "status": STATUS_OK,
            "ok": True,
            "relation": "no-source",
            "probe_mismatch": [],
            "summary": (
                "no repo-local source to compare against; the active installed "
                "CLI is the whole story"
            ),
        }
    if active.get("surface") == "source_tree" and (
        active.get("package_path") and source.get("package_path")
        and Path(active["package_path"]).resolve()
        == Path(source["package_path"]).resolve()
    ):
        return {
            "status": STATUS_OK,
            "ok": True,
            "relation": "active-is-source",
            "probe_mismatch": [],
            "summary": "the active surface is the repo-local source tree",
        }

    active_probes = active.get("feature_probes") or {}
    source_probes = source.get("feature_probes") or {}
    mismatch = [
        {"probe": key, "source": True, "active": bool(active_probes.get(key))}
        for key, present in source_probes.items()
        if present and not active_probes.get(key)
    ]
    versions_match = bool(active.get("version")) and active.get("version") == source.get(
        "version"
    )

    if mismatch and versions_match:
        return {
            "status": STATUS_DRIFTED,
            "ok": False,
            "relation": "same-version-probe-drift",
            "probe_mismatch": mismatch,
            "summary": (
                "active surface and repo-local source report the same version "
                f"({active.get('version')}) but the active runtime is missing "
                "gate-critical behavior the source ships "
                f"({', '.join(m['probe'] for m in mismatch)}); the runtime under "
                "test is stale despite the matching version string"
            ),
        }
    if mismatch:
        return {
            "status": STATUS_WARNING,
            "ok": False,
            "relation": "version-differs-probe-drift",
            "probe_mismatch": mismatch,
            "summary": (
                "active surface is missing gate-critical behavior the source "
                f"ships ({', '.join(m['probe'] for m in mismatch)}); versions "
                f"also differ (active {active.get('version')} vs source "
                f"{source.get('version')})"
            ),
        }
    return {
        "status": STATUS_WARNING,
        "ok": False,
        "relation": "same-version" if versions_match else "version-differs",
        "probe_mismatch": [],
        "summary": (
            "active surface differs from the repo-local source by path "
            "(feature probes match, but equal/parallel versions do not guarantee "
            "equal commits during dogfooding); prefer the repo-local CLI"
        ),
    }


def run_runtime_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    """Build the full runtime fingerprint result for ``doctor runtime``."""
    target = doctor_target(args)
    source_pkg = (target / "src" / "mozyo_bridge").resolve()
    source_init = source_pkg / "__init__.py"

    active_package_path = Path(mozyo_bridge.__file__).resolve().parent
    active: dict[str, Any] = {
        "version": __version__,
        "executable": shutil.which("mozyo-bridge") or "",
        "python": sys.executable,
        "package_file": str(Path(mozyo_bridge.__file__).resolve()),
        "package_path": str(active_package_path),
        "surface": classify_surface(str(active_package_path), source_pkg),
        "feature_probes": _active_feature_probes(),
    }

    source_present = source_init.is_file()
    source: dict[str, Any] = {
        "present": source_present,
        "package_path": str(source_pkg) if source_present else "",
        "version": _read_source_version(source_init) if source_present else None,
        "feature_probes": _source_feature_probes(source_pkg)
        if source_present
        else None,
    }

    verdict = evaluate_fingerprint(active, source)
    next_action: list[str] = []
    if not verdict["ok"]:
        next_action.append(
            "run the repo-local CLI for dogfood / pre-smoke verification: "
            f"{REPO_LOCAL_INVOCATION} <args>"
        )
        if verdict["status"] == STATUS_DRIFTED:
            next_action.append(
                "do NOT classify this as a behavior bug or PASS evidence; the "
                "runtime under test is stale. Reinstall/upgrade is owner-gated "
                "(no pipx reinstall, tag, publish, or release without approval)."
            )

    return {
        "ok": verdict["ok"],
        "status": verdict["status"],
        "relation": verdict["relation"],
        "summary": verdict["summary"],
        "probe_mismatch": verdict["probe_mismatch"],
        "active": active,
        "source": source,
        "repo": _git_anchor(target),
        "next_action": next_action,
    }


def format_runtime_text(result: dict[str, Any]) -> str:
    """Render the fingerprint as human-readable text."""
    active = result["active"]
    source = result["source"]
    repo = result["repo"]
    lines: list[str] = []
    flag = "OK" if result["ok"] else result["status"].upper()
    lines.append(f"runtime fingerprint: {flag}")
    lines.append(f"  {result['summary']}")
    lines.append("active surface:")
    lines.append(f"  surface:      {active['surface']}")
    lines.append(f"  version:      {active['version']}")
    lines.append(f"  executable:   {active['executable'] or '(not on PATH)'}")
    lines.append(f"  python:       {active['python']}")
    lines.append(f"  package:      {active['package_file']}")
    lines.append(
        "  probes:       "
        + ", ".join(f"{k}={v}" for k, v in sorted(active["feature_probes"].items()))
    )
    if source["present"]:
        lines.append("repo-local source:")
        lines.append(f"  package:      {source['package_path']}")
        lines.append(f"  version:      {source['version'] or '(unreadable)'}")
        probes = source["feature_probes"] or {}
        lines.append(
            "  probes:       "
            + ", ".join(f"{k}={v}" for k, v in sorted(probes.items()))
        )
    else:
        lines.append("repo-local source: (none under --repo/cwd)")
    if repo.get("is_repo"):
        dirty = repo.get("dirty")
        dirty_label = "dirty" if dirty else ("clean" if dirty is False else "unknown")
        lines.append(
            f"git anchor:     {repo.get('branch') or '(detached)'} @ "
            f"{repo.get('head') or '?'} ({dirty_label})"
        )
    if result["probe_mismatch"]:
        lines.append("probe mismatch (source ships, active lacks):")
        for item in result["probe_mismatch"]:
            lines.append(f"  - {item['probe']}")
    for action in result["next_action"]:
        lines.append(f"next: {action}")
    return "\n".join(lines)


def cmd_doctor_runtime(args: argparse.Namespace) -> int:
    """`doctor runtime` handler: print the fingerprint, exit non-zero on drift."""
    result = run_runtime_fingerprint(args)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_runtime_text(result))
    return 0 if result["ok"] else 1
