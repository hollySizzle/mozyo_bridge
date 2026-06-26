"""Command handlers for the desired-presentation current-table family (#12304).

``mozyo-bridge presentation seed`` migrates the static repo-local
``.mozyo-bridge/config.yaml`` presentation block into the home-scoped desired
presentation current tables (:mod:`mozyo_bridge.presentation_state`).
``mozyo-bridge presentation show`` is a read-only inspector of those tables and
the recorded seed provenance.

Both handlers are thin: the schema boundary, the idempotent / non-destructive
seed, and the read-model semantics live in
:mod:`mozyo_bridge.presentation_state` (and the config schema in
:mod:`mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config`). These handlers only resolve the
repo / home, call the store, and render text or JSON. Following the project's
fail-closed CLI convention (``doctor`` / ``observe reload``): a
:class:`mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config.RepoLocalConfigError` is printed
and the process exits non-zero, never a bare traceback.
"""

from __future__ import annotations

import argparse
import json as _json
import sys

from mozyo_bridge.application.commands_common import repo_root_from_args


def cmd_presentation_seed(args: argparse.Namespace) -> int:
    """Seed home-scoped current tables from the repo-local presentation config.

    Loads (and validates) ``<repo>/.mozyo-bridge/config.yaml`` and seeds the
    ``unit_overrides`` into ``cockpit_group_membership`` / ``projection_preferences``
    under the mozyo-bridge home. The seed is idempotent (a re-run of an unchanged
    config writes nothing) and non-destructive (it never deletes a row). With
    ``--dry-run`` the planned :class:`SeedResult` is computed and rolled back.
    """
    from mozyo_bridge.application.repo_local_config_loader import (
        load_repo_local_config,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
        REPO_LOCAL_CONFIG_VERSION,
        RepoLocalConfigError,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
        PRESENTATION_GROUPING_VERSION,
    )
    from mozyo_bridge.presentation_state import (
        PresentationStateError,
        PresentationStateStore,
    )

    repo_root = repo_root_from_args(args)
    as_json = bool(getattr(args, "as_json", False))
    dry_run = bool(getattr(args, "dry_run", False))

    def _fail(message: str, *, db_path: str = None) -> int:
        if as_json:
            payload = {"ok": False, "error": message}
            if db_path is not None:
                payload["db_path"] = db_path
            print(
                _json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            )
        else:
            print(message, file=sys.stderr)
        return 1

    try:
        config = load_repo_local_config(repo_root)
    except RepoLocalConfigError as exc:
        # Fail closed: a present-but-broken config never silently seeds nothing.
        return _fail(f"invalid repo-local config: {exc}")

    store = PresentationStateStore()
    try:
        result = store.seed_from_grouping_config(
            config.presentation.grouping,
            source_config_version=REPO_LOCAL_CONFIG_VERSION,
            grouping_version=PRESENTATION_GROUPING_VERSION,
            dry_run=dry_run,
        )
    except PresentationStateError as exc:
        # An unknown-schema / corrupt desired-state DB fails closed, never a
        # silent no-op seed.
        return _fail(
            f"presentation state unwritable: {exc}", db_path=str(store.path)
        )

    if as_json:
        payload = result.as_payload()
        payload["ok"] = True
        payload["dry_run"] = dry_run
        payload["db_path"] = str(store.path)
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    prefix = "[dry-run] " if dry_run else ""
    if result.changed == 0:
        print(
            f"{prefix}presentation seed: no changes (source config v"
            f"{result.source_config_version}, {result.skipped_overrides} override(s) "
            f"with nothing to seed)"
        )
    else:
        print(
            f"{prefix}presentation seed: "
            f"membership +{result.membership_inserted}/~{result.membership_updated} "
            f"(={result.membership_unchanged} unchanged), "
            f"projection +{result.projection_inserted}/~{result.projection_updated} "
            f"(={result.projection_unchanged} unchanged) "
            f"from source config v{result.source_config_version}"
        )
    if not dry_run:
        print(f"db: {store.path}")
    return 0


def cmd_presentation_show(args: argparse.Namespace) -> int:
    """Read-only inspector of the desired-presentation current tables.

    Prints the membership rows, projection preferences, and the recorded seed
    provenance from the home-scoped ``presentation.sqlite``. Never writes and
    never resolves routing; a ``desired_but_missing`` / ``stale`` projection is a
    later live-fold concern (:func:`mozyo_bridge.presentation_state.classify_membership`).
    """
    from mozyo_bridge.presentation_state import (
        PresentationStateError,
        PresentationStateStore,
    )

    as_json = bool(getattr(args, "as_json", False))
    store = PresentationStateStore()
    try:
        membership = store.list_group_membership()
        projections = store.list_projection_preferences()
        provenance = store.get_provenance()
    except PresentationStateError as exc:
        # An existing-but-broken desired-state DB (unknown schema / corruption)
        # must not be shown as an empty store; fail closed with an explicit
        # error and a non-zero exit (Redmine #12304 review j#62220).
        message = f"presentation state unreadable: {exc}"
        if as_json:
            print(
                _json.dumps(
                    {"ok": False, "db_path": str(store.path), "error": message},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(message, file=sys.stderr)
        return 1

    if as_json:
        payload = {
            "db_path": str(store.path),
            "membership": [row.as_payload() for row in membership],
            "projection_preferences": [row.as_payload() for row in projections],
            "provenance": provenance.as_payload() if provenance else None,
        }
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"db: {store.path}")
    if provenance is not None:
        print(
            f"last seed: source={provenance.source} "
            f"config_v{provenance.source_config_version} at {provenance.seeded_at}"
        )
    else:
        print("last seed: (none recorded)")
    print(f"cockpit_group_membership ({len(membership)} row(s)):")
    for row in membership:
        flags = []
        if row.pinned:
            flags.append("pinned")
        if row.hidden:
            flags.append("hidden")
        suffix = f" [{','.join(flags)}]" if flags else ""
        print(
            f"  {row.group_id} <- {row.unit_id} "
            f"(position={row.position}){suffix}"
        )
    print(f"projection_preferences ({len(projections)} row(s)):")
    for row in projections:
        fallback = (
            f" fallback={row.fallback_projection}" if row.fallback_projection else ""
        )
        print(f"  {row.unit_id} -> {row.preferred_projection}{fallback}")
    return 0


__all__ = ("cmd_presentation_seed", "cmd_presentation_show")
