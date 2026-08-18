"""Public ``layout preset`` CLI: declarative pair-geometry preset switching (Redmine #15708).

The config-editing entry point behind the owner ask "上下 / 左右 を簡単に変えられる
プリセット": three subcommands under the existing ``layout`` group —

- ``layout preset list`` — the closed preset vocabulary (:data:`~...domain.layout_preset.LAYOUT_PRESETS`).
- ``layout preset status`` — read-only: the declared / effective ``lane_placement``
  geometry per lane class (+ any ``by_lane_kind`` declarations), which preset it matches
  (else ``custom``), and the typed live-effect matrix.
- ``layout preset apply <preset>`` — rewrite ONLY the ``lane_placement`` block of
  ``.mozyo-bridge/config.yaml`` to the preset's declaration. Dry-run (``--check``) by
  default, ``--write`` applies with the same atomic-replace + ``.bak`` + re-validate
  discipline as ``config migrate`` (the sibling this surface deliberately mirrors).

One ``apply --write`` is the whole acceptance-1 flow (#15708): the declaration is
persisted, and every FRESH launch / heal applies it automatically through the existing
``LanePlacementConfig.resolve_effective`` chain — no second application step exists.
What it can NEVER do is relayout live panes: the typed ``live_effect`` block in every
output states that boundary (dedicated pairs take ``herdr pair-placement`` explicitly;
shared-tab column re-split has no Herdr API — ``herdr_api_gaps``), instead of this
command simulating a mutation the runtime cannot perform (acceptance 3).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.application.repo_local_config_loader import repo_local_config_path
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (  # noqa: E501
    _atomic_write,
    _dump_v2,
    _load_raw_record,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_LANE_CLASSES,
    LANE_PLACEMENT_RATIO_MAX,
    LANE_PLACEMENT_RATIO_MIN,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.layout_preset import (  # noqa: E501
    HERDR_API_GAPS,
    LAYOUT_PRESETS,
    LIVE_EFFECT_MATRIX,
    LayoutPresetError,
    apply_preset_to_lane_placement,
    classify_effective_preset,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
    RepoLocalConfigError,
)

#: Human wording for the preset vocabulary, keyed by preset name. Display-only.
_PRESET_NOTES: "dict[str, str]" = {
    "stacked": "vertical pair (上下; split: down — the product default since #14568)",
    "side-by-side": "horizontal pair (左右; split: right)",
}


def register_preset(layout_sub) -> None:
    """Register the ``preset`` subgroup onto the ``layout`` subparsers object."""
    preset = layout_sub.add_parser(
        "preset",
        help=(
            "Declarative pair-geometry presets (Redmine #15708): switch the "
            "`lane_placement` declaration (上下 stacked / 左右 side-by-side, optional "
            "ratio) that fresh launches / heals apply. Never moves a live pane."
        ),
    )
    preset_sub = preset.add_subparsers(dest="layout_preset_command", required=True)

    p_list = preset_sub.add_parser(
        "list", help="List the closed preset vocabulary (read-only)."
    )
    p_list.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result instead of human text.",
    )
    p_list.set_defaults(func=cmd_layout_preset_list)

    p_status = preset_sub.add_parser(
        "status",
        help=(
            "Report the declared / effective lane_placement geometry, the matching "
            "preset (else `custom`), and the typed live-effect matrix (read-only)."
        ),
    )
    add_repo_option(p_status)
    p_status.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result instead of human text.",
    )
    p_status.set_defaults(func=cmd_layout_preset_status)

    p_apply = preset_sub.add_parser(
        "apply",
        help=(
            "Declare a preset into `.mozyo-bridge/config.yaml` `lane_placement`. "
            "Dry-run (`--check`) by default; pass `--write` to apply atomically. "
            "Fresh launches / heals then apply it automatically; live panes are "
            "never moved (see the typed live_effect output)."
        ),
    )
    p_apply.add_argument(
        "preset_name",
        choices=sorted(LAYOUT_PRESETS),
        help="Layout preset to declare.",
    )
    p_apply.add_argument(
        "--ratio",
        type=float,
        default=None,
        help=(
            "First pane's share of the pair split (the top pane under `stacked`, the "
            f"left pane under `side-by-side`), in "
            f"{LANE_PLACEMENT_RATIO_MIN}..{LANE_PLACEMENT_RATIO_MAX} (herdr's own "
            "effective domain). Omitted: any declared ratio is preserved."
        ),
    )
    add_repo_option(p_apply)
    mode = p_apply.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        dest="write",
        action="store_false",
        help="Preview the change and the would-be lane_placement block; write nothing (default).",
    )
    mode.add_argument(
        "--write",
        dest="write",
        action="store_true",
        help="Apply the declaration: atomic replace of config.yaml with a .bak backup.",
    )
    p_apply.set_defaults(write=False)
    p_apply.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON result instead of human text.",
    )
    p_apply.set_defaults(func=cmd_layout_preset_apply)


def _live_effect_payload() -> "dict[str, object]":
    """The typed live-effect matrix + Herdr API gap listing (acceptance 3)."""
    return {
        "live_effect": dict(LIVE_EFFECT_MATRIX),
        "herdr_api_gaps": list(HERDR_API_GAPS),
    }


def _print_live_effect() -> None:
    print("  live effect (typed):")
    for population, token in LIVE_EFFECT_MATRIX.items():
        print(f"    {population}: {token}")
    print("    hint: existing dedicated pairs take `mozyo-bridge herdr pair-placement "
          "preview` / `apply` explicitly; shared-tab columns have no supported re-split "
          f"path (herdr_api_gaps: {', '.join(HERDR_API_GAPS)}).")


def cmd_layout_preset_list(args) -> int:
    """Handle ``layout preset list`` — the closed vocabulary, read-only."""
    as_json = bool(getattr(args, "json", False))
    entries = [
        {"preset": name, "split": split, "note": _PRESET_NOTES.get(name, "")}
        for name, split in sorted(LAYOUT_PRESETS.items())
    ]
    if as_json:
        print(json.dumps({"ok": True, "presets": entries, **_live_effect_payload()}))
    else:
        print("layout presets:")
        for entry in entries:
            print(f"  {entry['preset']}: split={entry['split']} — {entry['note']}")
        _print_live_effect()
    return 0


def _load_config(repo: "str | None") -> "tuple[Path, RepoLocalConfig]":
    from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

    path = repo_local_config_path(repo)
    return path, load_repo_local_config(repo)


def cmd_layout_preset_status(args) -> int:
    """Handle ``layout preset status`` — read-only declaration / effective projection."""
    as_json = bool(getattr(args, "json", False))
    try:
        path, config = _load_config(getattr(args, "repo", None))
    except RepoLocalConfigError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"layout preset status: cannot read config: {exc}", file=sys.stderr)
        return 1

    placement = config.lane_placement
    matched = classify_effective_preset(placement)
    classes = []
    for lane_class in sorted(LANE_PLACEMENT_LANE_CLASSES):
        declared = placement.resolve(lane_class)
        effective = placement.resolve_effective(lane_class)
        classes.append(
            {
                "lane_class": lane_class,
                "declared": {
                    "split": declared.split,
                    "order": list(declared.order) if declared.order else None,
                    "ratio": declared.ratio,
                },
                "effective": {
                    "split": effective.split,
                    "order": list(effective.order) if effective.order else None,
                    "ratio": effective.ratio,
                },
            }
        )
    kinds = [
        {
            "lane_kind": lane_kind,
            "split": split,
            "order": list(order) if order else None,
            "ratio": ratio,
        }
        for lane_kind, split, order, ratio in placement.kind_placements
    ]

    if as_json:
        print(json.dumps({
            "ok": True,
            "path": str(path),
            "matched_preset": matched,
            "lane_classes": classes,
            "by_lane_kind": kinds,
            **_live_effect_payload(),
        }))
    else:
        print(f"layout preset status: {path}")
        print(f"  matched preset: {matched}")
        for entry in classes:
            declared = entry["declared"]
            effective = entry["effective"]
            print(
                f"  {entry['lane_class']}: effective split={effective['split']} "
                f"ratio={effective['ratio']} order={effective['order']} "
                f"(declared split={declared['split']} ratio={declared['ratio']} "
                f"order={declared['order']})"
            )
        if kinds:
            print("  by_lane_kind declarations (shadow the lane-class preset wholesale):")
            for entry in kinds:
                print(
                    f"    {entry['lane_kind']}: split={entry['split']} "
                    f"ratio={entry['ratio']} order={entry['order']}"
                )
        _print_live_effect()
    return 0


def cmd_layout_preset_apply(args) -> int:
    """Handle ``layout preset apply`` — preview (default) or write the declaration."""
    as_json = bool(getattr(args, "json", False))
    write = bool(getattr(args, "write", False))
    path = repo_local_config_path(getattr(args, "repo", None))

    try:
        raw_record = _load_raw_record(path)
    except RepoLocalConfigError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"layout preset apply: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    if raw_record is None:
        raw_record = {}
    if not isinstance(raw_record, dict):
        msg = (
            f"layout preset apply: {path} is not a YAML mapping; refusing to rewrite it"
        )
        if as_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    try:
        application = apply_preset_to_lane_placement(
            raw_record.get("lane_placement"),
            preset=getattr(args, "preset_name"),
            ratio=getattr(args, "ratio", None),
        )
    except LayoutPresetError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"layout preset apply: {exc}", file=sys.stderr)
        return 1

    # The rewrite touches EXACTLY the `lane_placement` key (#15708 acceptance 2: a
    # display / placement declaration must never reach any other block).
    new_record = dict(raw_record)
    new_record["lane_placement"] = {
        key: (dict(value) if isinstance(value, dict) else value)
        for key, value in application.lane_placement_record.items()
    }

    # Re-validate the WHOLE produced config through the same fail-closed loader every
    # command uses, before anything could be written (mirrors `config migrate`).
    try:
        RepoLocalConfig.from_record(new_record)
    except RepoLocalConfigError as exc:
        msg = f"layout preset apply: refusing to write an invalid config: {exc}"
        if as_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    payload = {
        "ok": True,
        "path": str(path),
        "preset": application.preset,
        "split": application.split,
        "ratio": application.ratio,
        "already_matching": application.already_matching,
        "changes": list(application.changes),
        "shadowed_by_lane_kind": list(application.shadowed_lane_kinds),
        "written": False,
        **_live_effect_payload(),
    }

    if application.already_matching:
        if as_json:
            print(json.dumps(payload))
        else:
            print(
                f"layout preset apply: {path} already declares preset "
                f"'{application.preset}'; nothing to do."
            )
            _print_shadow_warning(application.shadowed_lane_kinds)
        return 0

    if not write:
        document = _dump_v2(new_record)
        if as_json:
            payload["document"] = document
            print(json.dumps(payload))
        else:
            print(f"layout preset apply (dry-run): {path}")
            for change in application.changes:
                print(f"  - {change}")
            _print_shadow_warning(application.shadowed_lane_kinds)
            _print_live_effect()
            print("\n--- would write ---")
            print(document, end="" if document.endswith("\n") else "\n")
            print("--- end (pass --write to apply) ---")
        return 0

    try:
        backup = _atomic_write(path, _dump_v2(new_record))
    except OSError as exc:
        msg = f"layout preset apply: could not write {path}: {exc}"
        if as_json:
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1

    payload["written"] = True
    payload["backup"] = str(backup) if backup.exists() else None
    if as_json:
        print(json.dumps(payload))
    else:
        print(f"layout preset apply: wrote {path} (preset '{application.preset}').")
        if backup.exists():
            print(f"  backup: {backup}")
        for change in application.changes:
            print(f"  - {change}")
        _print_shadow_warning(application.shadowed_lane_kinds)
        _print_live_effect()
    return 0


def _print_shadow_warning(shadowed: "tuple[str, ...]") -> None:
    if shadowed:
        print(
            "  warning: by_lane_kind declares "
            f"{', '.join(shadowed)}; a declared lane kind shadows the lane-class "
            "preset wholesale for that kind (preserved verbatim, not rewritten)."
        )


__all__ = (
    "cmd_layout_preset_apply",
    "cmd_layout_preset_list",
    "cmd_layout_preset_status",
    "register_preset",
)
