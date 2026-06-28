"""agents command family — OOP-first boundary, attention-projection tranche (Redmine #12749 / #12638).

Second OOP-first conversion tranche for the ``application/commands.py``
decomposition (after the tmux-config family). It carries the ``agents
attention-project`` command into the policy's object boundaries
(``vibes/docs/logics/object-oriented-architecture-policy.md``), behavior-preserving:

- **Port/Adapter**: the tmux pane-option *write* side effect goes through the
  injected :class:`~mozyo_bridge.application.tmux_option_port.TmuxOptionWriterPort`
  instead of the old naked ``run_tmux(*argv, check=False)`` loop.
- **Use case**: :class:`ProjectAttentionUseCase` owns the
  derive-attention → build-plan → best-effort-apply state transition over the
  discovered candidates, returning typed entries; it has no presentation or
  ``argparse`` dependency and is unit-tested with a fake writer port.
- **Value object**: :class:`AttentionProjectionEntry` (frozen) replaces the ad-hoc
  ``(candidate, record, plan, applied_ok)`` tuple the procedural handler threaded
  between its apply loop and its two render branches.
- **Thin command handler**: :func:`cmd_agents_attention_project` resolves the CLI
  flags, runs discovery, drives the use case with a live writer, and renders text
  / JSON. It holds no external boundary directly beyond the tmux availability
  guard.

Compatibility: ``commands.py`` re-exports :func:`cmd_agents_attention_project`
and :func:`_attention_for_candidate` so
``mozyo_bridge.application.commands.cmd_agents_attention_project`` (cli_agents /
cli parser registrar) and the ``commands._attention_for_candidate`` import that
``agents targets`` and a discovery test rely on keep their identity. The shared
``_agents_target_candidates`` discovery pipeline and the ``agents list`` /
``agents targets`` read handlers remain in ``commands.py`` for now; converting
their discovery-read boundary to a port (and migrating their monkeypatch tests to
fake ports) is the larger residual carried to #12638 / #12785.
"""

from __future__ import annotations

import argparse
import json as _json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from mozyo_bridge.application.attention_projection import build_attention_option_plan
from mozyo_bridge.application.tmux_option_port import (
    LiveTmuxOptionWriter,
    TmuxOptionWriterPort,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    require_tmux,
)


# Reason code for the conservative pre-wiring attention projection (#11952):
def _attention_for_candidate(candidate, observed_at: str):
    """Derive a conservative :class:`AttentionRecord` for one target (#11952).

    First read-only exposure of the #11951 attention read model. No durable
    attention source is wired yet, so this never fabricates an
    owner/review/blocked/stalled signal: it only distinguishes a cleanly
    identified target (``healthy``, reason ``no_attention_source``) from one
    whose identity itself is ambiguous / unreadable (``unknown``). Later
    extraction tasks feed real durable / observed signals into the same pure
    :func:`derive_attention`; this stays an additive projection and is never used
    for routing / target selection. Delegates to the shared
    :func:`~mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention.conservative_attention` so this and the
    cockpit ``/api/units`` join (#12007) cannot drift.
    """
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
        CONFIDENCE_NONE,
        ROLE_SOURCE_UNKNOWN,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import (
        ROLE_CLAUDE,
        ROLE_CODEX,
        conservative_attention,
    )

    identity_readable = (
        candidate.role in (ROLE_CLAUDE, ROLE_CODEX)
        and candidate.confidence != CONFIDENCE_NONE
        and candidate.role_source != ROLE_SOURCE_UNKNOWN
    )
    return conservative_attention(
        observed_at=observed_at,
        role=candidate.role,
        identity_readable=identity_readable,
        contradictory=bool(candidate.ambiguous),
        host=candidate.host or "local",
        workspace_id=candidate.workspace_id or "",
        lane_id=candidate.lane_id or "default",
        pane_id=candidate.pane_id,
    )


@dataclass(frozen=True)
class AttentionProjectionEntry:
    """Per-target outcome of the attention projection.

    ``applied_ok`` is ``None`` in preview (no write attempted), and ``True`` /
    ``False`` once a write was attempted (``False`` if any of the pane's
    ``set-option`` writes failed — best-effort, never raised).
    """

    pane_id: Optional[str]
    attention: object
    plan: tuple[tuple[str, ...], ...]
    applied_ok: Optional[bool]


class ProjectAttentionUseCase:
    """Derive attention for each candidate and best-effort project it via a port.

    The tmux pane-option write side effect is delegated to the injected
    :class:`TmuxOptionWriterPort`, so the apply decision (preview vs. write, and
    the best-effort failure posture) is unit-testable with a fake writer.
    """

    def __init__(self, writer: TmuxOptionWriterPort) -> None:
        self._writer = writer

    def execute(
        self, candidates, observed_at: str, *, apply: bool
    ) -> list[AttentionProjectionEntry]:
        entries: list[AttentionProjectionEntry] = []
        for candidate in candidates:
            record = _attention_for_candidate(candidate, observed_at)
            plan = build_attention_option_plan(candidate.pane_id, record)
            applied_ok: Optional[bool] = None
            if apply:
                # Apply happens once here, before any caller render branch, so
                # `--json --apply` and text `--apply` perform identical writes and
                # both report the true outcome (Redmine #11954 review #58539).
                applied_ok = True
                for argv in plan:
                    if not self._writer.set_option(argv):
                        # Best-effort: a failed option write is recorded, not
                        # raised (projection-cache posture); the run still finishes.
                        applied_ok = False
            entries.append(
                AttentionProjectionEntry(
                    pane_id=candidate.pane_id,
                    attention=record,
                    plan=tuple(tuple(argv) for argv in plan),
                    applied_ok=applied_ok,
                )
            )
        return entries


def _discover_candidates(args: argparse.Namespace) -> list:
    # The shared discovery pipeline still lives in ``commands`` (residual; see
    # module docstring). Resolve it at call time so a test that patches
    # ``commands._agents_target_candidates`` (or the discovery deps it reads on
    # ``commands``) still intercepts the call.
    from mozyo_bridge.application.commands import _agents_target_candidates

    return _agents_target_candidates(args)


def cmd_agents_attention_project(args: argparse.Namespace) -> int:
    """Project derived attention onto tmux pane user options (Redmine #11954).

    Writes a re-derivable **projection cache** of the #11951 ``AttentionRecord``
    (derived conservatively, #11952) onto each discovered target's tmux pane user
    options: ``@mozyo_attention_state`` / ``@mozyo_attention_severity`` /
    ``@mozyo_attention_reason`` / ``@mozyo_attention_updated_at``.

    Boundaries:

    - **Projection cache only.** The source of truth stays the durable state /
      the ``derive_attention`` read model; these user options are a cache that
      can be deleted and re-derived. They are never consulted for routing /
      handoff preflight / target resolution.
    - **Safe by default.** Default is a preview (no tmux mutation) that prints
      the exact ``set-option`` plan per pane; ``--apply`` performs the writes
      best-effort (a failed option write never aborts the run, like other
      best-effort projections). ``--dry-run`` forces preview and wins over
      ``--apply``.
    - **No color / ``agent-ui.conf`` / iTerm changes** here — this task only
      writes the machine-readable user options; rendering them is a later task.
    """
    require_tmux()
    candidates = _discover_candidates(args)

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Safe default: preview unless --apply is given; --dry-run always wins.
    apply = bool(getattr(args, "apply", False)) and not bool(
        getattr(args, "dry_run", False)
    )

    entries = ProjectAttentionUseCase(LiveTmuxOptionWriter()).execute(
        candidates, observed_at, apply=apply
    )

    if getattr(args, "as_json", False):
        payload = [
            {
                "pane_id": entry.pane_id,
                "attention": entry.attention.as_payload(),
                "applied": apply,
                "applied_ok": entry.applied_ok,
                "plan": [list(argv) for argv in entry.plan],
            }
            for entry in entries
        ]
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not entries:
        print("no agent targets discovered; nothing to project")
        return 0

    for entry in entries:
        record = entry.attention
        label = (
            f"{entry.pane_id or '-'} {record.attention_state}/{record.severity} "
            f"({record.reason_code})"
        )
        if not apply:
            print(f"(dry-run) {label}")
            for argv in entry.plan:
                print("  tmux " + " ".join(argv))
            continue
        print(
            f"projected {label}"
            if entry.applied_ok
            else f"warning: partial projection {label}"
        )
    return 0
