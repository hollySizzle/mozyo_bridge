"""The canonical dispatch-disposition writer (Redmine #13892, design j#80629).

The ONLY sanctioned producer of a ``dispatch-disposition`` marker. It exists so the record
that discharges a dispatch action is made by the role that can attest it, from a fresh read of
the source of truth — never from prose, a pane, a delivery ACK, or a store that does not own
the completion.

Writer contract (j#80629), enforced here rather than trusted:

1. the ``dispatch_journal`` must own **exactly one** valid ``AUTHORIZE`` whose issue /
   workspace / lane / target / action_id match the request exactly;
2. the ``terminal_journal`` must be a **later** journal on the same issue carrying a canonical
   ``review_request`` gate — the only positive terminal gate (``implementation_done`` is not
   one: a partial implementation_done is a routine, truthful shape, so treating it as discharge
   would false-discharge work the worker still owes);
3. the record is appended **after** the terminal journal, with the fixed vocabulary literal;
4. an identical prior record is an idempotent no-op; a conflicting one is refused zero-write.

``recorded_by_role`` is fixed to ``implementation_gateway``: a worker's own claim of completion
is not a discharge. Historical repair uses this same producer and the same fresh checks — there
is no backfill path that skips them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_disposition import (  # noqa: E501
    RECORDED_BY_IMPLEMENTATION_GATEWAY,
    parse_dispatch_dispositions,
    render_dispatch_disposition_marker,
)

#: The record was appended.
WRITE_RECORDED = "recorded"
#: An identical record already exists — an idempotent retry, nothing written.
WRITE_ALREADY_RECORDED = "already_recorded"
#: Refused. Nothing written.
WRITE_REFUSED = "refused"

REASON_NO_AUTHORIZE = "authorize_not_found"
REASON_AUTHORIZE_MISMATCH = "authorize_identity_mismatch"
REASON_AUTHORIZE_AMBIGUOUS = "authorize_ambiguous"
REASON_NO_TERMINAL_GATE = "terminal_gate_not_found"
REASON_ORDER_INVERTED = "order_inverted"
REASON_CONFLICTING_RECORD = "conflicting_record"
REASON_SOURCE_UNREADABLE = "source_unreadable"
REASON_BLANK_IDENTITY = "blank_identity"


def _norm(value) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DispositionWriteResult:
    state: str
    reason: str = ""
    detail: str = ""
    marker: str = ""

    @property
    def ok(self) -> bool:
        """A fresh append and a proven idempotent retry are both non-error outcomes."""
        return self.state in (WRITE_RECORDED, WRITE_ALREADY_RECORDED)

    @property
    def wrote(self) -> bool:
        return self.state == WRITE_RECORDED


def _refused(reason: str, detail: str) -> DispositionWriteResult:
    return DispositionWriteResult(state=WRITE_REFUSED, reason=reason, detail=detail)


def record_dispatch_disposition(
    *,
    issue: str,
    dispatch_journal: str,
    terminal_journal: str,
    workspace_id: str,
    lane_id: str,
    target_assigned_name: str,
    action_id: str,
    source,
    append_note,
) -> DispositionWriteResult:
    """Attest that one exact dispatch action was discharged, or refuse. (zero-write on refusal)

    ``source`` is a :class:`...domain.redmine_journal_source.RedmineJournalSource` — the live
    composition passes ``LiveRedmineJournalSource.from_environment()``, so the checks run
    against a **fresh** read of the source of truth rather than anything the caller believed.
    ``append_note(issue, text) -> None`` performs the one durable append.

    Every refusal writes nothing. An unreadable source refuses rather than assuming: a record
    this producer cannot justify is worse than no record, because the reader treats it as proof.
    """
    for name, value in (
        ("issue", issue),
        ("dispatch_journal", dispatch_journal),
        ("terminal_journal", terminal_journal),
        ("workspace_id", workspace_id),
        ("lane_id", lane_id),
        ("target_assigned_name", target_assigned_name),
        ("action_id", action_id),
    ):
        if not _norm(value):
            return _refused(
                REASON_BLANK_IDENTITY,
                f"a disposition requires a non-empty {name}; a blank identity names no round",
            )

    try:
        entries = list(source.read_entries(_norm(issue)))
    except Exception as exc:  # noqa: BLE001 - never attest from an unread source
        return _refused(
            REASON_SOURCE_UNREADABLE,
            f"the source of truth could not be read ({exc}); refusing to attest a discharge "
            "this producer cannot verify",
        )

    order = {_norm(getattr(e, "journal_id", "")): i for i, e in enumerate(entries)}

    # (1) exactly one valid AUTHORIZE at the dispatch journal, matching the request exactly.
    auths = _authorizations_at(entries, _norm(dispatch_journal))
    if not auths:
        return _refused(
            REASON_NO_AUTHORIZE,
            f"journal {dispatch_journal} carries no valid AUTHORIZE; the dispatch this "
            "disposition claims to close cannot be confirmed",
        )
    if len(auths) > 1:
        return _refused(
            REASON_AUTHORIZE_AMBIGUOUS,
            f"journal {dispatch_journal} carries {len(auths)} valid AUTHORIZE markers; "
            "refusing to guess which round this discharges",
        )
    auth = auths[0]
    if (
        _norm(getattr(auth, "issue", "")) != _norm(issue)
        or _norm(getattr(auth, "workspace_id", "")) != _norm(workspace_id)
        or _norm(getattr(auth, "lane_id", "")) != _norm(lane_id)
        or _norm(getattr(auth, "target_assigned_name", "")) != _norm(target_assigned_name)
        or _norm(getattr(auth, "action_id", "")) != _norm(action_id)
    ):
        return _refused(
            REASON_AUTHORIZE_MISMATCH,
            "the AUTHORIZE at that journal names a different identity; refusing to attest "
            "across a foreign dispatch",
        )

    # (2) the terminal journal must be a LATER canonical review_request on this issue.
    if _norm(terminal_journal) not in _review_request_journals(entries):
        return _refused(
            REASON_NO_TERMINAL_GATE,
            f"journal {terminal_journal} carries no canonical review_request gate on issue "
            f"{issue}; only a review_request positively terminates a dispatch round",
        )
    dispatch_pos = order.get(_norm(dispatch_journal))
    terminal_pos = order.get(_norm(terminal_journal))
    if dispatch_pos is None or terminal_pos is None or not dispatch_pos < terminal_pos:
        return _refused(
            REASON_ORDER_INVERTED,
            "the terminal journal does not follow the dispatch journal; a gate that precedes "
            "the dispatch cannot have closed it",
        )

    marker = render_dispatch_disposition_marker(
        action_id=_norm(action_id),
        dispatch_journal=_norm(dispatch_journal),
        workspace_id=_norm(workspace_id),
        lane_id=_norm(lane_id),
        target_assigned_name=_norm(target_assigned_name),
        terminal_journal=_norm(terminal_journal),
    )

    # (4) idempotency / conflict, decided against the SAME fresh read.
    causal = (
        _norm(issue),
        _norm(workspace_id),
        _norm(lane_id),
        _norm(target_assigned_name),
        _norm(dispatch_journal),
        _norm(action_id),
    )
    for entry in entries:
        for prior in parse_dispatch_dispositions(entry):
            if prior.causal_key != causal:
                continue
            if (
                prior.terminal_journal == _norm(terminal_journal)
                and prior.recorded_by_role == RECORDED_BY_IMPLEMENTATION_GATEWAY
                and prior.fixed_fields_valid
            ):
                return DispositionWriteResult(
                    state=WRITE_ALREADY_RECORDED,
                    detail=f"an identical disposition is already recorded at j#{prior.journal}",
                    marker=marker,
                )
            return _refused(
                REASON_CONFLICTING_RECORD,
                f"a different disposition for this action already exists at j#{prior.journal} "
                f"(terminal {prior.terminal_journal!r}); refusing to record a second, "
                "conflicting claim",
            )

    append_note(_norm(issue), marker)
    return DispositionWriteResult(state=WRITE_RECORDED, marker=marker)


def _authorizations_at(entries: Sequence, journal: str) -> list:
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
        parse_dispatch_authorizations,
    )

    return [
        a
        for a in parse_dispatch_authorizations(entries)
        if a.valid and _norm(a.journal) == journal
    ]


def _review_request_journals(entries: Sequence) -> set:
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
        extract_markers,
    )

    return {
        _norm(m.journal)
        for m in extract_markers(entries)
        if _norm(m.gate) == "review_request"
    }


__all__ = (
    "WRITE_RECORDED",
    "WRITE_ALREADY_RECORDED",
    "WRITE_REFUSED",
    "REASON_NO_AUTHORIZE",
    "REASON_AUTHORIZE_MISMATCH",
    "REASON_AUTHORIZE_AMBIGUOUS",
    "REASON_NO_TERMINAL_GATE",
    "REASON_ORDER_INVERTED",
    "REASON_CONFLICTING_RECORD",
    "REASON_SOURCE_UNREADABLE",
    "REASON_BLANK_IDENTITY",
    "DispositionWriteResult",
    "record_dispatch_disposition",
)
