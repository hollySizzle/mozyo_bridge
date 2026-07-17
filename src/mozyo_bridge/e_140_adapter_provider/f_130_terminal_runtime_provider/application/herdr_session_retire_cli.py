"""``herdr session-retire`` — the public parser + command boundary (Redmine #13892).

The inverse of ``herdr session-start``: it retires the exact scratch pair that command
mints. Read-only by default; ``--execute`` is the explicit destructive intent.

The parser is deliberately narrow — there is no ``--force``, no label / focus selection,
and no pane / locator argument. A caller names the pair by the durable identity
``session-start`` itself used (``--repo`` → workspace segment, ``--lane`` → lane), and the
surface re-derives the assigned names from it. A locator can never be passed in, so an
operator cannot aim this at a pane the identity does not name.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    format_session_retire_text,
    run_session_retire,
)


def cmd_herdr_session_retire(args: argparse.Namespace) -> int:
    """Run the guarded scratch-pair retire; exit 0 only on a proven retire."""
    from mozyo_bridge.application.commands_common import repo_root_from_args

    repo_root = repo_root_from_args(args)
    result = run_session_retire(args, Path(repo_root))
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
    else:
        print(format_session_retire_text(result))
    return 0 if result.ok else 1


def register_herdr_session_retire_parser(herdr_sub, *, add_repo_option=None) -> None:
    """Register ``herdr session-retire`` on the ``herdr`` subparser group."""
    parser = herdr_sub.add_parser(
        "session-retire",
        help=(
            "Retire the exact session-start scratch pair for a lane (read-only "
            "preflight unless --execute)."
        ),
        description=(
            "Close the exact Claude/Codex pair `herdr session-start` minted for a lane, "
            "identified by its durable workspace / lane / role / assigned-name identity. "
            "This is the retirement rail for a pair that carries NO lane lifecycle "
            "record; a lane that HAS one is refused here and belongs to `sublane retire`. "
            "Fails closed: an unreadable inventory, a duplicate or foreign occupant, an "
            "unlocatable slot, a busy agent or a pending composer all close nothing. "
            "Never removes a worktree or branch, never launches or resumes a process, and "
            "never fabricates a lifecycle record to pass itself."
        ),
    )
    parser.add_argument(
        "--lane",
        required=True,
        help="The lane label whose scratch pair to retire (the `session-start --lane`).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Actually close the pair's slots. Without this the command is a read-only "
            "preflight that reports the verdict and closes nothing."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the verdict as JSON.",
    )
    if add_repo_option is not None:
        add_repo_option(parser)
    parser.set_defaults(func=cmd_herdr_session_retire)


__all__ = ("cmd_herdr_session_retire", "register_herdr_session_retire_parser")
