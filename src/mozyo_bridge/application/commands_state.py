"""Command handlers for the home-scoped state store family (Redmine #12305).

``mozyo-bridge state inspect`` is the read-only component-status surface (it reuses
the doctor inspector from #12273). ``state migrate`` plans (dry-run) or performs the
backup-first, idempotent, non-destructive migration of the legacy per-kind SQLite
files into the home-scoped ``state.sqlite``. ``state cleanup`` is the deliberately
separate, destructive retirement of migrated legacy files and refuses to delete
anything without an explicit ``--confirm-destroy`` gate.

The handlers are thin: the container layout, component registry, planner, and
migration live in :mod:`mozyo_bridge.state_store`; the component-status inspector
lives in :mod:`mozyo_bridge.application.doctor`. These handlers only resolve the
home, call the facade, and render text or JSON — failing closed (non-zero exit, no
bare traceback) on a :class:`~mozyo_bridge.state_store.StateStoreError`, matching the
project's ``doctor`` / ``presentation`` CLI convention.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from pathlib import Path
from typing import Optional


def _home_from_args(args: argparse.Namespace) -> Optional[Path]:
    """Resolve an explicit ``--home`` override, else ``None`` (the default home)."""
    home = getattr(args, "home", None)
    return Path(home).expanduser().resolve() if home else None


def _components_from_args(args: argparse.Namespace) -> Optional[tuple[str, ...]]:
    selected = getattr(args, "components", None)
    return tuple(selected) if selected else None


def _print_json(payload: dict) -> None:
    print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_state_inspect(args: argparse.Namespace) -> int:
    """Read-only state-store component report (reuses the #12273 doctor inspector).

    Prints each legacy component and the future single DB side-by-side with its
    status and next-action token. Creates nothing and writes nothing.
    """
    from mozyo_bridge.application.doctor import collect_state_store

    as_json = bool(getattr(args, "as_json", False))
    report = collect_state_store(home=_home_from_args(args))
    if as_json:
        _print_json(report)
        return 0
    print(f"state store (home: {report['home']}) — section: {report['status']}")
    for component in report["components"]:
        print(
            f"  {component['component']}: {component['status']} "
            f"(next: {component['next_action']})"
        )
    if report["next_action"]:
        print("next actions:")
        for line in report["next_action"]:
            print(f"  - {line}")
    return 0


def cmd_state_migrate(args: argparse.Namespace) -> int:
    """Plan (dry-run) or perform the legacy -> single-DB migration.

    Without ``--write`` this is a read-only dry-run: it prints the per-component
    plan and writes nothing. With ``--write`` it performs the backup-first,
    idempotent, non-destructive migration. ``--component`` narrows the scope.
    """
    from mozyo_bridge.state_store import StateStore, StateStoreError

    as_json = bool(getattr(args, "as_json", False))
    do_write = bool(getattr(args, "write", False))
    store = StateStore(home=_home_from_args(args))
    try:
        components = _components_from_args(args)
        plan = (
            store.migrate(components=components)
            if do_write
            else store.plan_migration(components=components)
        )
    except StateStoreError as exc:
        message = f"state migration unavailable: {exc}"
        if as_json:
            _print_json({"ok": False, "db_path": str(store.path), "error": message})
        else:
            print(message, file=sys.stderr)
        return 1

    if as_json:
        payload = plan.as_payload()
        payload["ok"] = True
        _print_json(payload)
        return 0

    prefix = "" if plan.performed else "[dry-run] "
    print(f"{prefix}state migrate (db: {plan.db_path})")
    for component in plan.components:
        rows = "" if component.source_rows is None else f", {component.source_rows} row(s)"
        print(f"  {component.component}: {component.action}{rows} — {component.reason}")
    if plan.performed:
        if plan.backup_dir:
            print(f"backup: {plan.backup_dir} ({', '.join(plan.backup_files) or 'none'})")
        print("migration written." if plan.backup_dir else "no component to migrate.")
    else:
        migratable = [c.component for c in plan.migratable]
        print(
            f"would migrate: {', '.join(migratable) or 'nothing'} "
            f"(re-run with --write to perform a backup-first migration)"
        )
    return 0


def cmd_state_cleanup(args: argparse.Namespace) -> int:
    """Plan or perform the destructive retirement of migrated legacy files.

    The destructive stage is separately gated: without ``--confirm-destroy`` this
    only prints the cleanup plan (which migrated legacy files are eligible) and
    deletes nothing, even with ``--write``. With both ``--write`` and
    ``--confirm-destroy`` it backs up and removes only the legacy files whose
    component is recorded complete in the single DB.
    """
    from mozyo_bridge.state_store import StateStore, StateStoreError

    as_json = bool(getattr(args, "as_json", False))
    confirm = bool(getattr(args, "confirm_destroy", False)) and bool(
        getattr(args, "write", False)
    )
    store = StateStore(home=_home_from_args(args))
    try:
        components = _components_from_args(args)
        plan = store.cleanup(components=components, confirm_destroy=confirm)
    except StateStoreError as exc:
        message = f"state cleanup unavailable: {exc}"
        if as_json:
            _print_json({"ok": False, "db_path": str(store.path), "error": message})
        else:
            print(message, file=sys.stderr)
        return 1

    if as_json:
        payload = plan.as_payload()
        payload["ok"] = True
        _print_json(payload)
        return 0

    prefix = "" if plan.performed else "[plan] "
    print(f"{prefix}state cleanup (db: {plan.db_path})")
    for component in plan.components:
        mark = "eligible" if component.eligible else "skip"
        print(f"  {component.component}: {mark} — {component.reason}")
    if plan.performed:
        if plan.backup_dir:
            print(f"backup: {plan.backup_dir}")
        print(f"removed legacy file(s): {', '.join(plan.removed) or 'none'}")
    else:
        eligible = [c.component for c in plan.eligible]
        print(
            f"eligible for retirement: {', '.join(eligible) or 'nothing'}. "
            f"DESTRUCTIVE: pass --write --confirm-destroy to back up and delete "
            f"the migrated legacy file(s)."
        )
    return 0


__all__ = ("cmd_state_inspect", "cmd_state_migrate", "cmd_state_cleanup")
