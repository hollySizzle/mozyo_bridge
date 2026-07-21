"""Pure same-lane worker-send composition + fail-closed containment (Redmine #13357).

Carved out of ``sublane_worker_dispatcher`` (Redmine #14192, to keep that use-case
module under the module-health line cap) as a cohesive leaf: it composes the governed
``handoff send`` argv the gateway already runs by hand, renders the replayable retry
command, and drives the composed argv under the shared j#71597 stdout-capture
containment. The tmux :class:`LiveWorkerDispatchOps` and the herdr
:class:`~...application.sublane_worker_dispatch_herdr_ops.HerdrWorkerDispatchOps` both
call these through the ``sublane_worker_dispatcher`` module namespace (which re-exports
them), so their existing monkeypatch seams keep working byte-for-byte.
"""

from __future__ import annotations

import contextlib
import io
import sys
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_worker_dispatch import (
    classify_send_known_not_sent,
)


def _drive_worker_send_argv(argv: list[str]) -> tuple[int, bool]:
    """Run the composed same-lane ``handoff send`` argv, fail-closed (shared).

    Review j#71597: the inner `handoff send` fails closed through
    `die()` == `raise SystemExit`, which `except Exception` never
    catches, and it emits its own delivery record to stdout. Both must
    be contained here so the outer WorkerDispatchOutcome stays the
    single fail-closed, machine-readable surface: run the composed
    primitive under stdout capture and convert any SystemExit
    (including an argparse usage error) to its exit code. The captured
    inner record is surfaced to stderr on failure so the blocked send
    stays diagnosable without polluting the outer `--json` stdout.

    Returns ``(rc, known_not_sent)`` (Redmine #14192). ``known_not_sent`` is
    ``True`` only when a non-zero send's captured structured outcome PROVES a
    pre-injection zero-send (``classify_send_known_not_sent`` — a
    ``gateway_route_blocked`` / ``reader_upgrade_required`` gate that ``die``s before
    the transport rail types a byte), so the caller can cancel the exact fence key
    rather than poison it to the reconcile-only ``uncertain`` terminal. Every other
    non-zero outcome — unparseable, timeout, post-injection — is ``False`` (stays
    uncertain, never-replay).

    Shared by the tmux :class:`LiveWorkerDispatchOps` and the herdr
    :class:`~...application.sublane_worker_dispatch_herdr_ops.HerdrWorkerDispatchOps`
    (#13357), so both backends measure the delivery ACK with the identical
    containment; extracting it changes no tmux behaviour.
    """
    from mozyo_bridge.application.cli import build_parser, normalize_paths

    inner_out = io.StringIO()
    try:
        with contextlib.redirect_stdout(inner_out):
            args = build_parser().parse_args(argv)
            args = normalize_paths(args)
            rc = int(args.func(args) or 0)
    except SystemExit as exc:
        # A SystemExit is always the *fail-closed* leg here (`die()` /
        # argparse usage error); the success leg returns an int. A
        # non-int / None exit code is never treated as a delivery ACK —
        # an ambiguous exit must not promote to `worker_dispatched`.
        code = exc.code
        rc = code if isinstance(code, int) and code != 0 else 1
    captured = inner_out.getvalue()
    known_not_sent = classify_send_known_not_sent(captured) if rc != 0 else False
    if rc != 0 and captured.strip():
        print(
            "worker handoff send (inner delivery record):\n" + captured.strip(),
            file=sys.stderr,
        )
    return rc, known_not_sent


def _worker_dispatch_argv(
    *,
    issue: str,
    journal: str,
    worker_pane: str,
    lane_label: str,
    gateway_callback_target: Optional[str],
    target_repo: str,
    allow_direct_worker: bool = False,
    repo_root: Optional[str] = None,
    target_lane: Optional[str] = None,
    worker_provider: str = "claude",
) -> list[str]:
    """Compose the governed worker forward; optional pins bind repo and lane."""
    argv: list[str] = []
    if repo_root:
        # Top-level flag: it MUST precede the ``handoff`` subcommand.
        argv += ["--repo", repo_root]
    argv += [
        "handoff",
        "send",
        "--to",
        worker_provider,
        "--source",
        "redmine",
        "--issue",
        issue,
        "--journal",
        journal,
        "--kind",
        "implementation_request",
        "--target",
        worker_pane,
        "--target-repo",
        target_repo,
    ]
    if target_lane:
        # Redmine #13485: explicit lane authority (mirrors the gateway dispatch's
        # `--target-lane`). Placed with the other target coordinates, before `--mode`.
        argv += ["--target-lane", target_lane]
    argv += [
        "--mode",
        "queue-enter",
        "--role-profile",
        "implementation_worker",
        "--profile-field",
        f"lane={lane_label}",
    ]
    if gateway_callback_target:
        argv += [
            "--profile-field",
            f"gateway_callback_target={gateway_callback_target}",
        ]
    if allow_direct_worker:
        argv.append("--allow-direct-worker")
    return argv


def _replayable_command(
    *,
    issue: str,
    journal: Optional[str],
    worker_pane: Optional[str],
    lane_label: str,
    gateway_callback_target: Optional[str],
    target_repo: str,
    allow_direct_worker: bool = False,
    target_lane: Optional[str] = None,
    repo_root: Optional[str] = None,
    worker_provider: str = "claude",
) -> str:
    """Render the replay command with the same optional repo/lane pins as send."""
    return "mozyo-bridge " + " ".join(
        _worker_dispatch_argv(
            issue=issue,
            journal=journal or "<journal>",
            worker_pane=worker_pane or "<worker-pane>",
            lane_label=lane_label,
            gateway_callback_target=gateway_callback_target,
            target_repo=target_repo,
            allow_direct_worker=allow_direct_worker,
            target_lane=target_lane,
            repo_root=repo_root,
            worker_provider=worker_provider,
        )
    )


__all__ = (
    "_drive_worker_send_argv",
    "_worker_dispatch_argv",
    "_replayable_command",
)
