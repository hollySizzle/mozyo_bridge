"""`release check drift` — canonical renderer + skill mirror drift gates.

Split out of ``release.py`` by Redmine #14580. The command grew a third
sub-check (the legacy project Claude skill partial mirror) and pushed
``release.py`` past its module-health allowlist baseline; extracting the whole
``release check drift`` surface is the split the gate asks for, not a baseline
bump. Design source: ``vibes/docs/logics/release-helper-contract.md`` ->
``release check drift`` and ``vibes/docs/logics/release-flow.md`` ->
``Canonical Renderer / Skill Mirror Drift``.

Import direction is one-way (this module imports shared helpers from
``release``; ``release`` does not import this module) so the split introduces
no import cycle. The CLI wiring imports :func:`cmd_release_check_drift` from
here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .release import (
    EXIT_BLOCKER,
    EXIT_CLEAN,
    _print_section,
    _require_command,
    _run,
    resolve_repo_root,
)

_PLUGIN_SKILL_SYNC_RELATIVE = Path("scripts/sync_plugin_skill.sh")
_LEGACY_PROJECT_SKILL_SYNC_RELATIVE = Path("scripts/sync_legacy_project_skill.sh")


#: Recovery guidance per mirror gate. Kept per-gate rather than generated from
#: the script name because the two mirrors do not have the same recovery
#: contract, and review j#90322 F1 showed that stating one fix for every drift
#: class sends the operator to a command that cannot resolve theirs. The plugin
#: sync is a full `rsync -a --delete`, so rerunning it clears every class it
#: reports. The legacy partial sync deliberately refuses to delete an unpinned
#: reference, so that class needs a reviewed disposition first.
_PLUGIN_MIRROR_RECOVERY = (
    "rerun `scripts/sync_plugin_skill.sh` (no --check, from the repo root) and recommit."
)
_LEGACY_PROJECT_MIRROR_RECOVERY = (
    "follow the disposition the sub-check printed above: content drift and a missing "
    "mirrored file are cleared by rerunning `scripts/sync_legacy_project_skill.sh` "
    "(no --check, from the repo root); an unpinned entry is not — that sync refuses "
    "while one is present and never deletes it for you. Then recommit."
)


def _run_skill_mirror_check(
    repo_root: Path,
    script_relative: Path,
    *,
    label: str,
    recovery: str,
    blockers: list[str],
) -> None:
    """Run one skill-mirror ``--check`` script and record a blocker on failure.

    Shared by the plugin (full) and legacy project (partial) mirror gates so
    the two cannot diverge in how a non-zero exit or a missing script is
    handled. A missing script is itself a blocker: the gate would otherwise
    pass silently because nothing ran. ``recovery`` stays per-gate because the
    two mirrors have different recovery contracts (see the constants above).
    """
    script = repo_root / script_relative
    if not script.is_file():
        print(f"missing sync script: {script}")
        blockers.append(
            f"{label} sync script missing at {script}; "
            "restore from the repo or branch source."
        )
        return
    _require_command("sh")
    result = _run(["sh", str(script), "--check"], cwd=repo_root)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode != 0:
        blockers.append(f"{label} drift detected; {recovery}")


def cmd_release_check_drift(args: argparse.Namespace) -> int:
    """Run canonical-renderer and skill-mirror drift gates as one release check.

    Bundles the pre-existing drift gates so a release operator (and CI)
    can fail fast on canonical-rendered guardrail output drift or on either
    skill mirror drifting, without invoking the unit-test suite. Reproduces:

    - ``mozyo-bridge scaffold canonical --check --repo <root>`` (Redmine
      #10345 / #10426): router pair + governed preset workflow pair.
    - ``scripts/sync_plugin_skill.sh --check`` (Redmine #10663): plugin
      skill mirror (`plugins/mozyo-bridge-agent/skills/...`), a full byte
      mirror.
    - ``scripts/sync_legacy_project_skill.sh --check`` (Redmine #14580):
      legacy project Claude skill *partial* mirror
      (`.claude/skills/mozyo-bridge-agent/references/...`). Added because
      that mirror had no sync path or gate here at all, so commit
      ``7ca3380f`` updated canonical + the plugin mirror and silently left
      it behind; the parity regression only surfaced later in a full-suite
      run, which the focused pre-commit lane deliberately does not perform.

    Honors the ``release check`` family invariants: read-only, idempotent,
    strict-fail (exit 1) on any drift, no implicit mutation. Each sub-check
    runs independently so a clean tree on one side still fails the
    overall command if another side drifted.
    """
    repo_root = resolve_repo_root(getattr(args, "repo", None))
    blockers: list[str] = []

    _print_section("scaffold canonical --check")
    # The canonical check must run the *target tree's* package: staged
    # release copies (and dev checkouts under an interpreter without the
    # package installed) are not importable as `mozyo_bridge` from the
    # subprocess's default sys.path, so prepend the target `src` layout.
    canonical_env = os.environ.copy()
    target_src = repo_root / "src"
    if (target_src / "mozyo_bridge" / "__init__.py").is_file():
        existing_pythonpath = canonical_env.get("PYTHONPATH")
        canonical_env["PYTHONPATH"] = (
            str(target_src)
            if not existing_pythonpath
            else f"{target_src}{os.pathsep}{existing_pythonpath}"
        )
    canonical = _run(
        [sys.executable, "-m", "mozyo_bridge", "scaffold", "canonical", "--check", "--repo", str(repo_root)],
        cwd=repo_root,
        env=canonical_env,
    )
    if canonical.stdout:
        print(canonical.stdout, end="" if canonical.stdout.endswith("\n") else "\n")
    if canonical.stderr:
        print(canonical.stderr, end="" if canonical.stderr.endswith("\n") else "\n")
    if canonical.returncode != 0:
        blockers.append(
            "scaffold canonical drift detected; rerun "
            "`mozyo-bridge scaffold canonical` (no --check) and recommit."
        )

    _print_section("sync_plugin_skill.sh --check")
    _run_skill_mirror_check(
        repo_root,
        _PLUGIN_SKILL_SYNC_RELATIVE,
        label="plugin skill mirror",
        recovery=_PLUGIN_MIRROR_RECOVERY,
        blockers=blockers,
    )

    _print_section("sync_legacy_project_skill.sh --check")
    _run_skill_mirror_check(
        repo_root,
        _LEGACY_PROJECT_SKILL_SYNC_RELATIVE,
        label="legacy project skill mirror",
        recovery=_LEGACY_PROJECT_MIRROR_RECOVERY,
        blockers=blockers,
    )

    print("")
    if blockers:
        print("result: blocker")
        for item in blockers:
            print(f"- {item}")
        return EXIT_BLOCKER
    print("result: clean")
    return EXIT_CLEAN
