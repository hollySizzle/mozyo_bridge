"""`mozyo-bridge herdr launch-generation-store` parser + handlers (Redmine #14203 F2).

The public, high-level recovery rail for the home-scoped launch-generation store, so a
corrupt ``herdr-launch-generation.sqlite`` degrades (``rebuildable_cache``) instead of
bricking every future managed launch — recovered without raw SQLite. Registered as a
feature-local parser module (``cli_core`` is near the module-health ceiling), mirroring the
#13882 ``attestation-store`` rail.

Thin by construction: parse, resolve the home + the liveness view, delegate to
:mod:`herdr_launch_generation_store_maintenance`, render. Every gate — consumer liveness,
backup-first, the healthy-store refusal — lives in the use case so it is testable without
argparse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.application.herdr_launch_generation_store_maintenance import (  # noqa: E501
    format_maintenance_text,
    run_launch_generation_store_rebuild,
    run_launch_generation_store_status,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home


def _home(args: argparse.Namespace) -> Path:
    home = getattr(args, "home", None)
    return Path(home).expanduser().resolve() if home else mozyo_bridge_home()


def _repo_root(args: argparse.Namespace) -> Path:
    repo = getattr(args, "repo", None)
    return Path(repo).expanduser() if repo else Path.cwd()


def _inventory_view(args: argparse.Namespace):
    """The live consumer read the rebuild gate depends on (fail-closed, no raise)."""
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


def cmd_herdr_launch_generation_store_status(args: argparse.Namespace) -> int:
    return _emit(args, run_launch_generation_store_status(home=_home(args)))


def cmd_herdr_launch_generation_store_rebuild(args: argparse.Namespace) -> int:
    return _emit(
        args,
        run_launch_generation_store_rebuild(
            home=_home(args),
            view=_inventory_view(args),
            write=bool(getattr(args, "write", False)),
        ),
    )


def _add_common(parser: argparse.ArgumentParser, *, add_repo_option=None) -> None:
    parser.add_argument(
        "--home",
        dest="home",
        default=None,
        help=(
            "Launch-generation store home (default: MOZYO_BRIDGE_HOME, else "
            "~/.mozyo_bridge) — the same home a managed launch reserves generations in."
        ),
    )
    if add_repo_option is not None:
        add_repo_option(parser)
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )


def register_herdr_launch_generation_store_parser(herdr_sub, *, add_repo_option=None) -> None:
    """Register `herdr launch-generation-store {status,rebuild}` (Redmine #14203 F2)."""
    parser = herdr_sub.add_parser(
        "launch-generation-store",
        help=(
            "Redmine #14203: inspect / rebuild the home-scoped launch-generation store (the "
            "collision-free per-launch generation authority the gateway recovery binds on). "
            "Terminal-bound schema v2 is a required current-process conjunct. Legacy v1 "
            "is read only by the four-store offline rollout, which backup/rebuilds it before "
            "restore; normal managed launch refuses v1 before durable reservation. "
            "Requires no raw SQLite; closes, sends to, and launches NO process; refuses "
            "while managed agents hold a generation here."
        ),
    )
    sub = parser.add_subparsers(dest="launch_generation_store_command", required=True)

    status = sub.add_parser(
        "status",
        help=(
            "Read-only: report the store's shape (absent / healthy / corrupt) and what it "
            "admits (creates nothing)."
        ),
    )
    _add_common(status, add_repo_option=add_repo_option)
    status.set_defaults(func=cmd_herdr_launch_generation_store_status)

    rebuild = sub.add_parser(
        "rebuild",
        help=(
            "Rotate a CORRUPT store into backups/ and remove it so the next managed launch "
            "re-creates it (legitimate only because this is a rebuildable_cache: the next "
            "reserve/finalize re-derives the generation, and until then reads degrade "
            "fail-closed). Legacy v1 upgrade belongs to offline-rollout, not this live rail. "
            "Refuses a healthy store (it holds live generations) and refuses "
            "while managed agents hold a generation here."
        ),
    )
    rebuild.add_argument(
        "--write",
        dest="write",
        action="store_true",
        default=False,
        help=(
            "Perform the rebuild (default is a read-only plan). The corrupt store is "
            "preserved under backups/ before removal; a backup failure aborts unchanged."
        ),
    )
    _add_common(rebuild, add_repo_option=add_repo_option)
    rebuild.set_defaults(func=cmd_herdr_launch_generation_store_rebuild)


__all__ = (
    "cmd_herdr_launch_generation_store_rebuild",
    "cmd_herdr_launch_generation_store_status",
    "register_herdr_launch_generation_store_parser",
)
