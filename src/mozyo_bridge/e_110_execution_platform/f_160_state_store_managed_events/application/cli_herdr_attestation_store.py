"""`mozyo-bridge herdr attestation-store` parser + handlers (Redmine #13882).

The public, high-level rail for the one store a shared ``MOZYO_BRIDGE_HOME`` hands to
launchers of several vintages at once. Registered as a feature-local parser module rather
than as flags in ``cli_core`` (that module is near the module-health ceiling, the
``cli_sublane_retire`` precedent).

Thin by construction: parse, resolve the home + the liveness view, delegate to
:mod:`herdr_attestation_store_maintenance`, render. Every gate — consumer liveness,
backup-first, idempotency, the migrate-vs-rebuild boundary — lives in the use case so it
is testable without argparse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_attestation_store_maintenance import (  # noqa: E501
    format_maintenance_text,
    run_attestation_store_migrate,
    run_attestation_store_rebuild,
    run_attestation_store_status,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


def _home(args: argparse.Namespace) -> Path:
    home = getattr(args, "home", None)
    return Path(home).expanduser().resolve() if home else mozyo_bridge_home()


def _repo_root(args: argparse.Namespace) -> Path:
    repo = getattr(args, "repo", None)
    return Path(repo).expanduser() if repo else Path.cwd()


def _inventory_view(args: argparse.Namespace):
    """The live consumer read the mutating intents gate on (fail-closed, no raise)."""
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
        read_herdr_inventory,
    )

    return read_herdr_inventory(_repo_root(args), env=dict(os.environ))


def _emit(args: argparse.Namespace, result) -> int:
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        stream = sys.stdout if result.ok else sys.stderr
        print(format_maintenance_text(result), file=stream)
    return 0 if result.ok else 1


def cmd_herdr_attestation_store_status(args: argparse.Namespace) -> int:
    return _emit(args, run_attestation_store_status(home=_home(args)))


def cmd_herdr_attestation_store_migrate(args: argparse.Namespace) -> int:
    return _emit(
        args,
        run_attestation_store_migrate(
            home=_home(args),
            view=_inventory_view(args),
            write=bool(getattr(args, "write", False)),
        ),
    )


def cmd_herdr_attestation_store_rebuild(args: argparse.Namespace) -> int:
    return _emit(
        args,
        run_attestation_store_rebuild(
            home=_home(args),
            view=_inventory_view(args),
            write=bool(getattr(args, "write", False)),
        ),
    )


def cmd_herdr_attestation_store_lane_epoch_recovery_plan(args: argparse.Namespace) -> int:
    """Plan (never perform) the legacy lane-epoch recovery for one exact lane (#14756)."""
    from mozyo_bridge.core.state.lane_lifecycle_model import DecisionPointer
    from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.lane_epoch_legacy_recovery_plan import (  # noqa: E501
        format_recovery_plan,
        plan_lane_epoch_legacy_recovery,
    )

    plan = plan_lane_epoch_legacy_recovery(
        home=_home(args),
        view=_inventory_view(args),
        workspace_id=args.workspace,
        lane=args.lane,
        issue_id=args.issue,
        expected_revision=int(args.revision),
        decision=DecisionPointer(
            source="redmine", issue_id=args.issue, journal_id=str(args.journal)
        ),
        asserted_slots=tuple(args.assert_slot or ()),
    )
    if getattr(args, "json", False):
        print(json.dumps(plan.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            format_recovery_plan(plan),
            file=sys.stdout if plan.ok else sys.stderr,
        )
    return 0 if plan.ok else 1


def _add_common(parser: argparse.ArgumentParser, *, add_repo_option=None) -> None:
    parser.add_argument(
        "--home",
        dest="home",
        default=None,
        help=(
            "Attestation store home (default: MOZYO_BRIDGE_HOME, else ~/.mozyo_bridge) — "
            "the same selection a managed launch injects into the child launcher."
        ),
    )
    if add_repo_option is not None:
        add_repo_option(parser)
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )


def register_herdr_attestation_store_parser(herdr_sub, *, add_repo_option=None) -> None:
    """Register `herdr attestation-store {status,migrate,rebuild}` (Redmine #13882)."""
    parser = herdr_sub.add_parser(
        "attestation-store",
        help=(
            "Redmine #13882: inspect / migrate / rebuild the home-scoped startup "
            "self-attestation store. A shared home is written by launchers of several "
            "vintages, so an older store is left as-is and read compatibly rather than "
            "migrated by a launch; this is the public rail for changing it on purpose. "
            "Backup-first and idempotent; requires no raw SQLite; closes, sends to, and "
            "launches NO process; refuses while managed agents are live."
        ),
    )
    sub = parser.add_subparsers(dest="attestation_store_command", required=True)

    status = sub.add_parser(
        "status",
        help=(
            "Read-only: report the selected store's schema shape and what it admits "
            "(creates nothing)."
        ),
    )
    _add_common(status, add_repo_option=add_repo_option)
    status.set_defaults(func=cmd_herdr_attestation_store_status)

    migrate = sub.add_parser(
        "migrate",
        help=(
            "Additive forward migration of a recognized older store (backup-first, "
            "idempotent). Needed only to admit REPLACEMENT launches, which the older "
            "shape cannot carry; normal launches already work read-compatibly. After "
            "migrating, launchers that write only the older shape are refused visibly "
            "at the managed-launch preflight rather than silently dropped."
        ),
    )
    migrate.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=False,
        help=(
            "Perform the migration (default is a read-only plan). Backs the store up "
            "first; a backup failure aborts with the store byte-unchanged."
        ),
    )
    _add_common(migrate, add_repo_option=add_repo_option)
    migrate.set_defaults(func=cmd_herdr_attestation_store_migrate)

    rebuild = sub.add_parser(
        "rebuild",
        help=(
            "Rotate an UNREADABLE / unsupported store into backups/ and start a fresh "
            "one (legitimate only because this projection is a rebuildable cache: each "
            "slot's next launch re-derives it, and until then reads degrade to "
            "fail-closed, never to a false attestation). Refuses a recognized older "
            "store — use `migrate`, which preserves its rows."
        ),
    )
    rebuild.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=False,
        help=(
            "Perform the rebuild (default is a read-only plan). The prior store is "
            "preserved under backups/ before removal."
        ),
    )
    _add_common(rebuild, add_repo_option=add_repo_option)
    rebuild.set_defaults(func=cmd_herdr_attestation_store_rebuild)

    plan = sub.add_parser(
        "lane-epoch-recovery-plan",
        help=(
            "Redmine #14756: PLAN (never perform) the recovery of a lane hibernated by a "
            "pre-epoch build. Prints the canonical close-first sequence only when the "
            "attested-live consumer intersection is already empty; otherwise refuses with "
            "`offline_global_runtime_upgrade_required` and names the live consumers, "
            "because the migration step this recovery depends on cannot succeed while any "
            "of them holds a row here. There is no --write: closing the target pair and "
            "then attempting a fleet-wide migration would destroy that pair and still stop "
            "at the same wall (j#96866). Reads only; closes, launches and migrates nothing."
        ),
    )
    plan.add_argument("--workspace", required=True, help="Exact repo workspace id.")
    plan.add_argument("--lane", required=True, help="Exact lane id.")
    plan.add_argument("--issue", required=True, help="Exact issue the lane must own.")
    plan.add_argument(
        "--revision",
        required=True,
        type=int,
        help=(
            "Lifecycle revision observed for the lane. Reported back through the same CAS "
            "predicate the adoption step uses, so a stale pin reads as refused here too."
        ),
    )
    plan.add_argument(
        "--journal",
        required=True,
        help="Durable journal id authorising this recovery for --issue.",
    )
    plan.add_argument(
        "--assert-slot",
        action="append",
        default=[],
        metavar="ASSIGNED_NAME",
        help=(
            "ASSERT that the lane's own pair contains this assigned name (repeatable). The "
            "pair itself is derived from the lane's stored release observation, joined "
            "byte-exact against the live inventory and the startup attestation; this flag "
            "only checks that derivation against your expectation and refuses on any "
            "difference. It cannot add a slot, and therefore cannot shrink the consumer "
            "census — supplying every consumer's name refuses rather than clearing the "
            "global blocker (Redmine #14756 j#96881 F1)."
        ),
    )
    _add_common(plan, add_repo_option=add_repo_option)
    plan.set_defaults(func=cmd_herdr_attestation_store_lane_epoch_recovery_plan)


__all__ = (
    "cmd_herdr_attestation_store_migrate",
    "cmd_herdr_attestation_store_rebuild",
    "cmd_herdr_attestation_store_status",
    "register_herdr_attestation_store_parser",
)
