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


def resolve_append_lane_argv(worktree_path: str, *, config_root) -> list[str]:
    """The ``cockpit append`` argv for ``worktree_path``, incl. the #13155 model.

    Appends ``--claude-model <token>`` when the **source repo's**
    ``.mozyo-bridge/config.yaml`` (``config_root``) sets
    ``agent_launch.sublane_claude_model``; a missing / empty config yields the
    historical argv byte-for-byte, so an unconfigured lane is unaffected.

    The config is read from ``config_root`` — the checkout ``sublane start``
    runs from — never from ``worktree_path``: at dry-run time the planned
    worktree usually does not exist yet (j#71880), and after creation its
    committed config is identical to the source repo's, so one source serves
    both the preview and the live drive consistently.
    """
    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

    argv = ["cockpit", "append", "--repo", worktree_path, "--no-attach"]
    model = load_repo_local_config(config_root).agent_launch.sublane_claude_model
    if model:
        argv += ["--claude-model", model]
    return argv
