"""``herdr`` CLI parser group (Redmine #14654).

Feature-local parser registration for the pure-herdr session helpers, mirroring the
``cli_herdr_recovery`` / ``cli_herdr_distribution`` precedent this same group already
composes: the parsers live with the terminal-runtime-provider feature that owns them,
not in the shared ``cli_core`` assembly site. ``cli_core`` composes the group by
calling :func:`register_herdr_group` from ``register_lifecycle``.

Moved here rather than allowlisted (Redmine #14654): merging the #13249 herdr CLI
registration into ``main-next`` composed two independently-green branches into a
1007-line ``cli_core``, tripping the module-health ``new_oversized`` gate. Pure
relocation — the parsers, their flags, help text, defaults, ``func`` bindings, the
function-local imports and the registration order are moved verbatim.

``agent_choices`` is the resolved ``--agent`` vocabulary the composition root derives
once from its single injected provider snapshot and shares with ``init``; the two
shared option helpers stay owned by ``cli_core`` and are injected, so this module adds
no import back into the CLI core.
"""
from __future__ import annotations

import argparse
from typing import Callable, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_cli import (
    cmd_herdr_session_start,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_agent_attest import (
    cmd_herdr_agent_attest,
)


def register_herdr_group(
    sub,
    *,
    add_repo_option: Callable[[argparse.ArgumentParser], None],
    add_lifecycle_json: Callable[[argparse.ArgumentParser], None],
    agent_choices: Sequence[str],
) -> None:
    """Register the `herdr` command group onto the top-level subparsers ``sub``."""
    # `herdr` groups the pure-herdr session helpers (Redmine #13261). `session-start`
    # is the opt-in write side: it mints durable herdr assigned names for the
    # workspace's `claude` / `codex` agents and injects their self-identity env so the
    # herdr-native target resolution has stable identities to resolve against. Not
    # coupled to the `terminal_transport.backend` flag; in pure-herdr operation both
    # are used together.
    herdr = sub.add_parser(
        "herdr",
        help=(
            "Pure-herdr session helpers (Redmine #13261): mint durable herdr "
            "assigned names for the workspace's agents (session-start)."
        ),
    )
    herdr_sub = herdr.add_subparsers(dest="herdr_command", required=True)
    # `attestation-store` (Redmine #13882) is the public maintenance rail for the
    # home-scoped self-attestation store. A feature-local parser module, per the
    # `cli_sublane_retire` precedent, so this near-ceiling module gains no flags.
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_attestation_store import (  # noqa: E501
        register_herdr_attestation_store_parser,
    )
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_launch_generation_store import (  # noqa: E501
        register_herdr_launch_generation_store_parser,
    )

    register_herdr_attestation_store_parser(herdr_sub, add_repo_option=add_repo_option)
    register_herdr_launch_generation_store_parser(
        herdr_sub, add_repo_option=add_repo_option
    )
    # Redmine #14838 Phase A: one global, drift-checked, side-effect-zero rollout plan.
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.cli_herdr_offline_rollout import (  # noqa: E501
        register_herdr_offline_rollout_parser,
    )

    register_herdr_offline_rollout_parser(
        herdr_sub, add_repo_option=add_repo_option
    )
    # Redmine #13892 / #13948: every herdr session recovery surface, in one call.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_recovery import (  # noqa: E501
        register_herdr_recovery_surfaces,
    )

    register_herdr_recovery_surfaces(herdr_sub, add_repo_option=add_repo_option)
    # Redmine #13249: the distribution surface — the supply-chain pin posture
    # (pin-posture) and the opt-in Claude/Codex session-hook installer
    # (integration-install). One registrar call, mirroring the recovery surfaces.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_distribution import (  # noqa: E501
        register_herdr_distribution_surfaces,
    )

    register_herdr_distribution_surfaces(herdr_sub, add_repo_option=add_repo_option)
    # Redmine #14608: preview-first, identity/generation-bound live pair placement.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_live_pair_placement import (  # noqa: E501
        register_herdr_pair_placement_parser,
    )

    register_herdr_pair_placement_parser(
        herdr_sub, add_repo_option=add_repo_option
    )
    # Redmine #14065: the read-only composer-render measurement diagnostic (phase 1).
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_composer_render_cli import (  # noqa: E501
        register_herdr_composer_render_parser,
    )

    register_herdr_composer_render_parser(herdr_sub)
    # Redmine #14187: the read-only isolated shared_space smoke preflight (no agent
    # actuation; the live cross-process smoke is the #14185 driver's job).
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_cli import (  # noqa: E501
        register_herdr_smoke_shared_space_parser,
    )

    register_herdr_smoke_shared_space_parser(herdr_sub)
    # Redmine #15114: read-only coordinator Unit board + display-only metadata.
    # Kept feature-local so the shared CLI composition root gains no surface.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_unit_board import (
        register_herdr_unit_board_parser,
    )

    register_herdr_unit_board_parser(herdr_sub)
    herdr_session_start = herdr_sub.add_parser(
        "session-start",
        help=(
            "Prepare a pure-herdr session: register the workspace, launch (or adopt) "
            "the requested `claude` / `codex` agents as herdr-managed panes pinned to "
            "the repo root, mint their durable `mzb1_...` assigned names, and inject "
            "the self-identity env (MOZYO_WORKSPACE_ID / MOZYO_AGENT_ROLE / "
            "MOZYO_LANE_ID). Idempotent: an agent already carrying the slot's durable "
            "name is adopted, not re-launched. The herdr binary comes only from the "
            "trusted environment (MOZYO_HERDR_BINARY)."
        ),
    )
    herdr_session_start.add_argument(
        "--agent",
        dest="agent",
        action="append",
        choices=agent_choices,
        help="Provider agent to prepare (repeatable). Default: both claude and codex.",
    )
    herdr_session_start.add_argument(
        "--lane",
        dest="lane",
        default=None,
        help="Lane id for the minted identities (default: the workspace-default lane).",
    )
    herdr_session_start.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Plan only: report which slots would launch / adopt without any side "
        "effect — no launch, no rename, and no workspace registration / anchor write "
        "(Redmine #13595). An unregistered workspace fails closed with actionable "
        "guidance rather than being registered.",
    )
    add_repo_option(herdr_session_start)
    add_lifecycle_json(herdr_session_start)
    herdr_session_start.set_defaults(func=cmd_herdr_session_start)

    # `agent-attest` is the managed-launch wrapper (Redmine #13637): the launch
    # execs the provider THROUGH this command so the agent's own process can
    # self-inspect its injected identity env before `exec`ing the provider, and
    # record a generation-bound startup self-attestation. It is not an operator
    # command — it is the wrapper the launch argv points at.
    # This subcommand's `--help` is also the managed-launch **capability contract**: the
    # preflight probes a candidate launcher with it and joins what the launcher advertises
    # against what it will actually be pointed at — the attestation store schema it must
    # write (#13847) and the store shapes it can write (#13882), the repo-local config it
    # must parse and the shared lane lifecycle schema it must read (#14258), and the
    # launch-generation wire protocol it must speak (#14203). Every token is composed by the
    # single canonical producer below, built from the constants of the authorities it
    # describes, so a schema bump anywhere re-renders here with no edit and no producer can
    # fall behind. A launcher predating a token advertises none, cannot be proven
    # compatible, and fails the preflight closed before any process launch.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (
        build_attest_capability_epilog,
    )

    herdr_agent_attest = herdr_sub.add_parser(
        "agent-attest",
        help=(
            "Managed-launch internal wrapper (Redmine #13637): self-inspect this "
            "agent's injected identity env, record a generation-bound startup "
            "self-attestation, then exec the provider given after `--`."
        ),
        # RawDescriptionHelpFormatter so the capability contract tokens in the epilog are
        # emitted VERBATIM: argparse's default formatter reflows the epilog and would split
        # a token across lines (measured), making a capable launcher's probe read as
        # incapable. Each token is on its own line, unwrapped, so a launcher's `--help` (the
        # preflight probe input) carries them intact.
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=build_attest_capability_epilog(),
    )
    herdr_agent_attest.add_argument("--assigned-name", dest="assigned_name", default="")
    herdr_agent_attest.add_argument("--workspace-id", dest="workspace_id", default="")
    herdr_agent_attest.add_argument("--role", dest="role", default="")
    herdr_agent_attest.add_argument("--lane", dest="lane", default="")
    herdr_agent_attest.add_argument(
        "--workflow-role",
        dest="workflow_role",
        default="",
        help=(
            "Governed runtime responsibility observed independently from the provider "
            "identity (empty for a legacy role-unaware launch)."
        ),
    )
    herdr_agent_attest.add_argument(
        "--replacement-action-id",
        dest="replacement_action_id",
        default="",
        help=(
            "Redmine #13806 tranche D: the replacement transaction action_id that launched "
            "this process (empty on a normal launch); recorded into the startup self-attestation "
            "so a recovery can verify the exact action bound its fresh worker."
        ),
    )
    herdr_agent_attest.add_argument(
        "--lane-epoch",
        dest="lane_epoch",
        default="",
        help=(
            "Redmine #14756: the lane epoch the lifecycle authority had minted when this "
            "launch was planned (empty when the lane has none). The launcher-EXPECTED "
            "value only — what gets recorded is the epoch this process actually observes "
            "in its own env, so a disagreement is reported rather than papered over."
        ),
    )
    herdr_agent_attest.add_argument(
        "provider_argv",
        nargs=argparse.REMAINDER,
        help="The provider command to exec, after a `--` separator.",
    )
    herdr_agent_attest.set_defaults(func=cmd_herdr_agent_attest)
