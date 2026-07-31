"""CLI parser registration for the core (non-feature) command set.

Split out of ``application/cli.py`` (Redmine #12155) so the residual inline
``build_parser()`` blocks compose through the internal module registry like the
feature families (Redmine #12153 / #12154) already do. Behavior-preserving: the
block text is moved verbatim from ``build_parser()`` so help / choices /
defaults / dest / ``func`` bindings are unchanged, and the registrars are called
in the same order, so the top-level subcommand sequence is identical.

The core families are the hard command set — pane discovery / I/O / lifecycle /
diagnostics — that the registry marks ``core`` (mandatory, never config-disabled).
They are interleaved with the feature families in ``build_parser()``, so they are
registered as four ordered entry points rather than one block:

- :func:`register_top` — ``status`` / ``list``
- :func:`register_pane_io` — ``id`` / ``resolve`` / ``read`` / ``type``
- :func:`register_keys` — ``keys``
- :func:`register_lifecycle` — ``init`` / ``doctor`` (+ ``doctor instruction`` /
  ``doctor runtime``), then the ``sublane`` and ``herdr`` groups

The ``sublane`` and ``herdr`` groups are NOT core commands — they belong to the
delegated-coordinator and terminal-runtime-provider features — so their parsers live
with those features (``cli_sublane_group`` / ``cli_herdr_group``) and this module
composes them with one call each (Redmine #14654). That keeps this assembly site the
non-feature command set its name claims, and stops additive feature wiring from
growing it into the module-health ceiling, which is how the #13249 integration merge
produced a 1007-line ``new_oversized`` failure out of two green branches.

The two option helpers every group shares — ``add_repo_option`` (from ``cli_common``)
and :func:`_add_lifecycle_json` — stay owned here and are injected into the group
registrars, per the ``cli_sublane_retire`` / ``cli_herdr_recovery`` precedent, so no
feature module imports back into the CLI core.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    agent_provider_ids,
)
from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.commands import (
    cmd_doctor,
    cmd_doctor_instruction,
    cmd_id,
    cmd_init,
    cmd_keys,
    cmd_list,
    cmd_read,
    cmd_resolve,
    cmd_status,
    cmd_type,
)
from mozyo_bridge.application.doctor_runtime import cmd_doctor_runtime
from mozyo_bridge.application.instruction_doctor import (
    KNOWN_PROFILES,
    PROFILE_REDMINE_CODEX,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_sublane_group import (  # noqa: E501
    register_sublane_group,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_group import (  # noqa: E501
    register_herdr_group,
)


def _add_doctor_diagnostic_options(parser: argparse.ArgumentParser) -> None:
    """Shared --target/--repo/--home/--json for `doctor` and `doctor instruction`."""
    parser.add_argument(
        "--target",
        dest="repo",
        help="Project root to check for scaffold and Claude project-skill readiness. "
        "Defaults to MOZYO_REPO or the current working directory.",
    )
    parser.add_argument(
        "--repo",
        dest="repo",
        help="Alias for --target.",
    )
    parser.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )


def _add_lifecycle_json(parser: argparse.ArgumentParser) -> None:
    """Shared --json for the lifecycle subcommands, injected into the feature groups."""
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )


def register_top(sub) -> None:
    """Register the `status` and `list` core commands onto ``sub``."""
    status = sub.add_parser("status")
    add_repo_option(status)
    status.add_argument(
        "--session",
        default=None,
        help=(
            "Tmux session to describe. Defaults to the current session when "
            "run inside tmux, else the bare-`mozyo` derived session name "
            "(`mozyo-bridge session name`)."
        ),
    )
    status.set_defaults(func=cmd_status)

    sub.add_parser("list").set_defaults(func=cmd_list)


def register_pane_io(sub) -> None:
    """Register the `id` / `resolve` / `read` / `type` pane I/O commands onto ``sub``."""
    sub.add_parser("id").set_defaults(func=cmd_id)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("target")
    resolve.set_defaults(func=cmd_resolve)

    read = sub.add_parser("read")
    read.add_argument("target")
    read.add_argument("lines", type=int, nargs="?", default=50)
    read.set_defaults(func=cmd_read)

    type_cmd = sub.add_parser("type")
    type_cmd.add_argument("target")
    type_cmd.add_argument("text")
    type_cmd.set_defaults(func=cmd_type)


def register_keys(sub) -> None:
    """Register the `keys` core command onto ``sub``."""
    keys = sub.add_parser("keys")
    keys.add_argument("target")
    keys.add_argument("keys", nargs="+")
    keys.set_defaults(func=cmd_keys)


def register_lifecycle(sub, *, snapshot=None) -> None:
    """Register `init` / `doctor`, then compose the `sublane` / `herdr` groups onto ``sub``.

    ``init`` and ``doctor`` (+ ``doctor instruction`` / ``doctor runtime``) are core
    commands and stay here. The ``sublane`` and ``herdr`` groups belong to the
    delegated-coordinator and terminal-runtime-provider features, so their parsers live
    with those features and are composed by one call each (Redmine #14654).

    ``snapshot`` (Redmine #13569 R1-F1) supplies the ``init`` / ``herdr session-start``
    ``--agent`` choice vocabulary from the composition root's single injected snapshot;
    ``None`` uses the built-in provider ids (byte-identical). It is also carried into
    the ``sublane`` group, which pins it onto the ``sublane create`` namespace.
    """
    agent_choices = (
        sorted(snapshot.provider_ids) if snapshot is not None else sorted(agent_provider_ids())
    )
    init = sub.add_parser(
        "init",
        help=(
            "Adopt the current/target pane into its workspace as a `claude` / "
            "`codex` agent. Smart default: derive the workspace's expected tmux "
            "session, pin it into `.vscode/settings.json`, rename a "
            "tmux-integrated fallback session (e.g. `___________`) into the "
            "derived name, then rename the window to the agent. Fails closed when "
            "adoption is not provably safe (meaningful foreign session, "
            "expected-session collision, unidentifiable workspace root). Defaults "
            "to the current pane when no target is given."
        ),
    )
    init.add_argument("agent", choices=agent_choices)
    init.add_argument("target", nargs="?")
    init.add_argument(
        "--window-only",
        action="store_true",
        default=False,
        dest="window_only",
        help=(
            "Legacy low-level behavior: only rename the current/target window, "
            "with no session rename and no `.vscode/settings.json` write. Use for "
            "manual / debug workflows or to adopt into a meaningful (non-fallback) "
            "session in place."
        ),
    )
    init.add_argument(
        "--no-vscode-settings",
        action="store_true",
        default=False,
        dest="no_vscode_settings",
        help=(
            "Run the smart session/window adoption but do not write "
            "`<workspace>/.vscode/settings.json`."
        ),
    )
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser(
        "doctor",
        help="Diagnose CLI, central rules, agent skills, and scaffold readiness",
    )
    _add_doctor_diagnostic_options(doctor)
    doctor.set_defaults(func=cmd_doctor)

    # `doctor instruction` is the read-only recovery runbook (Redmine #11051):
    # given the doctor diagnostics, it prints the ordered fix procedure with
    # primary vs legacy-fallback commands. Bare `doctor` keeps running the
    # diagnostics (subparser is optional so set_defaults(func=cmd_doctor) wins).
    doctor_sub = doctor.add_subparsers(dest="doctor_command", required=False)
    doctor_instruction = doctor_sub.add_parser(
        "instruction",
        help=(
            "Read-only recovery runbook: orders the fix steps for the current "
            "doctor diagnostics, distinguishing primary (Claude plugin) from "
            "legacy fallback paths and routing scaffold drift through "
            "review-before-restore. Does not write, install, or hit the network."
        ),
    )
    _add_doctor_diagnostic_options(doctor_instruction)
    doctor_instruction.add_argument(
        "--profile",
        choices=list(KNOWN_PROFILES),
        default=PROFILE_REDMINE_CODEX,
        help="Runtime-config profile to fold into the runbook. Only "
        "`redmine-codex` is defined today.",
    )
    doctor_instruction.set_defaults(func=cmd_doctor_instruction)

    # `doctor runtime` is the runtime fingerprint (Redmine #12612): it proves
    # which executable surface is under test (source tree vs installed pipx /
    # site-packages) and fails when the active runtime and the repo-local source
    # report the same version but differ on gate-critical feature probes
    # (#12597 standard_target_admission / --no-target-activation). Read-only.
    doctor_runtime = doctor_sub.add_parser(
        "runtime",
        help=(
            "Read-only runtime fingerprint: classify the active executable "
            "surface (source vs installed), report version / executable / "
            "package path / git anchor, and probe gate-critical behavior so a "
            "stale install cannot pass a dogfood/smoke gate while reporting the "
            "same version as source. Does not install or hit the network."
        ),
    )
    _add_doctor_diagnostic_options(doctor_runtime)
    doctor_runtime.set_defaults(func=cmd_doctor_runtime)

    register_sublane_group(
        sub,
        add_repo_option=add_repo_option,
        add_lifecycle_json=_add_lifecycle_json,
        snapshot=snapshot,
    )
    register_herdr_group(
        sub,
        add_repo_option=add_repo_option,
        add_lifecycle_json=_add_lifecycle_json,
        agent_choices=agent_choices,
    )
