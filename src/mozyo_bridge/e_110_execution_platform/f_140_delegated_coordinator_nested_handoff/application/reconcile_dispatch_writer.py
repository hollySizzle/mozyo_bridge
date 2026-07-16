"""Canonical Implementation Request dispatch writer (Redmine #13758 Q2 / j#79507; review R6-F4/R7).

The exact dispatch anchor the reconciler correlates against must be a durable **machine marker** in
the Implementation Request journal — not the surrounding prose (which the reader fail-closes on,
review R5-F3). This module is the production **producer** the marker's reader
(:func:`...domain.redmine_journal_source.resolve_dispatch_entry_journal`) was written against: it
is the canonical author-and-dispatch path (no lower code layer mints the IR journal — the live
sublane actuator requires a pre-existing ``--journal`` anchor, review R7-F2), driving the Design
Answer j#79507 Q2 **write -> readback -> handoff** sequence, executed (never a printed string):

1. validate the required route identity (``--target`` / ``--target-repo`` / role profile fields);
   a request the handoff primitive could not accept never writes and is never ``sendable`` (R7-F4);
2. **pre-read** the issue's journals and, keyed on ``(lane, lane_generation)``, count the current
   dispatch markers (R7-F3 idempotency): exactly one -> RECOVER that owning journal id with NO new
   write; two-or-more -> fail closed (ambiguous, never add a further marker); a pre-read failure ->
   fail closed with NO write (so a retry can never create a duplicate marker);
3. only when there is no current marker, **write** the marker-bearing IR note once (the live
   ``PUT /issues/<id>.json`` returns 204 / no id — so the anchor is NOT taken from the write) and
   **read back** the owning entry journal id (post-write readback failure leaves the durable intent
   for the next run's pre-read to recover — still no duplicate);
4. on a resolved anchor, **execute** the injected handoff port and reflect its delivery outcome:
   only a delivered handoff is ``DISPATCH_WRITTEN`` / ``sendable``.

All I/O is injected (``post_note`` / ``read_entries`` / ``handoff_send``) so the producer is
deterministically test-pinned against production-shape Redmine journals; :func:`build_live_ir_dispatch`
wires the live Redmine note transport + journal source, and :func:`build_live_handoff_send` drives the
same gated ``handoff send`` primitive the sublane actuator uses (in-process CLI parse + handler).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
    RedmineJournalEntry,
    dispatch_entry_journals,
    render_dispatch_note,
)

#: The awaited gate a fresh Implementation Request dispatch owes back (the worker's next durable gate).
DISPATCH_EXPECTED_GATE = "implementation_done"

#: The default role profile the gated IR handoff routes with (the same-lane worker leg).
DISPATCH_ROLE_PROFILE = "implementation_worker"

#: Terminal dispatch outcomes (fixed vocabulary; a fail-closed status never delivers a handoff).
DISPATCH_WRITTEN = "dispatched"  #: fresh marker written + anchor resolved + handoff delivered
DISPATCH_RECOVERED = "recovered"  #: existing marker reused (idempotent, no new write) + delivered
DISPATCH_INPUT_INVALID = "input_invalid"  #: required route identity missing -> no write, no send
DISPATCH_WRITE_FAILED = "write_failed"  #: the journal write raised -> no anchor, no handoff
DISPATCH_READBACK_FAILED = "readback_failed"  #: a pre/post read raised -> no anchor, no handoff
DISPATCH_ANCHOR_UNRESOLVED = "anchor_unresolved"  #: 0 (post-write) / ambiguous marker -> no handoff
DISPATCH_HANDOFF_FAILED = "handoff_failed"  #: anchor resolved but the handoff was NOT delivered

#: A dispatch that both resolved a single anchor AND delivered its handoff (the only sendable states).
DISPATCH_SENDABLE = frozenset({DISPATCH_WRITTEN, DISPATCH_RECOVERED})


@dataclass(frozen=True)
class DispatchRoute:
    """The route identity the gated IR handoff needs — validated non-empty before any write (R7-F4)."""

    to: str
    target: str
    target_repo: str
    lane: str
    role_profile: str = DISPATCH_ROLE_PROFILE
    source: str = "redmine"

    def missing(self) -> "tuple[str, ...]":
        """The required identity fields that are empty (a non-empty result blocks the dispatch)."""
        required = {
            "to": self.to, "target": self.target, "target_repo": self.target_repo,
            "lane": self.lane, "role_profile": self.role_profile,
        }
        return tuple(k for k, v in required.items() if not str(v or "").strip())


@dataclass(frozen=True)
class HandoffOutcome:
    """The outcome of executing the IR handoff port (delivered gates ``sendable``)."""

    delivered: bool
    detail: str = ""


@dataclass(frozen=True)
class IrDispatchResult:
    """The outcome of one canonical IR dispatch (a durable value; the anchor + delivery gate sendable)."""

    status: str
    issue: str
    lane: str
    lane_generation: str
    #: The resolved dispatch entry journal id (the exact anchor) — non-empty once an anchor resolved.
    dispatch_journal: str = ""
    #: Whether the injected handoff port actually delivered (only true on a sendable status).
    handoff_delivered: bool = False
    #: A short, redacted reason for a fail-closed status (never a credential / URL / absolute path).
    detail: str = ""

    @property
    def sendable(self) -> bool:
        return self.status in DISPATCH_SENDABLE and bool(self.dispatch_journal) and self.handoff_delivered


def build_ir_handoff_argv(anchor: str, route: DispatchRoute, *, issue: str) -> "list[str]":
    """The gated ``handoff send`` argv for a resolved IR dispatch, anchored on its journal id.

    Mirrors the live sublane actuator's dispatch argv (``sublane_actuator_ops.dispatch_implementation_request``):
    the #12918-route-gated ``handoff send`` with the mandatory ``--target-repo`` identity gate, the
    anchor ``--journal``, and the role-profile fields (``lane``) the receiver contract needs (R7-F4).
    """
    return [
        "handoff", "send",
        "--to", str(route.to),
        "--target", str(route.target),
        "--target-repo", str(route.target_repo),
        "--source", str(route.source),
        "--issue", str(issue),
        "--journal", str(anchor),
        "--kind", "implementation_request",
        "--role-profile", str(route.role_profile),
        "--profile-field", f"lane={route.lane}",
    ]


def dispatch_implementation_request(
    *,
    issue: str,
    lane: str,
    lane_generation: object,
    body: str,
    route: DispatchRoute,
    post_note: Callable[[str, str], object],
    read_entries: Callable[[str], "Iterable[RedmineJournalEntry]"],
    handoff_send: Callable[[str], HandoffOutcome],
) -> IrDispatchResult:
    """Idempotently write the marker-bearing IR journal, resolve its anchor, and EXECUTE the handoff.

    The production write -> readback -> handoff sequence (Design Answer j#79507 Q2; review R7),
    injected so it is test-pinned against real Redmine journal shapes:

    - **R7-F4** — ``route.missing()`` non-empty -> :data:`DISPATCH_INPUT_INVALID`, no write, not sendable;
    - **R7-F3** — a PRE-READ counts current dispatch markers for ``(lane, lane_generation)``: exactly
      one -> recover that owning journal id with NO new write; two-or-more -> :data:`DISPATCH_ANCHOR_UNRESOLVED`
      (never add a further marker); a pre-read failure -> :data:`DISPATCH_READBACK_FAILED` with NO write
      (so a retry can never create a duplicate). Only a zero count writes the marker once, then reads
      back the owning journal id (a post-write readback failure leaves the durable intent for the next
      run's pre-read to recover);
    - **R7-F2** — on a resolved anchor, ``handoff_send(anchor)`` is EXECUTED; only a delivered handoff
      is :data:`DISPATCH_WRITTEN` / :data:`DISPATCH_RECOVERED` (sendable). A failed delivery is
      :data:`DISPATCH_HANDOFF_FAILED` (the durable marker persists for a later retry).
    """
    lane_s = str(lane or "").strip()
    gen_s = str(lane_generation if lane_generation is not None else "").strip()

    def _result(status, *, dispatch_journal="", handoff_delivered=False, detail=""):
        return IrDispatchResult(
            status=status, issue=str(issue), lane=lane_s, lane_generation=gen_s,
            dispatch_journal=dispatch_journal, handoff_delivered=handoff_delivered, detail=detail,
        )

    # R7-F4: a request the handoff primitive could not accept never writes and is never sendable.
    missing = route.missing()
    if missing:
        return _result(DISPATCH_INPUT_INVALID, detail=f"missing_route_identity:{','.join(missing)}")

    # R7-F3: pre-read idempotency — never write a duplicate marker.
    try:
        pre = list(read_entries(str(issue)))
    except Exception as exc:  # noqa: BLE001 - a pre-read failure fails closed WITHOUT writing
        return _result(DISPATCH_READBACK_FAILED, detail=f"preread:{type(exc).__name__}")

    existing = dispatch_entry_journals(pre, lane=lane_s, lane_generation=gen_s)
    if len(existing) >= 2:
        # Ambiguous / foreign duplicate already present -> never add another marker.
        return _result(DISPATCH_ANCHOR_UNRESOLVED, detail="ambiguous_preexisting")

    if len(existing) == 1:
        anchor, recovered = existing[0], True  # idempotent recover: NO new write
    else:
        # No current marker -> write the marker-bearing IR note ONCE.
        note = render_dispatch_note(body, lane=lane_s, lane_generation=gen_s)
        try:
            post_note(str(issue), note)
        except Exception as exc:  # noqa: BLE001 - a write failure fails closed (no anchor, no send)
            return _result(DISPATCH_WRITE_FAILED, detail=f"write:{type(exc).__name__}")
        try:
            post = list(read_entries(str(issue)))
        except Exception as exc:  # noqa: BLE001 - the intent persists; next pre-read recovers it
            return _result(DISPATCH_READBACK_FAILED, detail=f"postread:{type(exc).__name__}")
        written = dispatch_entry_journals(post, lane=lane_s, lane_generation=gen_s)
        if len(written) != 1:
            return _result(DISPATCH_ANCHOR_UNRESOLVED, detail="post_write_not_single")
        anchor, recovered = written[0], False

    # R7-F2: EXECUTE the handoff — only a delivered handoff is sendable.
    try:
        outcome = handoff_send(anchor)
    except Exception as exc:  # noqa: BLE001 - a handoff crash is a fail-closed non-delivery
        return _result(DISPATCH_HANDOFF_FAILED, dispatch_journal=anchor, detail=f"handoff:{type(exc).__name__}")
    if not getattr(outcome, "delivered", False):
        return _result(
            DISPATCH_HANDOFF_FAILED, dispatch_journal=anchor,
            detail=getattr(outcome, "detail", "") or "handoff_not_delivered",
        )
    status = DISPATCH_RECOVERED if recovered else DISPATCH_WRITTEN
    return _result(status, dispatch_journal=anchor, handoff_delivered=True)


def build_live_ir_dispatch() -> "tuple[Callable[[str, str], object], Callable[[str], Iterable[RedmineJournalEntry]]]":
    """Build the live ``(post_note, read_entries)`` seams for :func:`dispatch_implementation_request`.

    - ``post_note`` is the opt-in-gated live Redmine note transport
      (:func:`redmine_delivery_transport_from_env` -> ``post_issue_note``); if the live-write opt-in
      is unset it raises, so an un-opted-in dispatch fails closed at :data:`DISPATCH_WRITE_FAILED`;
    - ``read_entries`` is the read-only live journal source (:meth:`LiveRedmineJournalSource.from_environment`),
      the same trusted-credential reader the reconciler polls with.
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


def build_live_handoff_send(*, issue: str, route: DispatchRoute) -> Callable[[str], HandoffOutcome]:
    """Build the live handoff port: drive the gated ``handoff send`` in-process, return its outcome.

    Mirrors the sublane actuator's live drive (``sublane_actuator_ops._drive_cli``): parse the
    ``handoff send`` argv with the composed CLI parser and run its handler, so the anchored IR
    dispatch is byte-for-byte the operator's gated command (the ``require_tmux`` gate, route gates,
    and delivery emission all apply). A non-zero exit / raised gate -> ``delivered=False`` (the
    durable marker persists for a retry).
    """

    def _send(anchor: str) -> HandoffOutcome:
        from mozyo_bridge.application.cli import build_parser, normalize_paths

        argv = build_ir_handoff_argv(anchor, route, issue=issue)
        try:
            args = normalize_paths(build_parser().parse_args(argv))
            rc = int(args.func(args))
        except SystemExit as exc:  # argparse / gate refusal -> non-delivery, never crash the caller
            return HandoffOutcome(delivered=False, detail=f"handoff_exit:{exc.code}")
        except Exception as exc:  # noqa: BLE001 - a drive failure is a fail-closed non-delivery
            return HandoffOutcome(delivered=False, detail=f"handoff_error:{type(exc).__name__}")
        return HandoffOutcome(delivered=(rc == 0), detail="" if rc == 0 else f"handoff_rc:{rc}")

    return _send


__all__ = (
    "DISPATCH_EXPECTED_GATE",
    "DISPATCH_ROLE_PROFILE",
    "DISPATCH_WRITTEN",
    "DISPATCH_RECOVERED",
    "DISPATCH_INPUT_INVALID",
    "DISPATCH_WRITE_FAILED",
    "DISPATCH_READBACK_FAILED",
    "DISPATCH_ANCHOR_UNRESOLVED",
    "DISPATCH_HANDOFF_FAILED",
    "DISPATCH_SENDABLE",
    "DispatchRoute",
    "HandoffOutcome",
    "IrDispatchResult",
    "build_ir_handoff_argv",
    "dispatch_implementation_request",
    "build_live_ir_dispatch",
    "build_live_handoff_send",
)
