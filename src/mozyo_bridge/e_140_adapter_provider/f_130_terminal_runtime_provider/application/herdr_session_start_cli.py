"""`herdr session-start` CLI presentation + command surface (Redmine #13261).

Split out of :mod:`.herdr_session_start` (Redmine #13882, design answer j#80190 /
supersession j#80207, module-split approval j#80207). The use case there had reached the
1000-line module-health ceiling exactly, so the launch-admission shared lock that ruling
requires could not be added at all. Presentation and the argparse handler are the natural
seam: they render and exit, while the module they left decides and actuates.

Behavior-preserving by construction — the rendering, the exit codes and the JSON payload
are the same objects that were here before, only relocated. `cli_core` binds
:func:`cmd_herdr_session_start` from here.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (  # noqa: E501
    COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_session_start as _use_case,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    SessionStartResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E501
    COMPENSATION_NOT_NEEDED,
)


def _render_text(result: SessionStartResult) -> str:
    lines = [
        f"herdr session-start: workspace={result.workspace_id} lane={result.lane_id}"
    ]
    if result.herdr_tab_id:
        lines[0] += f" tab={result.herdr_tab_id}"
    for slot in result.slots:
        line = (
            f"  - {slot.provider}: {slot.outcome} name={slot.assigned_name}"
            + (f" locator={slot.locator}" if slot.locator else "")
        )
        # Redmine #13948: the outcome is what the launcher did; the health is what is
        # actually there. Say both — the defect was a run that printed `launched` twice
        # and exited 0 while one of the two panes had already died.
        if not result.dry_run:
            line += f" health={slot.health}"
            if slot.blocker_id:
                line += f" blocker={slot.blocker_id}"
            if slot.compensation != COMPENSATION_NOT_NEEDED:
                line += f" compensation={slot.compensation}"
        lines.append(line)
        if not slot.healthy and slot.health_detail:
            lines.append(f"      {slot.health_detail}")
    if result.base_pane_id:
        state = (
            "reclaimed"
            if result.base_pane_reclaimed
            else f"reclaim-failed ({result.base_pane_detail})"
        )
        lines.append(f"base pane {result.base_pane_id}: {state}")
    if result.tab_pane_id:
        state = (
            "reclaimed"
            if result.tab_pane_reclaimed
            else f"reclaim-failed ({result.tab_pane_detail})"
        )
        lines.append(f"tab root pane {result.tab_pane_id}: {state}")
    if not result.ok:
        # Name the next action. A partial pair used to be silent (exit 0), which is how
        # #13882 j#80951 spent a dogfood cycle discovering it from a follow-up dry-run.
        lines.append(
            "session-start did NOT fully succeed: at least one requested role is not "
            "live-and-attested (see health above). This run closed nothing."
        )
        if any(s.compensation != COMPENSATION_NOT_NEEDED for s in result.slots):
            lines.append(
                "  a fresh launch of this run is owed a rollback: converge it with the "
                "explicit public rollback rail, not by hand."
            )
    return "\n".join(lines)


def cmd_herdr_session_start(args: argparse.Namespace) -> int:
    """CLI entry: prepare durable herdr identities for the workspace's agents."""
    from mozyo_bridge.application.commands_common import repo_root_from_args

    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        RepoLocalConfigError,
    )

    repo_root = repo_root_from_args(args)
    agents = getattr(args, "agent", None) or [PROVIDER_CLAUDE, PROVIDER_CODEX]
    lane_id = getattr(args, "lane", None) or ""
    dry_run = bool(getattr(args, "dry_run", False))
    # Config-driven launch argv (Redmine #13425) + pane placement (Redmine #13646):
    # resolved from the repo the command runs in. lane_class is derived inside
    # `prepare_session` from the resolved lane. One load serves both surfaces.
    try:
        repo_config = load_repo_local_config(repo_root)
    except RepoLocalConfigError as exc:
        die(f"herdr session-start failed: invalid repo-local config: {exc}")
        raise AssertionError("unreachable")
    agent_launch = repo_config.agent_launch
    lane_placement = repo_config.lane_placement
    try:
        result = _use_case.prepare_session(
            repo_root=repo_root,
            providers=list(agents),
            lane_id=lane_id,
            env=os.environ,
            dry_run=dry_run,
            claude_permission_mode_default=COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
            agent_launch=agent_launch,
            lane_placement=lane_placement,
        )
    except HerdrSessionStartError as exc:
        die(f"herdr session-start failed: {exc}")
        raise AssertionError("unreachable")
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(result))
    # Redmine #13948 Acceptance 1: a partial pair is not a success. The exit code used to
    # be a hardcoded 0 — there was no axis on which a started-but-dead role could be
    # reported, so `session-start` told the truth it had and it was the wrong one.
    return 0 if result.ok else 1


__all__ = ("cmd_herdr_session_start",)
