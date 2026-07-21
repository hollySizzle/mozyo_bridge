"""`herdr composer-render` — the redacted composer-style measurement diagnostic.

The public, read-only diagnostic half of the #14065 measurement instrument
(Design Answer j#82160, phase 1). It authority-resolves the herdr backend + the
target handle and prints ONLY the redacted :class:`PaneRenderObservation` (closed
enums / bool) — never pane body, raw ANSI, hash, length, or excerpt. It exists so
an operator / coordinator can exercise the ``agent read --format ansi`` style
capability against a live pane *through the semantic facade*, rather than running
raw herdr, and record only the enum/bool observation durably.

Phase 1 is measurement only: this command reports the measured style provenance
(``dim`` / ``normal`` / ``mixed`` / ``unknown``); it makes **no** empty / pending
decision and touches nothing. Read-only, no ``--execute``, no send, no Enter.
"""

from __future__ import annotations

import argparse
import json
import os

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    ComposerRenderView,
    read_composer_render,
)
from mozyo_bridge.shared.errors import die


def _render_text(view: ComposerRenderView) -> str:
    if not view.backend_selected:
        return (
            "herdr composer-render: herdr backend not selected for this repo — "
            "nothing observed (this diagnostic is herdr-only)"
        )
    obs = view.observation
    if obs is None:  # defensive: a selected backend always carries an observation
        return f"herdr composer-render: target={view.target} observation=none"
    return (
        f"herdr composer-render: target={view.target} readable={obs.readable} "
        f"style_provenance={obs.style_provenance} cursor_relation={obs.cursor_relation} "
        f"prompt_present={obs.prompt_present} reason={obs.reason}"
    )


def cmd_herdr_composer_render(args: argparse.Namespace) -> int:
    """CLI entry: read one pane's composer style and print the redacted observation."""
    from mozyo_bridge.application.commands_common import repo_root_from_args

    repo_root = repo_root_from_args(args)
    target = (getattr(args, "target", "") or "").strip()
    if not target:
        die(
            "herdr composer-render failed: a target handle (herdr assigned name or "
            "window:pane locator) is required."
        )
        raise AssertionError("unreachable")
    view = read_composer_render(repo_root, target, env=os.environ)
    if getattr(args, "json", False):
        print(json.dumps(view.to_record(), ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(view))
    # A diagnostic is a report, not a success claim. It exits 0 whenever it produced
    # a view; it never gates a decision on this exit code (there is no decision in
    # phase 1). A non-herdr backend is a valid, reportable "nothing observed" state.
    return 0


def register_herdr_composer_render_parser(sub) -> None:
    """Bind `herdr composer-render` onto the `herdr` subparser group (#14065)."""
    parser = sub.add_parser(
        "composer-render",
        help=(
            "read-only: measure a pane composer's rendered style (redacted; #14065 "
            "phase-1 measurement, no decision)"
        ),
        description=(
            "Observe one pane's composer *style provenance* through the herdr "
            "`agent read --format ansi` capability and print ONLY a redacted, typed "
            "observation (readable / style_provenance / cursor_relation / reason) — "
            "never pane body, raw ANSI, hash, or length. Herdr-backend-only and "
            "read-only: it measures whether a ghost idle-placeholder (rendered dim) "
            "is distinguishable from real unsent input (rendered normal), and makes "
            "no empty/pending decision. An unsupported ANSI capability, an absent "
            "stream, a composer-less/empty pane, or any transport failure is reported "
            "as a fail-closed reason, never a positive signal."
        ),
    )
    parser.add_argument(
        "target",
        help="the herdr assigned name (mzb1_...) or window:pane locator to observe",
    )
    parser.add_argument("--repo", help="target repo root (default: cwd)")
    parser.add_argument(
        "--json", action="store_true", help="emit the redacted observation as JSON"
    )
    parser.set_defaults(func=cmd_herdr_composer_render)


__all__ = (
    "cmd_herdr_composer_render",
    "register_herdr_composer_render_parser",
)
