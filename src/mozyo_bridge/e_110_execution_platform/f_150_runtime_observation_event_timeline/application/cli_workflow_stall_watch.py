"""CLI surface for ``workflow stall-watch`` — the screen-diff stall watcher (#15843).

Operator / watchdog entrypoint for one bounded pass of
:func:`...stall_watch_pass.run_stall_watch_pass`: sample every named target's screen twice
around one interval, classify whatever did not advance, and print the prescriptions.

**Present-only, by construction.** The command reads panes and prints; there is no
``--apply``, no ``--enter``, no ``--reset``, no ``--relaunch``, and no code path in this
module that sends anything. That is the acceptance condition from issue #15843 ("検知 ≠
回復", "分類なしの自動回復を直結しない") expressed as an absence rather than a flag, so
granting the watcher the ability to act stays a reviewable change to this file instead of
a default someone can flip.

**Not an agent-turn tool.** `## Wait / polling 効率標準` moves bounded waiting out of LLM
turns and into background watchers; this command sleeps for its sampling interval, so it
belongs to a watchdog process or an operator, never to an agent polling its own dispatch.
The help text says so where an agent reading ``--help`` will see it.

Exit codes are the operator contract:

- ``0`` — the pass ran. Findings are output, not failure: a cockpit with a stalled lane is
  exactly what this command is for, and making it exit non-zero would put every watchdog
  wrapper into an alert loop over its normal working output.
- ``1`` — the pass observed nothing. Three ways to get there, all the same operational
  fault: no ``--target`` was given, a spec named a target that was not passed, no read
  primitive resolved, or every target's screen was unreadable. A blocked watcher looks
  identical to a quiet cockpit unless it says so, so all four are non-zero rather than a
  cheerful empty report.
- ``2`` — argparse's own usage error (an unknown flag, a non-float interval). Input
  problems argparse can see are its exit code; input problems only this command can see
  (an unknown target in a per-target spec) are ``1``, because by then the parser has
  already accepted the argv.
"""

from __future__ import annotations

import argparse
import json as _json
import os
import subprocess
import time
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
    StallObservation,
    StallWatchTarget,
    load_default_signatures,
    run_stall_watch_pass,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.pane_stall_sensor import (  # noqa: E501
    DEFAULT_CHROME_SIMILARITY,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DIFF_INCOMPARABLE,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
    RX_NO_ACTION,
)

#: Per-read bound. Generous relative to a pane read, tight relative to the sampling
#: interval, so one wedged read cannot eat the pass's whole cadence.
VISIBLE_READ_TIMEOUT_SECONDS = 10.0

EXIT_OK = 0
EXIT_UNOBSERVABLE = 1


def _parse_assignment(spec: str) -> tuple[str, str]:
    """Parse ``LOCATOR=VALUE``.

    Split on the FIRST ``=`` only: herdr locators contain ``:`` (``w1V:pY``) but not
    ``=``, while a dispatched body marker can contain anything. Splitting on the last
    ``=`` would truncate such a marker and silently weaken the composer check.
    """
    locator, sep, value = spec.partition("=")
    if not sep or not locator.strip():
        raise argparse.ArgumentTypeError(
            f"expected LOCATOR=VALUE, got {spec!r}"
        )
    return locator.strip(), value


def build_targets(args: argparse.Namespace) -> tuple[StallWatchTarget, ...]:
    """Fold the flags into targets, failing closed on a spec naming an unknown target."""
    targets = [t.strip() for t in (args.target or []) if t.strip()]
    if not targets:
        raise argparse.ArgumentTypeError("at least one --target is required")

    providers = dict(args.provider_for or [])
    markers = dict(args.pending_body_marker or [])
    exhausted = {t.strip() for t in (args.patient_window_exhausted or [])}

    known = set(targets)
    for label, keys in (
        ("--provider-for", providers.keys()),
        ("--pending-body-marker", markers.keys()),
        ("--patient-window-exhausted", exhausted),
    ):
        unknown = sorted(set(keys) - known)
        if unknown:
            raise argparse.ArgumentTypeError(
                f"{label} names target(s) that were not passed as --target: {unknown}"
            )

    return tuple(
        StallWatchTarget(
            target=target,
            provider_id=providers.get(target, args.provider or ""),
            pending_body_marker=markers.get(target, ""),
            patient_window_exhausted=target in exhausted,
        )
        for target in targets
    )


def live_screen_reader() -> Optional[Callable[[str], tuple[bool, str]]]:
    """Bind herdr's read-only visible-pane read, or ``None`` when no binary resolves.

    Adapts the launch layer's own :func:`live_visible_reader` — the same read the startup
    health probe and the send-time admission gate use — onto this pass's
    ``(readable, content)`` shape. Reusing it rather than issuing ``agent read`` here keeps
    the argv, the payload parsing and the timeout in one place, and means the watcher can
    never read a pane through a path the send boundary does not already trust.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501
        live_visible_reader,
    )

    try:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            _resolve_binary_or_die,
        )

        binary = _resolve_binary_or_die(os.environ)
    except (Exception, SystemExit):
        return None

    read = live_visible_reader(binary, subprocess.run, VISIBLE_READ_TIMEOUT_SECONDS)

    def _read(target: str) -> tuple[bool, str]:
        content = read(target)
        if not isinstance(content, str):
            return False, ""
        return True, content

    return _read


def _text_lines(observations: Sequence[StallObservation]) -> list[str]:
    lines: list[str] = []
    for obs in observations:
        detail = obs.matched_id or "-"
        lines.append(
            f"{obs.target}\t{obs.diff.state}\tsim={obs.diff.similarity:.4f}\t"
            f"{obs.stall_class}\t{obs.prescription.action}\t{detail}"
        )
    actionable = [o for o in observations if o.prescription.action != RX_NO_ACTION]
    lines.append(
        f"observed={len(observations)} actionable={len(actionable)} "
        f"posture=present_only"
    )
    return lines


def cmd_workflow_stall_watch(args: argparse.Namespace) -> int:
    """Run one bounded, read-only stall-watch pass over the named targets."""
    try:
        targets = build_targets(args)
    except argparse.ArgumentTypeError as exc:
        print(f"error: {exc}")
        return EXIT_UNOBSERVABLE

    # No injection seam here, deliberately. The pass takes its collaborators as
    # arguments, so a seam on this command would be a second substitution point that no
    # caller uses — and the acceptance tests would then exercise it instead of the live
    # path, which is the #15745 j#109007 shape (a faked port leaving the real adapter
    # unexecuted). The tests substitute the *herdr binary*, so everything below runs.
    read_screen = live_screen_reader()
    if read_screen is None:
        print(
            "error: no read-only pane reader resolved (herdr binary unavailable); "
            "a watcher that cannot read is blocked, not quiet"
        )
        return EXIT_UNOBSERVABLE

    observations = run_stall_watch_pass(
        targets,
        read_screen=read_screen,
        clock=time.monotonic,
        sleep=time.sleep,
        signatures=load_default_signatures(),
        interval_seconds=float(args.interval_seconds),
        chrome_similarity=float(args.chrome_similarity),
    )

    payload = {
        "posture": "present_only",
        "interval_seconds": float(args.interval_seconds),
        "observations": {obs.target: obs.telemetry() for obs in observations},
    }
    if getattr(args, "as_json", False):
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for line in _text_lines(observations):
            print(line)

    comparable = [o for o in observations if o.diff.state != DIFF_INCOMPARABLE]
    return EXIT_OK if comparable else EXIT_UNOBSERVABLE


def register_stall_watch(workflow_sub) -> None:
    """Register ``workflow stall-watch`` onto the ``workflow`` subparser (#15843)."""
    parser = workflow_sub.add_parser(
        "stall-watch",
        description=(
            "Run ONE bounded, read-only screen-diff pass over the named targets and "
            "report a stall classification and a prescription for each (Redmine "
            "#15843). Each target's rendered screen is sampled twice around "
            "--interval-seconds; a screen that advanced is progress, a screen where only "
            "chrome moved is a live-but-quiet unit (reasoning / tool / long test run), "
            "and a byte-identical screen is the stall candidate. Whatever did not advance "
            "is then classified against the receiver's declared startup screens (#13760 / "
            "#14741), the dispatched body if --pending-body-marker supplies it (#15842), "
            "and the provider's declared stall signatures; anything unmatched stays "
            "'unresponsive_indeterminate', which prescribes patience because a provider "
            "outage and a wedged runtime are indistinguishable from outside and only one "
            "of the two remedies is destructive. PRESENT-ONLY: this command never types, "
            "presses Enter, resets a session, or relaunches anything -- there is no flag "
            "that makes it do so. NOT for an agent turn: it sleeps for its sampling "
            "interval, so it belongs to a background watcher or an operator (see "
            "'## Wait / polling 効率標準'). Exit 0 = the pass ran (findings are output, "
            "not failure); exit 1 = nothing was observable, which is a blocked watcher."
        ),
        help=(
            "Read-only screen-diff stall pass: classify non-advancing panes and print a "
            "prescription for each. Never acts. Watcher/operator only, not an agent turn."
        ),
    )
    parser.add_argument(
        "--target",
        action="append",
        metavar="LOCATOR",
        help="A target locator to observe (repeatable). At least one is required.",
    )
    parser.add_argument(
        "--provider",
        default="",
        metavar="PROVIDER",
        help=(
            "Default provider id for every --target. Without it (and without "
            "--provider-for) a target is unprofiled: its startup screens and stall "
            "signatures cannot be classified, so it falls through to the patient "
            "indeterminate class rather than being guessed."
        ),
    )
    parser.add_argument(
        "--provider-for",
        action="append",
        type=_parse_assignment,
        metavar="LOCATOR=PROVIDER",
        help="Per-target provider id override (repeatable).",
    )
    parser.add_argument(
        "--pending-body-marker",
        action="append",
        type=_parse_assignment,
        metavar="LOCATOR=MARKER",
        help=(
            "The exact marker of the last body dispatched to this target, taken from the "
            "durable delivery record (repeatable). Supplying it enables the "
            "'unsent_composer' classification (#15842 swallowed Enter); omitting it "
            "simply leaves that class unasserted -- it is never guessed from the screen."
        ),
    )
    parser.add_argument(
        "--patient-window-exhausted",
        action="append",
        metavar="LOCATOR",
        help=(
            "Assert from the durable record that patience has already been spent on this "
            "target (repeatable). Only then may a still-frozen unit escalate to the owner "
            "window with relaunch named as a CANDIDATE for a human. Never an action."
        ),
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=DEFAULT_SAMPLE_INTERVAL_SECONDS,
        help=(
            "Seconds between the two samples (default: %(default)s, one bounded-wait "
            "watcher tick). Slept once per pass, not once per target."
        ),
    )
    parser.add_argument(
        "--chrome-similarity",
        type=float,
        default=DEFAULT_CHROME_SIMILARITY,
        help=(
            "Similarity at or above which movement counts as chrome rather than content "
            "(default: %(default)s). Below it the screen is treated as progressing."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help=(
            "Emit one structured envelope per pass as JSON (posture, interval, and one "
            "observation per target). Carries classification tokens only, never pane "
            "content, so it is safe to paste into a durable record."
        ),
    )
    parser.set_defaults(func=cmd_workflow_stall_watch)


__all__ = (
    "build_targets",
    "cmd_workflow_stall_watch",
    "live_screen_reader",
    "register_stall_watch",
)
