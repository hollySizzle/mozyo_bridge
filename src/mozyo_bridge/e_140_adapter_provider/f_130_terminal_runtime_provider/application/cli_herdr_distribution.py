"""``herdr`` distribution surface: pin posture + opt-in hook installer (Redmine #13249).

One registration point for the two public commands this US ships — mirroring the
``cli_herdr_recovery`` precedent so the near-ceiling ``cli_core`` composition root gains
only a single import + call:

- ``herdr pin-posture`` — generate the herdr supply-chain pin config, or verify an
  existing herdr config is pinned (read-only; ``--verify <path>``). This is the config
  half of the US: it never mutates operator state.
- ``herdr integration-install`` — the **opt-in** Claude / Codex session-hook installer.
  Read-only plan by default (mutates nothing); ``--apply`` is the explicit opt-in that
  runs ``herdr integration install`` bracketed by a snapshot / diff / rollback
  transaction. It refuses to touch home unless herdr's posture is pinned and every gate
  passes.

The parser is deliberately narrow: an operator names agents by their known token
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


def register_herdr_distribution_surfaces(herdr_sub, *, add_repo_option=None) -> None:
    """Register the pin-posture + opt-in hook installer surfaces (Redmine #13249).

    ``add_repo_option`` is accepted for signature parity with the sibling registrars
    but unused: these commands take an explicit ``--home`` / ``--herdr-config`` rather
    than resolving a repo root, so a checkout can never implicitly widen their scope.
    """
    register_herdr_pin_posture_parser(herdr_sub)
    register_herdr_integration_install_parser(herdr_sub)


__all__ = (
    "cmd_herdr_integration_install",
    "cmd_herdr_pin_posture",
    "register_herdr_distribution_surfaces",
    "register_herdr_integration_install_parser",
    "register_herdr_pin_posture_parser",
)
