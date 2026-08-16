"""Bare ``mozyo`` adoption gate — the refusal policy (Redmine #13379).

Bare ``mozyo`` in an UNADOPTED directory used to resolve the repo root up to
an incidental ancestor marker — the home directory's ``.tmux.conf`` in the
observed trap — and silently start two real agents there (quota burn + stray
session). The gate fails closed BEFORE any session/window side effect, naming
the resolved root (the silent resolution is the surprising part) and the
adoption path. Adopted repos (config / scaffold manifest / workspace anchor
at the root, see :data:`mozyo_bridge.shared.paths.ADOPTION_MARKERS`) are
untouched, as are explicit subcommands; the herdr branch never consults this
gate and is adopted by construction (its repo-local config selected it).

The home directory is refused even WITH a marker: home is where every
unadopted cwd's walk lands, and a stray home-level manifest — e.g. a
long-forgotten ``scaffold apply`` run from home, observed live on the trap
host — would otherwise silently re-open the exact trap this gate closes. A
deliberate home cockpit remains reachable through explicit subcommands; bare
``mozyo`` never targets home.

This is a pure decision function so the policy (and its exact user-facing
wording) is testable without the launch use case's ops port.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ("adoption_refusal",)


def adoption_refusal(
    repo_root: Path,
    marker: str | None,
    home: Path | None = None,
    *,
    nested: tuple[Path, str] | None = None,
) -> str | None:
    """The bare-``mozyo`` refusal for ``repo_root``, or ``None`` to proceed.

    ``marker`` is the adoption marker found at the resolved root (see
    :func:`mozyo_bridge.shared.paths.workspace_adoption_marker`); ``home`` is
    injectable for tests and defaults to the real home directory.

    ``nested`` is the ``(directory, marker)`` this walk passed on the way up (see
    :func:`mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity
    .domain.workspace_adoption.nested_adoption_marker`). When the resolved
    root is unadopted but such a marker exists, the generic "adopt this project
    first" text tells an operator to do the thing they just did — the loop
    measured in Redmine #15526 — so the refusal names the marker instead and
    gives both routes. Omitted / ``None`` keeps the original wording byte for
    byte, which is what every pre-#15526 case still gets.
    """
    if repo_root == (home if home is not None else Path.home()):
        return (
            f"bare `mozyo` resolved repo root to the home directory "
            f"{repo_root}; refusing to start agent sessions there (an "
            "unadopted directory resolves up to incidental home markers). "
            "cd into an adopted project root, or adopt the project first: "
            "`mozyo-bridge scaffold apply <preset> --target <project_root>`."
        )
    if marker is None and nested is not None:
        nested_dir, nested_marker = nested
        return (
            f"bare `mozyo` resolved repo root {repo_root}, which is not an "
            "adopted mozyo workspace (no .mozyo-bridge/config.yaml or "
            f"scaffold/workspace marker); refusing to start agent sessions "
            f"there. A marker DOES exist below it, at {nested_dir} "
            f"({nested_marker}), but the Git worktree root is the workspace "
            "identity, so that subtree marker is deliberately not adopted "
            "(Redmine #13641: letting a subtree shadow the root would silently "
            "change the selected terminal backend). Either adopt the resolved "
            f"root — `mozyo-bridge scaffold apply <preset> --target "
            f"{repo_root}` — or, to run that subtree as its own workspace, "
            f"point the CLI at it explicitly (`--repo {nested_dir}` or "
            "MOZYO_REPO) and declare how it relates to the parent with "
            "`mozyo-bridge workspace alias`."
        )
    if marker is None:
        return (
            f"bare `mozyo` resolved repo root {repo_root}, which is not an "
            "adopted mozyo workspace (no .mozyo-bridge/config.yaml or "
            "scaffold/workspace marker); refusing to start agent sessions "
            "there. cd into an adopted project root, or adopt this project "
            "first: `mozyo-bridge scaffold apply <preset> --target "
            "<project_root>` (see `mozyo-bridge scaffold --help` for "
            "presets), then re-run `mozyo`."
        )
    return None
