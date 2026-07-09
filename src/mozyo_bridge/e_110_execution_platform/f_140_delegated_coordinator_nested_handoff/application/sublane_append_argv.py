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
    """The ``cockpit append`` argv for ``worktree_path``, incl. the #13155/#13425 model.

    Appends ``--claude-model <token>`` when the **source repo's**
    ``.mozyo-bridge/config.yaml`` (``config_root``) resolves a Claude sublane
    launch model; a missing / empty config yields the historical argv
    byte-for-byte, so an unconfigured lane is unaffected.

    Redmine #13425 (design consultation answer j#73949 Q6): the model is read
    through the single-source resolver
    ``AgentLaunchConfig.resolve_launch_argv("claude", "sublane")`` — the same
    resolver the herdr launch chokepoint consumes — so the old
    ``sublane_claude_model`` key and the new ``launch_argv.claude.sublane`` slot
    both feed the tmux path from one place. The tmux ``cockpit append`` CLI
    transports a single Claude model *token* via ``--claude-model`` (its historical
    contract), so a resolved ``["--model", <token>]`` (the old key's fold and the
    equivalent new-key shape) relays byte-for-byte. A richer resolved argv (extra
    tokens, or a non-``--model`` flag) that the single-token CLI cannot carry fails
    closed here rather than silently dropping tokens — such configs are herdr-only
    for now (recorded #13425 tmux-transport limitation).

    The config is read from ``config_root`` — the checkout ``sublane start``
    runs from — never from ``worktree_path``: at dry-run time the planned
    worktree usually does not exist yet (j#71880), and after creation its
    committed config is identical to the source repo's, so one source serves
    both the preview and the live drive consistently.
    """
    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

    argv = ["cockpit", "append", "--repo", worktree_path, "--no-attach"]
    tokens = load_repo_local_config(config_root).agent_launch.resolve_launch_argv(
        "claude", "sublane"
    )
    if tokens:
        if len(tokens) == 2 and tokens[0] == "--model":
            argv += ["--claude-model", tokens[1]]
        else:
            from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
                RepoLocalConfigError,
            )

            raise RepoLocalConfigError(
                "the tmux `cockpit append` launch transports only a single Claude "
                "model token (`--claude-model`) for the sublane slot; the configured "
                f"launch_argv.claude.sublane {list(tokens)!r} is not a single "
                "`--model <token>` pair (richer launch argv is herdr-only for now, "
                "Redmine #13425)"
            )
    return argv
