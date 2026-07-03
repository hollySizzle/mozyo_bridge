"""Shared ``cockpit append`` argv resolver for a sublane lane (Redmine #13155).

The single source of truth for the argv the sublane creation-side actuator uses
when it appends a lane column. The *live* append (``LiveSublaneActuatorOps``)
drives it, and the ``sublane start --dry-run`` *preview* renders its command
string from it, so both reflect the repo-configured Claude launch model
identically — the dry-run visibility the #13126 (j#71786) design constraint
requires: an operator can confirm, before launch, that the worker stands up on
the configured model.

Kept in its own tiny module so the resolver is not duplicated between the live
and preview paths and the actuator module's line budget does not grow.
"""

from __future__ import annotations


def resolve_append_lane_argv(worktree_path: str) -> list[str]:
    """The ``cockpit append`` argv for ``worktree_path``, incl. the #13155 model.

    Appends ``--claude-model <token>`` when the lane worktree's
    ``.mozyo-bridge/config.yaml`` sets ``agent_launch.sublane_claude_model``; a
    missing / empty config yields the historical argv byte-for-byte, so an
    unconfigured lane is unaffected.
    """
    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

    argv = ["cockpit", "append", "--repo", worktree_path, "--no-attach"]
    model = load_repo_local_config(worktree_path).agent_launch.sublane_claude_model
    if model:
        argv += ["--claude-model", model]
    return argv
