"""Canonical Implementation Request dispatch writer (Redmine #13758 Q2 / j#79507; review R6-F4).

The exact dispatch anchor the reconciler correlates against must be a durable **machine marker**
in the Implementation Request journal — not the surrounding prose (which the reader fail-closes on,
review R5-F3). This module is the production **producer** the marker's reader
(:func:`...domain.redmine_journal_source.resolve_dispatch_entry_journal`) was written against: it
embeds :func:`render_dispatch_note`'s marker in the IR journal body and drives the Design Answer
j#79507 Q2 **write -> readback -> handoff** sequence:

1. compose the IR note = prose body + the ``[mozyo:workflow-event:kind=implementation_request:...]``
   dispatch marker (a SEPARATE closed vocabulary — it never widens ``GATE_BEARING_KINDS``);
2. **write** the note as a Redmine journal (the live ``PUT /issues/<id>.json`` returns 204 with no
   journal id — Redmine's protocol — so the id is NOT taken from the write);
3. **read back** the issue's journals and resolve the marker's OWNING entry ``journal_id`` (the
   anchor authority is the durable entry, never the marker's self-reported field) — requiring
   EXACTLY ONE current dispatch for ``(lane, lane_generation)``;
4. only on a resolved anchor, produce the gated ``handoff send ... --kind implementation_request
   --journal <anchor>`` command anchored on that journal id.

A write failure, a readback failure, or an unresolved / ambiguous anchor all fail closed: NO
handoff is produced (Q2 point 3). The durable dispatch intent still persists in Redmine, so a later
run can readback-recover it. All I/O is injected (``post_note`` / ``read_entries``) so the producer
is deterministically test-pinned against production-shape Redmine journals; :func:`build_live_ir_dispatch`
wires the live Redmine note transport + journal source.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    render_dispatch_note,
    resolve_dispatch_entry_journal,
)

#: The awaited gate a fresh Implementation Request dispatch owes back (the worker's next durable gate).
DISPATCH_EXPECTED_GATE = "implementation_done"

#: The default role profile the gated IR handoff routes with (the same-lane worker leg).
DISPATCH_ROLE_PROFILE = "implementation_worker"

#: Terminal dispatch outcomes (fixed vocabulary; a fail-closed status never yields a handoff).
DISPATCH_WRITTEN = "dispatched"  #: marker written + anchor resolved from readback -> handoff ready
DISPATCH_WRITE_FAILED = "write_failed"  #: the journal write raised -> no anchor, no handoff
DISPATCH_READBACK_FAILED = "readback_failed"  #: the readback raised -> no anchor, no handoff
DISPATCH_ANCHOR_UNRESOLVED = "anchor_unresolved"  #: 0 / ambiguous marker on readback -> no handoff

#: A dispatch outcome carries a handoff command IFF the write + readback resolved a single anchor.
DISPATCH_SENDABLE = frozenset({DISPATCH_WRITTEN})


@dataclass(frozen=True)
class IrDispatchResult:
    """The outcome of one canonical IR dispatch (a durable value; the anchor gates the handoff)."""

    status: str
    issue: str
    lane: str
    lane_generation: str
    #: The resolved dispatch entry journal id (the exact anchor) — non-empty ONLY when dispatched.
    dispatch_journal: str = ""
    #: The gated ``handoff send`` command anchored on ``dispatch_journal`` — set ONLY when dispatched.
    handoff_command: str = ""
    #: A short, redacted reason for a fail-closed status (never a credential / URL / absolute path).
    detail: str = ""

    @property
    def sendable(self) -> bool:
        return self.status in DISPATCH_SENDABLE and bool(self.dispatch_journal)


def build_ir_handoff_command(
    *,
    issue: str,
    target: str,
    target_repo: str,
    dispatch_journal: str,
    role_profile: str = DISPATCH_ROLE_PROFILE,
    source: str = "redmine",
    to: str = "claude",
) -> str:
    """The gated ``handoff send`` command for a resolved IR dispatch, anchored on its journal id.

    Mirrors the feature's existing gated-command precedent (``grandchild_dispatch._recommended_command``
    / ``domain.handoff.explicit_standard_retry_command``): the actual pane send stays the separately
    #12918-route-gated ``handoff send`` primitive with the mandatory ``--target-repo`` identity gate;
    this only renders the runnable, anchor-bearing command. Every token is ``shlex.quote``-escaped so
    a repo path with spaces or a profile value with shell metacharacters stays a single argv token.
    """
    parts = [
        "mozyo-bridge", "handoff", "send",
        "--to", str(to),
        "--target", str(target),
        "--target-repo", str(target_repo),
        "--source", str(source),
        "--issue", str(issue),
        "--kind", "implementation_request",
        "--role-profile", str(role_profile),
        "--journal", str(dispatch_journal),
    ]
    return " ".join(shlex.quote(part) for part in parts)


def dispatch_implementation_request(
    *,
    issue: str,
    lane: str,
    lane_generation: object,
    body: str,
    post_note: Callable[[str, str], object],
    read_entries: Callable[[str], "Iterable[RedmineJournalEntry]"],
    handoff_builder: Optional[Callable[[str], str]] = None,
) -> IrDispatchResult:
    """Write the canonical IR journal with the dispatch marker, then readback-resolve its anchor (core).

    The production write -> readback -> handoff sequence (Design Answer j#79507 Q2), with the write
    and readback injected so it is test-pinned against real Redmine journal shapes:

    - ``post_note(issue, note)`` writes the marker-bearing note as a Redmine journal (the live
      transport returns 204 / no id, so the returned value is intentionally ignored — the anchor
      comes from the readback, never a self-reported id);
    - ``read_entries(issue)`` re-fetches the issue's journals for the readback;
    - :func:`resolve_dispatch_entry_journal` resolves the marker's OWNING entry journal id, requiring
      EXACTLY ONE current dispatch for ``(lane, lane_generation)``.

    A write failure -> :data:`DISPATCH_WRITE_FAILED`; a readback failure -> :data:`DISPATCH_READBACK_FAILED`;
    zero / ambiguous marker -> :data:`DISPATCH_ANCHOR_UNRESOLVED`. In every fail-closed case NO handoff
    command is produced (the durable dispatch intent still persists for a later readback-recovery). On
    a resolved anchor, ``handoff_builder(anchor)`` (default: none) yields the gated handoff command.
    """
    lane_s = str(lane or "").strip()
    gen_s = str(lane_generation if lane_generation is not None else "").strip()
    note = render_dispatch_note(body, lane=lane_s, lane_generation=gen_s)

    def _result(status: str, *, dispatch_journal: str = "", handoff_command: str = "", detail: str = ""):
        return IrDispatchResult(
            status=status, issue=str(issue), lane=lane_s, lane_generation=gen_s,
            dispatch_journal=dispatch_journal, handoff_command=handoff_command, detail=detail,
        )

    try:
        post_note(str(issue), note)
    except Exception as exc:  # noqa: BLE001 - a write failure fails closed (no anchor, no send)
        return _result(DISPATCH_WRITE_FAILED, detail=f"write:{type(exc).__name__}")

    try:
        entries = list(read_entries(str(issue)))
    except Exception as exc:  # noqa: BLE001 - a readback failure fails closed (no anchor, no send)
        return _result(DISPATCH_READBACK_FAILED, detail=f"readback:{type(exc).__name__}")

    anchor = resolve_dispatch_entry_journal(entries, lane=lane_s, lane_generation=gen_s)
    if not anchor:
        # Zero (marker not durably readable back) or ambiguous (>1 distinct entry) -> never guess.
        return _result(DISPATCH_ANCHOR_UNRESOLVED, detail="no_single_current_dispatch")

    handoff = handoff_builder(anchor) if handoff_builder is not None else ""
    return _result(DISPATCH_WRITTEN, dispatch_journal=anchor, handoff_command=handoff)


def build_live_ir_dispatch() -> "tuple[Callable[[str, str], object], Callable[[str], Iterable[RedmineJournalEntry]]]":
    """Build the live ``(post_note, read_entries)`` seams for :func:`dispatch_implementation_request`.

    - ``post_note`` is the opt-in-gated live Redmine note transport
      (:func:`redmine_delivery_transport_from_env` -> ``post_issue_note``); if the live-write opt-in
      is unset it raises, so an un-opted-in dispatch fails closed at :data:`DISPATCH_WRITE_FAILED`
      rather than silently no-op-ing;
    - ``read_entries`` is the read-only live journal source (:meth:`LiveRedmineJournalSource.from_environment`),
      the same trusted-credential reader the reconciler polls with.

    Kept as a thin composition root so the core stays pure and hermetically test-pinned.
    """
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
        redmine_delivery_transport_from_env,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalError,
        LiveRedmineJournalSource,
    )

    def _post_note(issue: str, note: str) -> object:
        transport = redmine_delivery_transport_from_env()
        if transport is None:
            raise LiveRedmineJournalError(
                "live Redmine write opt-in is unset (MOZYO_REDMINE_DELIVERY_WRITE); "
                "the IR dispatch marker cannot be persisted"
            )
        return transport.post_issue_note(str(issue), note)

    def _read_entries(issue: str) -> "Iterable[RedmineJournalEntry]":
        return LiveRedmineJournalSource.from_environment().read_entries(str(issue))

    return _post_note, _read_entries


__all__ = (
    "DISPATCH_EXPECTED_GATE",
    "DISPATCH_ROLE_PROFILE",
    "DISPATCH_WRITTEN",
    "DISPATCH_WRITE_FAILED",
    "DISPATCH_READBACK_FAILED",
    "DISPATCH_ANCHOR_UNRESOLVED",
    "DISPATCH_SENDABLE",
    "IrDispatchResult",
    "build_ir_handoff_command",
    "dispatch_implementation_request",
    "build_live_ir_dispatch",
)
