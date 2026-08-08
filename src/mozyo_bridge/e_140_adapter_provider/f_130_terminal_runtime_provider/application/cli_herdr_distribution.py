"""``herdr`` distribution surface: pin posture, hook installer, plugin policy.

One registration point for the public commands the distribution / supply-chain
surface ships — mirroring the ``cli_herdr_recovery`` precedent so the
near-ceiling ``cli_core`` composition root gains only a single import + call:

- ``herdr pin-posture`` (Redmine #13249) — generate the herdr supply-chain pin
  config, or verify an existing herdr config is pinned (read-only;
  ``--verify <path>``). This is the config half of that US: it never mutates
  operator state.
- ``herdr integration-install`` (Redmine #13249) — the **opt-in** Claude / Codex
  session-hook installer. Read-only plan by default (mutates nothing);
  ``--apply`` is the explicit opt-in that runs ``herdr integration install``
  bracketed by a snapshot / diff / rollback transaction. It refuses to touch home
  unless herdr's posture is pinned and every gate passes.
- ``herdr plugin-policy`` (Redmine #14619) — the managed-lane community-plugin
  policy: classify the installed plugins, or plan an enable / install. Read-only
  throughout; it has no apply mode at all, because enabling a herdr plugin is a
  *user-global* change and this surface never makes one.

The parsers are deliberately narrow: an operator names agents by their known token
(``--agent claude`` / ``--agent codex``), a home by ``--home`` (defaults to ``$HOME``),
and the herdr config that must be pinned by ``--herdr-config``. There is no ``--force``
and no way to name an arbitrary directory or executable — the config dirs are derived
from ``home`` and the known agent map, and the herdr binary comes only from the trusted
environment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_integration_install import (
    HerdrIntegrationInstallError,
    InstallReport,
    normalize_agents,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_pin_posture import (
    HerdrPinPostureError,
    PIN_MODE_OFFLINE,
    PIN_MODE_PINNED_MIRROR,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pin_posture_ops import (
    format_render_text,
    format_verify_text,
    render_posture,
    verify_config,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_integration_install_ops import (
    InstallInputs,
    format_report_text,
    report_payload,
    run_install,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_plugin_policy_ops import (
    InventoryReadError,
    RenderGuardError,
    guard_rendered_payload,
    guard_rendered_text,
    classify_inventory,
    format_enable_plan_text,
    format_install_plan_text,
    format_read_error_text,
    format_status_text,
    parse_inventory,
    plan_candidate_install,
    plan_enable,
    query_inventory,
    read_inventory_document,
)


# --- pin-posture -------------------------------------------------------------


def cmd_herdr_pin_posture(args: argparse.Namespace) -> int:
    """Render the pin config, or verify a herdr config's posture (read-only)."""
    manifest_url = getattr(args, "manifest_catalog_url", None)
    if getattr(args, "verify", None):
        result = verify_config(Path(args.verify), manifest_catalog_url=manifest_url)
        if getattr(args, "json", False):
            print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
        else:
            print(format_verify_text(result))
        return 0 if result.ok else 1
    try:
        result = render_posture(args.mode, manifest_catalog_url=manifest_url)
    except HerdrPinPostureError as exc:
        print(f"error: {exc}")
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result.as_payload(), indent=2, sort_keys=True))
    else:
        print(format_render_text(result))
    return 0


def register_herdr_pin_posture_parser(herdr_sub) -> None:
    """Register ``herdr pin-posture`` on the ``herdr`` subparser group."""
    parser = herdr_sub.add_parser(
        "pin-posture",
        help=(
            "Generate the herdr supply-chain pin config (offline or pinned-mirror), or "
            "verify an existing herdr config is pinned (--verify)."
        ),
        description=(
            "Render the herdr [update] config that pins the supply-chain posture so "
            "herdr performs no unattended version / manifest egress (PoC #13175 E3), or "
            "verify an existing herdr config file is pinned. Read-only: rendering prints "
            "the config, verify only reads. An absent update switch is herdr's default "
            "(on), so a config that omits it reads as UNPINNED."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=(PIN_MODE_OFFLINE, PIN_MODE_PINNED_MIRROR),
        default=PIN_MODE_OFFLINE,
        help="Pin mode to render (default: offline = both update checks off).",
    )
    parser.add_argument(
        "--manifest-catalog-url",
        dest="manifest_catalog_url",
        default=None,
        help=(
            "The pinned https manifest-catalog mirror URL (required for --mode "
            "pinned_mirror; also lets --verify accept a manifest_check=true config)."
        ),
    )
    parser.add_argument(
        "--verify",
        metavar="CONFIG_PATH",
        default=None,
        help="Instead of rendering, verify this herdr config file is pinned.",
    )
    parser.add_argument("--json", action="store_true", help="Emit as JSON.")
    parser.set_defaults(func=cmd_herdr_pin_posture)


# --- integration-install -----------------------------------------------------


def cmd_herdr_integration_install(args: argparse.Namespace) -> int:
    """Plan (read-only) or apply (opt-in) the Claude / Codex session-hook install."""
    try:
        agents = normalize_agents(getattr(args, "agent", None))
    except HerdrIntegrationInstallError as exc:
        # An unknown agent never reaches the ops layer: report it as a blocked plan.
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}")
        return 1
    home = Path(args.home) if getattr(args, "home", None) else Path(
        os.path.expanduser("~")
    )
    herdr_config = Path(args.herdr_config) if getattr(args, "herdr_config", None) else None
    inputs = InstallInputs(
        home=home,
        agents=agents,
        herdr_config=herdr_config,
        manifest_catalog_url=getattr(args, "manifest_catalog_url", None),
    )
    report: InstallReport = run_install(inputs, apply=bool(getattr(args, "apply", False)))
    if getattr(args, "json", False):
        print(json.dumps(report_payload(report), indent=2, sort_keys=True))
    else:
        print(format_report_text(report))
    return 0 if report.ok else 1


def register_herdr_integration_install_parser(herdr_sub) -> None:
    """Register ``herdr integration-install`` on the ``herdr`` subparser group."""
    parser = herdr_sub.add_parser(
        "integration-install",
        help=(
            "Opt-in Claude / Codex session-hook installer (read-only plan unless "
            "--apply). Refuses unless herdr's posture is pinned."
        ),
        description=(
            "Install the herdr session-resume hook into ~/.claude / ~/.codex — but only "
            "on explicit opt-in. Default is a read-only PLAN that mutates nothing; "
            "--apply runs `herdr integration install` bracketed by a snapshot / diff / "
            "rollback transaction. Fails closed on an unknown agent, a missing or unsafe "
            "config dir, an unpinned herdr posture, or a partial multi-agent failure (the "
            "whole set rolls back). The hook is herdr's artifact; this never authors hook "
            "bytes and never reads operator credentials."
        ),
    )
    parser.add_argument(
        "--agent",
        dest="agent",
        action="append",
        choices=("claude", "codex"),
        help="Agent to install the hook for (repeatable). Default: both.",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="Operator home the agent config dirs sit under (default: $HOME).",
    )
    parser.add_argument(
        "--herdr-config",
        dest="herdr_config",
        default=None,
        help=(
            "herdr config file whose pin posture gates the install. Without a pinned "
            "posture the install is refused (unpinned_remote)."
        ),
    )
    parser.add_argument(
        "--manifest-catalog-url",
        dest="manifest_catalog_url",
        default=None,
        help="Observed pinned https manifest-catalog mirror (lets a manifest_check=true "
        "herdr config gate as pinned).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Explicit opt-in: actually run the install (and roll back on any failure). "
            "Without it the command is a read-only plan and mutates nothing."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit as JSON.")
    parser.set_defaults(func=cmd_herdr_integration_install)


# --- plugin-policy -----------------------------------------------------------


def _emit(payload: dict, text: str, *, as_json: bool) -> int:
    """The single output point for this command — and its disclosure sink guard.

    Every mode renders through here, so this is the one place a check covers a
    surface nobody remembered to enumerate. Four review rounds on this issue each
    closed one surface and left its neighbour open, so the guard is deliberately
    attached to the exit rather than to any field: it asks whether the *finished*
    artifact carries an absolute path or a control character.

    A violation prints nothing and returns a non-zero code. Emitting a partial or
    scrubbed report would be worse than emitting none: this text exists to be
    pasted into a durable record.
    """
    try:
        if as_json:
            guard_rendered_payload(payload)
            rendered = json.dumps(payload, indent=2, sort_keys=True)
        else:
            rendered = guard_rendered_text(text)
    except RenderGuardError as exc:
        print(f"error [render_guard]: {exc}")
        return 1
    print(rendered)
    return 0


def cmd_herdr_plugin_policy(args: argparse.Namespace) -> int:
    """Classify the installed plugins, or plan an enable / install. Read-only.

    ``--plan-install`` is answered without reading the inventory at all: a candidate
    install is about a plugin that does not exist locally yet, so consulting the
    local inventory would only introduce a dependency on an unrelated surface.
    """
    as_json = bool(getattr(args, "json", False))
    if getattr(args, "plan_install", None):
        install_plan = plan_candidate_install(
            args.plan_install, getattr(args, "ref", None)
        )
        blocked = _emit(
            install_plan.as_payload(),
            format_install_plan_text(install_plan),
            as_json=as_json,
        )
        return blocked or (0 if install_plan.ok else 1)
    try:
        if getattr(args, "from_json", None):
            document = read_inventory_document(Path(args.from_json))
        else:
            document = query_inventory(os.environ)
        status = classify_inventory(parse_inventory(document))
    except InventoryReadError as exc:
        _emit(
            {"ok": False, "reason": exc.reason, "detail": exc.detail},
            format_read_error_text(exc),
            as_json=as_json,
        )
        return 1
    if getattr(args, "plan_enable", None):
        enable_plan = plan_enable(status, args.plan_enable)
        blocked = _emit(
            enable_plan.as_payload(),
            format_enable_plan_text(enable_plan),
            as_json=as_json,
        )
        return blocked or (0 if enable_plan.ok else 1)
    blocked = _emit(status.as_payload(), format_status_text(status), as_json=as_json)
    return blocked or (0 if status.ok else 1)


def register_herdr_plugin_policy_parser(herdr_sub) -> None:
    """Register ``herdr plugin-policy`` on the ``herdr`` subparser group."""
    parser = herdr_sub.add_parser(
        "plugin-policy",
        help=(
            "Classify installed herdr plugins against the managed-lane policy, or "
            "plan an enable / install (read-only; never changes plugin state)."
        ),
        description=(
            "Decide what a herdr community plugin may be in a managed lane. Two "
            "independent answers per plugin: whether it may be ENABLED (the "
            "lane-authority axis — a plugin that writes into agent input bypasses the "
            "exact-once handoff rail and the durable anchor), and whether its INSTALL "
            "may be run (the supply-chain axis — a [[build]] that fetches a remote "
            "artifact verified only from the same origin is an unpinned remote "
            "execution). An allow is pinned to an exact commit; a plugin observed at "
            "any unreviewed identity is unknown and denied. Reports state plainly that "
            "a herdr enable is USER-GLOBAL, never workspace-local, and carry no "
            "filesystem path. This command has no apply mode: it plans and reports, "
            "and installs / enables / disables nothing."
        ),
    )
    parser.add_argument(
        "--from-json",
        dest="from_json",
        metavar="INVENTORY_PATH",
        default=None,
        help=(
            "Classify a captured `herdr plugin list --json` document instead of "
            "querying the trusted-environment herdr binary."
        ),
    )
    parser.add_argument(
        "--plan-enable",
        dest="plan_enable",
        metavar="PLUGIN_ID",
        default=None,
        help=(
            "Answer whether this installed plugin may be enabled. Exits non-zero when "
            "it may not. Enables nothing."
        ),
    )
    parser.add_argument(
        "--plan-install",
        dest="plan_install",
        metavar="OWNER/REPO[/SUBDIR...]",
        default=None,
        help=(
            "Answer whether `herdr plugin install OWNER/REPO[/SUBDIR...] --ref "
            "COMMIT` may be run (needs --ref). Installs nothing."
        ),
    )
    parser.add_argument(
        "--ref",
        dest="ref",
        metavar="COMMIT",
        default=None,
        help=(
            "The exact full 40-hex commit a --plan-install candidate would be pinned "
            "to. Without it the candidate is unpinned and denied."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit as JSON.")
    parser.set_defaults(func=cmd_herdr_plugin_policy)


def register_herdr_distribution_surfaces(herdr_sub, *, add_repo_option=None) -> None:
    """Register the distribution / supply-chain surfaces (Redmine #13249 / #14619).

    ``add_repo_option`` is accepted for signature parity with the sibling registrars
    but unused: these commands take an explicit ``--home`` / ``--herdr-config`` rather
    than resolving a repo root, so a checkout can never implicitly widen their scope.
    """
    register_herdr_pin_posture_parser(herdr_sub)
    register_herdr_integration_install_parser(herdr_sub)
    register_herdr_plugin_policy_parser(herdr_sub)


__all__ = (
    "cmd_herdr_integration_install",
    "cmd_herdr_pin_posture",
    "cmd_herdr_plugin_policy",
    "register_herdr_distribution_surfaces",
    "register_herdr_integration_install_parser",
    "register_herdr_pin_posture_parser",
    "register_herdr_plugin_policy_parser",
)
