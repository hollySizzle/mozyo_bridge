"""`herdr session-rollback` — the public entry to a startup action's compensation.

The operator-facing half of :mod:`herdr_session_rollback`. It exists as its own command,
and not as a flag on `session-start`, because that separation IS the authority boundary
(Answer j#80991): a launch never closes anything, and a close is always something an
operator asked for, by action id, having first been shown what it would do.

Read-only by default. `--execute` is the only thing that closes a pane, and it closes only
what the named action started and still owns.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback import (  # noqa: E501
    REASON_PREFLIGHT,
    SessionRollbackVerdict,
    run_session_rollback,
)
from mozyo_bridge.shared.errors import die


def _render_text(verdict: SessionRollbackVerdict) -> str:
    lines = [
        f"herdr session-rollback: action={verdict.action_id} "
        f"state={verdict.state} reason={verdict.reason}"
    ]
    for participant in verdict.participants:
        line = (
            f"  - {participant.role}: {participant.verdict} "
            f"name={participant.assigned_name}"
        )
        if participant.locator:
            line += f" locator={participant.locator}"
        if participant.blocker_id:
            line += f" blocker={participant.blocker_id}"
        if participant.closed:
            line += " closed=yes"
        lines.append(line)
        if participant.detail:
            lines.append(f"      {participant.detail}")
        if participant.close_detail:
            lines.append(f"      close failed: {participant.close_detail}")
    if verdict.detail:
        lines.append(verdict.detail)
    return "\n".join(lines)


def cmd_herdr_session_rollback(args: argparse.Namespace) -> int:
    """CLI entry: preflight (default) or discharge one startup action's rollback debt."""
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_rollback_ops import (  # noqa: E501
        LiveStartupRollbackOps,
    )

    repo_root = repo_root_from_args(args)
    action_id = (getattr(args, "action_id", "") or "").strip()
    if not action_id:
        die(
            "herdr session-rollback failed: --action-id is required. A rollback acts only "
            "under the identity of the run that recorded what it started; `session-start` "
            "prints that id (and `--json` carries it as `action_id`)."
        )
        raise AssertionError("unreachable")
    verdict = run_session_rollback(
        action_id=action_id,
        ops=LiveStartupRollbackOps(repo_root=repo_root, env=os.environ),
        execute=bool(getattr(args, "execute", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps(verdict.as_payload(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(verdict))
    # A preflight is a report, not a success claim: it exits non-zero only when it could
    # not even produce one, so `--execute` is never gated on a preflight's exit code.
    if verdict.reason == REASON_PREFLIGHT:
        return 0
    return 0 if verdict.ok else 1


def register_herdr_session_rollback_parser(sub) -> None:
    """Bind `herdr session-rollback` onto the `herdr` subparser group."""
    parser = sub.add_parser(
        "session-rollback",
        help=(
            "converge a session-start action that did not fully come up (read-only "
            "unless --execute)"
        ),
        description=(
            "Close the panes ONE session-start action started, when that action did not "
            "report every requested role healthy. Only that action's own fresh launches "
            "are candidates, and each is re-checked at action time: a drifted locator, a "
            "duplicate name, a durable obligation, unsent composer input, a busy agent or "
            "an unreadable read all refuse the close. Adopted and foreign slots are never "
            "touched. A recognised provider startup screen is reported and never answered."
        ),
    )
    parser.add_argument(
        "--action-id",
        required=True,
        help="the startup action id `herdr session-start` reported for the failed run",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually close this action's participants (default: read-only preflight)",
    )
    parser.add_argument("--repo", help="target repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit the structured verdict")
    parser.set_defaults(func=cmd_herdr_session_rollback)


__all__ = ("cmd_herdr_session_rollback", "register_herdr_session_rollback_parser")
