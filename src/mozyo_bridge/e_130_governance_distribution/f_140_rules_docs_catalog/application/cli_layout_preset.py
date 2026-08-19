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
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LANE_PLACEMENT_LANE_CLASSES,
    LANE_PLACEMENT_RATIO_MAX,
    LANE_PLACEMENT_RATIO_MIN,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.layout_preset import (  # noqa: E501
    HERDR_API_GAPS,
    LAYOUT_PRESETS,
    LIVE_EFFECT_MATRIX,
    classify_effective_preset,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
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
    """Handle ``layout preset apply`` — argument reading, rendering, exit-code mapping.

    The whole load → expand → re-validate → preview / atomic-write flow is owned by
    :class:`~.layout_preset_apply.LayoutPresetApplyService` behind the config-document
    port (j#108183 finding_oopboundary); this handler only builds the typed input,
    projects the typed result to human / JSON output, and maps ``result.ok`` to the
    exit code. Every success rendering — including the already-matching no-op — carries
    the typed live-effect boundary (j#108183 finding_liveeffect).
    """
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.layout_preset_apply import (  # noqa: E501
        LayoutPresetApplyInput,
        LayoutPresetApplyService,
        YamlConfigDocumentAdapter,
    )

    as_json = bool(getattr(args, "json", False))
    path = repo_local_config_path(getattr(args, "repo", None))
    service = LayoutPresetApplyService(YamlConfigDocumentAdapter(path))
    result = service.apply(
        LayoutPresetApplyInput(
            preset_name=getattr(args, "preset_name"),
            ratio=getattr(args, "ratio", None),
            write=bool(getattr(args, "write", False)),
        )
    )

    if not result.ok:
        if as_json:
            print(json.dumps({"ok": False, "status": result.status, "error": result.error}))
        else:
            print(f"layout preset apply: {result.error}", file=sys.stderr)
        return 1

    if as_json:
        payload = {
            "ok": True,
            "status": result.status,
            "path": str(result.path),
            "preset": result.preset,
            "split": result.split,
            "ratio": result.ratio,
            "already_matching": result.status == "already_matching",
            "changes": list(result.changes),
            "shadowed_by_lane_kind": list(result.shadowed_by_lane_kind),
            "written": result.status == "written",
            **_live_effect_payload(),
        }
        if result.status == "previewed":
            payload["document"] = result.document
        if result.status == "written":
            payload["backup"] = str(result.backup) if result.backup else None
        print(json.dumps(payload))
        return 0

    if result.status == "already_matching":
        print(
            f"layout preset apply: {result.path} already declares preset "
            f"'{result.preset}'; nothing to do."
        )
        _print_shadow_warning(result.shadowed_by_lane_kind)
        _print_live_effect()
        return 0

    if result.status == "previewed":
        document = result.document or ""
        print(f"layout preset apply (dry-run): {result.path}")
        for change in result.changes:
            print(f"  - {change}")
        _print_shadow_warning(result.shadowed_by_lane_kind)
        _print_live_effect()
        print("\n--- would write ---")
        print(document, end="" if document.endswith("\n") else "\n")
        print("--- end (pass --write to apply) ---")
        return 0

    print(f"layout preset apply: wrote {result.path} (preset '{result.preset}').")
    if result.backup:
        print(f"  backup: {result.backup}")
    for change in result.changes:
        print(f"  - {change}")
    _print_shadow_warning(result.shadowed_by_lane_kind)
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
