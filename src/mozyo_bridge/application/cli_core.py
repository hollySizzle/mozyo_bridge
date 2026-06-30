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
- :func:`register_lifecycle` — ``init`` / ``doctor`` (+ ``doctor instruction``) / ``sublane``
"""
from __future__ import annotations

import argparse

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
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (
    cmd_sublane_callback_recovery,
    cmd_sublane_readiness,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_CHOICES,
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


def register_lifecycle(sub) -> None:
    """Register the `init` / `doctor` / `sublane` lifecycle commands onto ``sub``."""
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
    init.add_argument("agent", choices=["claude", "codex"])
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

    # `sublane` groups the read-only sublane startup / callback-stall
    # diagnostics (Redmine #12159). Both subcommands are pure over their inputs
    # and never change handoff / queue-enter / launch behavior.
    sublane = sub.add_parser(
        "sublane",
        help=(
            "Read-only sublane startup readiness and callback-stall recovery "
            "diagnostics (Redmine #12159)"
        ),
    )
    sublane_sub = sublane.add_subparsers(dest="sublane_command", required=True)

    sublane_readiness = sublane_sub.add_parser(
        "readiness",
        help=(
            "Report whether future managed Claude panes launch in auto mode, "
            "the coordinator-callback states this lane owes, and where the "
            "stall-recovery path lives. Exits non-zero when permission mode is "
            "not reproducible auto."
        ),
    )
    sublane_readiness.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    sublane_readiness.set_defaults(func=cmd_sublane_readiness)

    sublane_callback = sublane_sub.add_parser(
        "callback-recovery",
        help=(
            "Classify a delivered-but-quiet unit of work into the four "
            "callback-stall states from durable-record facts and print the "
            "standard recovery path. Exits non-zero on a genuine stall."
        ),
    )
    sublane_callback.add_argument(
        "--dispatch-delivered",
        dest="dispatch_delivered",
        action="store_true",
        help="A durable dispatch journal (Start / implementation_request / "
        "coordinator routing) exists on the issue.",
    )
    sublane_callback.add_argument(
        "--progress",
        dest="progress",
        action="store_true",
        help="A newer durable gate / Progress Log journal appeared after the "
        "dispatch (implementation_done, review_request, ...).",
    )
    sublane_callback.add_argument(
        "--callback",
        dest="callback",
        choices=CALLBACK_CHOICES,
        default=CALLBACK_ABSENT,
        help="What the durable record shows about the cross-lane coordinator "
        "callback. Default: absent.",
    )
    sublane_callback.add_argument(
        "--stale-cli",
        dest="stale_cli",
        action="store_true",
        help="Corroborating signal that a recorded callback attempt failed on "
        "a stale installed CLI (only meaningful with `--callback delivery_failed`).",
    )
    sublane_callback.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output instead of human-readable text",
    )
    sublane_callback.set_defaults(func=cmd_sublane_callback_recovery)
