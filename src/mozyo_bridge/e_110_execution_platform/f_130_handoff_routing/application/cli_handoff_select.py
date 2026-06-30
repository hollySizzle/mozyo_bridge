"""Semantic target-selection CLI arguments for `handoff send` / `message` (Redmine #12663).

Extracted from ``cli_handoff.py`` (which is at the 1000-line module-health cap,
the same reason ``cli_handoff_q_enter.py`` / ``cli_handoff_ticketless.py`` were
split out) so the fail-closed selector flags live in one cohesive place. These
registrars only *declare* the flags; the resolution itself runs through the
shared
:func:`mozyo_bridge.application.commands_target_select.select_semantic_target`
so ``handoff send`` and ``message`` never drift.
"""

from __future__ import annotations

import argparse


def add_handoff_select_args(parser: argparse.ArgumentParser) -> None:
    """Add `handoff send --select` semantic-selection flags (Redmine #12663).

    Resolves the target pane from ``--to`` (role) + ``--target-repo`` (repo) +
    optional session / project instead of a hand-copied ``%pane``, fail-closed
    on 0 / many. Never weakens the ``--target-repo`` / ``--target-project``
    identity gates: the resolved pane is fed back through them in
    ``orchestrate_handoff``.
    """
    parser.add_argument(
        "--select",
        action="store_true",
        help=(
            "Resolve the target pane semantically from `--to` (role) + "
            "`--target-repo` (default: the sender's own repo) + optional "
            "`--target-session` / `--target-project` instead of an explicit "
            "`--target %%pane`. Sends only when exactly one candidate matches; "
            "0 / multiple / cross-workspace Claude fail closed with candidate "
            "diagnostics. Mutually exclusive with an explicit `--target`."
        ),
    )
    parser.add_argument(
        "--target-session",
        dest="target_session",
        help=(
            "Optional tmux session discriminator for `--select` (Redmine "
            "#12663); narrows the selection to candidate panes in this session. "
            "Ignored without `--select`."
        ),
    )


def add_message_select_args(parser: argparse.ArgumentParser) -> None:
    """Add `message --select-role` semantic-selection flags (Redmine #12663).

    The operator / ticketless ``message`` rail has no ``--to`` flag, so
    ``--select-role`` carries the receiver role (and doubles as the selection
    trigger). Combined with ``--target-repo`` (default: the sender's own repo)
    and the optional session / project discriminators, it reuses the same
    fail-closed selector ``handoff send --select`` uses.
    """
    parser.add_argument(
        "--select-role",
        dest="select_role",
        choices=["claude", "codex"],
        help=(
            "Resolve the target pane semantically by receiver role instead of an "
            "explicit `%%pane` (Redmine #12663). Combine with `--target-repo` "
            "(default: the sender's own repo) and optional `--target-session` / "
            "`--target-project` to pick exactly one candidate; 0 / multiple / "
            "cross-workspace Claude fail closed with candidate diagnostics. Omit "
            "the positional `target` when using `--select-role`."
        ),
    )
    parser.add_argument(
        "--target-repo",
        dest="target_repo",
        help=(
            "Checkout root for `--select-role` semantic resolution (Redmine "
            "#12663); defaults to the sender's own repo. An explicit path narrows "
            "the selection to that repo."
        ),
    )
    parser.add_argument(
        "--target-session",
        dest="target_session",
        help="Optional tmux session discriminator for `--select-role` (Redmine #12663).",
    )
    parser.add_argument(
        "--target-project",
        dest="target_project",
        help="Optional project-scope discriminator for `--select-role` (Redmine #12663).",
    )
