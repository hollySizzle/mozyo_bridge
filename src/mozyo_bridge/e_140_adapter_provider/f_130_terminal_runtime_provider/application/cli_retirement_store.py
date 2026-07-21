"""``herdr retirement-store status`` — the operator surface for the retirement authority.

Redmine #13892 (design j#80526: "component status/doctor 可視性を追加し、recovery policy を
文書化する"; review j#80523 R3-F5: the status method existed but nothing production-side ever
called it, so it was a Python API, not operator visibility).

The retirement authority fails closed on every damaged artifact shape, and this issue
deliberately ships **no** generic recover/reset (an unsafe reset would let a lost store forget
prior retirements and re-close a relaunched pair). That combination is only operable if an
operator can *see* why a retire refuses — hence this read-only rail. It mirrors the
``herdr attestation-store status`` precedent: it creates nothing, mutates nothing, and never
touches a process.
"""

from __future__ import annotations

import argparse
import json


def cmd_herdr_retirement_store_status(args: argparse.Namespace) -> int:
    """Report the retirement authority's artifact shape and attempts. Creates nothing."""
    from mozyo_bridge.core.state.scratch_retirement_fence import ScratchRetirementFence

    status = ScratchRetirementFence().status()
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0 if status.get("readable") is not False else 1

    print(f"retirement authority: {status['path']}")
    print(f"  store state: {status['store_state']}")
    if status["present_artifacts"]:
        print(f"  artifacts: {', '.join(status['present_artifacts'])}")
    readable = status.get("readable")
    print(f"  readable: {readable}")
    attempts = status.get("attempts")
    if isinstance(attempts, dict) and attempts:
        for state, count in sorted(attempts.items()):
            print(f"    {state}: {count}")
    elif attempts == 0:
        print("    (no retirement was ever recorded here)")
    if status.get("detail"):
        print(f"  detail: {status['detail']}")
    if status["store_state"] == "damaged" or readable is False:
        # No generic recover/reset ships with this issue by design (j#80526): re-creating a
        # lost authority would erase the evidence of prior retirements. Say so plainly rather
        # than leaving an operator to guess at a repair that does not exist.
        print(
            "  note: `herdr session-retire` fails closed while the authority is damaged. "
            "This is deliberate — a lost authority is never silently re-created, because "
            "that would forget prior retirements and could re-close a relaunched pair. "
            "Recovery is an operator decision recorded on the issue, not a command."
        )
    return 0 if readable is not False else 1


def register_herdr_retirement_store_parser(herdr_sub, *, add_repo_option=None) -> None:
    """Register ``herdr retirement-store status`` (Redmine #13892)."""
    parser = herdr_sub.add_parser(
        "retirement-store",
        help=(
            "Redmine #13892: inspect the home-scoped scratch-pair retirement authority — the "
            "durable proof that `herdr session-retire` actually retired a pair. Read-only."
        ),
        description=(
            "The retirement authority records, per exact scratch pair, whether a retirement "
            "is pending or proven completed. `herdr session-retire` fails closed whenever it "
            "cannot read it, and this issue ships no generic recover/reset by design, so this "
            "rail exists to show an operator WHY a retire refuses. It creates nothing, "
            "mutates nothing, and touches no process."
        ),
    )
    sub = parser.add_subparsers(dest="retirement_store_command", required=True)
    status = sub.add_parser(
        "status",
        help=(
            "Read-only: report the authority's artifact shape (absent / present / damaged) "
            "and its recorded attempts. Creates nothing."
        ),
    )
    status.add_argument("--json", action="store_true", help="Emit structured JSON output")
    if add_repo_option is not None:
        add_repo_option(status)
    status.set_defaults(func=cmd_herdr_retirement_store_status)


__all__ = (
    "cmd_herdr_retirement_store_status",
    "register_herdr_retirement_store_parser",
    "register_herdr_retirement_surfaces",
)


def register_herdr_retirement_surfaces(herdr_sub, *, add_repo_option=None) -> None:
    """Register both scratch-pair retirement surfaces (Redmine #13892).

    One entry point so the near-ceiling ``cli_core`` composes a single call: the destructive
    rail (``session-retire``) and the read-only authority view (``retirement-store status``)
    are two halves of one operator story — the second exists to explain the first's refusals.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_cli import (  # noqa: E501
        register_herdr_session_retire_parser,
    )

    register_herdr_session_retire_parser(herdr_sub, add_repo_option=add_repo_option)
    register_herdr_retirement_store_parser(herdr_sub, add_repo_option=add_repo_option)
