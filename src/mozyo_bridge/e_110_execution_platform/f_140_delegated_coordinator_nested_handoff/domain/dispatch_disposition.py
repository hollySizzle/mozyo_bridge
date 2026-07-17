"""The dispatch-disposition marker: proof that one exact dispatch action was discharged.

Redmine #13892 (design answer j#80629, Option 1A; review j#80620 R5-F1).

`herdr session-retire` must prove no work is owed to the pair it is about to close. A
`delivered` dispatch-outbox row is a **delivery ACK** — it proves the message landed, never
that the work finished — so it can be neither waved through nor blocked forever (blocking
forever makes any pair that was ever dispatched to un-retirable, the permanent-stuck this
ticket exists to remove). The disposition must therefore be read from the source of truth.

But nothing there could answer it: repo-wide, ``action_id`` was written into exactly ONE
marker — the AUTHORIZE marker (``dispatch_authorization``) — and no terminal marker echoed it.
The gate vocabulary (``implementation_done`` / ``review_request`` / …) records transitions on
an *issue*, which cannot say which dispatch **round** finished when an issue has several.

This module is the missing half: a dedicated structured channel that states the causal
correspondence between one dispatch action and the gate that closed it.

Deliberate boundaries (j#80629):

- **``review_request`` is the ONLY positive terminal gate.** ``implementation_done`` is not
  terminal — this very issue's j#80627 is an ``implementation_done`` that explicitly reports
  "partial, incomplete", so treating it as discharge would false-discharge on a journal the
  worker wrote while still owing work. ``blocked`` / progress / callback delivery /
  ``dead_letter`` / a partial ``implementation_done`` never discharge.
- **The issue identity comes from the OWNING journal entry, never from the marker body.** A
  marker that self-reported its issue could name someone else's.
- **The writer is the implementation_gateway.** A worker's own claim of completion is not a
  discharge; the same separation as "a delivery ACK is not completion", applied to delegation.
- **This is not a workflow gate.** The channel is deliberately absent from the watcher's
  recognized channels and from ``GATE_BEARING_KINDS``, so it never becomes a callback / gate
  candidate — it explains a correlation, it does not drive the workflow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

#: The dedicated channel. NOT in the journal source's recognized channels and NOT a gate kind:
#: widening either would turn a correlation record into a workflow event.
MARKER_CHANNEL_DISPATCH_DISPOSITION = "dispatch-disposition"

#: The only gate that positively terminates a worker dispatch round (j#80629).
TERMINAL_GATE_REVIEW_REQUEST = "review_request"
#: The only conclusion this marker may carry. A fixed value, so a marker that says anything
#: else is invalid rather than "some other outcome".
CONCLUSION_DISCHARGED = "discharged"
#: The only role permitted to record a disposition.
RECORDED_BY_IMPLEMENTATION_GATEWAY = "implementation_gateway"

_REQUIRED_FIELDS = (
    "action_id",
    "dispatch_journal",
    "workspace_id",
    "lane_id",
    "target_assigned_name",
    "terminal_gate",
    "terminal_journal",
    "conclusion",
    "recorded_by_role",
)

_MARKER_RE = re.compile(r"\[mozyo:(?P<channel>[a-z0-9_-]+):(?P<body>[^\]]*)\]")


def _norm(value) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DispatchDisposition:
    """One recorded disposition: this exact dispatch action reached this exact terminal gate.

    ``issue`` is the id of the journal entry that OWNS the marker — never a value the body
    claimed. ``journal`` is that entry's own id.
    """

    issue: str
    journal: str
    action_id: str
    dispatch_journal: str
    workspace_id: str
    lane_id: str
    target_assigned_name: str
    terminal_gate: str
    terminal_journal: str
    conclusion: str
    recorded_by_role: str

    @property
    def causal_key(self) -> tuple[str, str, str, str, str, str]:
        """The identity a dispatch row must match exactly to be discharged by this."""
        return (
            self.issue,
            self.workspace_id,
            self.lane_id,
            self.target_assigned_name,
            self.dispatch_journal,
            self.action_id,
        )

    @property
    def payload_key(self) -> tuple:
        """The full semantic payload — two records with the same one are the same claim."""
        return self.causal_key + (
            self.terminal_gate,
            self.terminal_journal,
            self.conclusion,
            self.recorded_by_role,
        )

    @property
    def fixed_fields_valid(self) -> bool:
        """The fixed vocabulary must be literal: anything else is an invalid marker."""
        return (
            self.terminal_gate == TERMINAL_GATE_REVIEW_REQUEST
            and self.conclusion == CONCLUSION_DISCHARGED
            and self.recorded_by_role == RECORDED_BY_IMPLEMENTATION_GATEWAY
        )


def render_dispatch_disposition_marker(
    *,
    action_id: str,
    dispatch_journal: str,
    workspace_id: str,
    lane_id: str,
    target_assigned_name: str,
    terminal_journal: str,
) -> str:
    """The canonical marker text. (pure)

    The fixed fields are emitted literally — a caller cannot choose a different terminal gate,
    conclusion or recording role, because those are the contract, not parameters. The issue is
    deliberately NOT a field: it comes from the entry that owns the marker.
    """
    for name, value in (
        ("action_id", action_id),
        ("dispatch_journal", dispatch_journal),
        ("workspace_id", workspace_id),
        ("lane_id", lane_id),
        ("target_assigned_name", target_assigned_name),
        ("terminal_journal", terminal_journal),
    ):
        if not _norm(value):
            raise ValueError(
                f"a dispatch disposition requires a non-empty {name}; a blank identity could "
                "never name one exact dispatch round"
            )
    fields = [
        f"action_id={_norm(action_id)}",
        f"dispatch_journal={_norm(dispatch_journal)}",
        f"workspace_id={_norm(workspace_id)}",
        f"lane_id={_norm(lane_id)}",
        f"target_assigned_name={_norm(target_assigned_name)}",
        f"terminal_gate={TERMINAL_GATE_REVIEW_REQUEST}",
        f"terminal_journal={_norm(terminal_journal)}",
        f"conclusion={CONCLUSION_DISCHARGED}",
        f"recorded_by_role={RECORDED_BY_IMPLEMENTATION_GATEWAY}",
    ]
    return f"[mozyo:{MARKER_CHANNEL_DISPATCH_DISPOSITION}:" + ":".join(fields) + "]"


def _parse_fields(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in body.split(":"):
        key, sep, value = chunk.partition("=")
        if not sep:
            continue
        out[key.strip()] = value.strip()
    return out


def parse_dispatch_dispositions(entry) -> tuple[DispatchDisposition, ...]:
    """Every well-formed disposition in ONE journal entry. (pure, never raises)

    ``issue`` / ``journal`` are taken from the entry, so a marker cannot claim an issue it does
    not live in. A marker missing any required field is dropped rather than half-read; the
    fixed-field check is left to the caller so an invalid-but-present marker can be surfaced
    as a *block* rather than silently ignored.
    """
    notes = _norm(getattr(entry, "notes", ""))
    issue = _norm(getattr(entry, "issue_id", ""))
    journal = _norm(getattr(entry, "journal_id", ""))
    if not notes or not issue:
        return ()
    found: list[DispatchDisposition] = []
    for match in _MARKER_RE.finditer(notes):
        if match.group("channel") != MARKER_CHANNEL_DISPATCH_DISPOSITION:
            continue
        fields = _parse_fields(match.group("body"))
        if any(not _norm(fields.get(name)) for name in _REQUIRED_FIELDS):
            continue  # incomplete: names no exact round
        found.append(
            DispatchDisposition(
                issue=issue,
                journal=journal,
                action_id=_norm(fields["action_id"]),
                dispatch_journal=_norm(fields["dispatch_journal"]),
                workspace_id=_norm(fields["workspace_id"]),
                lane_id=_norm(fields["lane_id"]),
                target_assigned_name=_norm(fields["target_assigned_name"]),
                terminal_gate=_norm(fields["terminal_gate"]),
                terminal_journal=_norm(fields["terminal_journal"]),
                conclusion=_norm(fields["conclusion"]),
                recorded_by_role=_norm(fields["recorded_by_role"]),
            )
        )
    return tuple(found)


# -- the correlation verdict -------------------------------------------------

#: The three-way correspondence held: this exact action is discharged.
CORRELATION_DISCHARGED = "discharged"
#: Nothing claims this action finished. It is still owed.
CORRELATION_OWED = "owed"
#: The evidence exists but cannot be trusted (invalid / duplicate / conflicting / out of
#: order / foreign). Never read as discharged.
CORRELATION_AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class CorrelationVerdict:
    state: str
    detail: str = ""

    @property
    def discharged(self) -> bool:
        return self.state == CORRELATION_DISCHARGED


@dataclass(frozen=True)
class DispatchRowIdentity:
    """The dispatch outbox row's FULL causal identity — never a subset (review j#80523 R2-F2)."""

    issue: str
    journal: str
    workspace_id: str
    lane_id: str
    target_assigned_name: str
    action_id: str


def _entry_order(entries: Sequence) -> dict[str, int]:
    """Journal position by id. Order is read from the source's sequence, not from id math."""
    return {_norm(getattr(e, "journal_id", "")): i for i, e in enumerate(entries)}


def correlate_dispatch_disposition(
    row: DispatchRowIdentity,
    entries: Sequence,
    *,
    authorize_journals: Mapping[str, "object"],
    review_request_journals: Sequence[str],
) -> CorrelationVerdict:
    """Is this exact dispatch action positively discharged? (pure, fail-closed)

    Requires ALL THREE, in order (j#80629):

    1. the row's ``dispatch_journal`` owns a valid AUTHORIZE whose issue / workspace / lane /
       target / action_id match the row exactly;
    2. a later journal on the same issue carries a canonical ``review_request`` gate;
    3. a still-later disposition names that exact action AND that exact terminal journal, with
       the fixed fields literal.

    Everything else fails closed. A missing correspondence is :data:`CORRELATION_OWED` (the
    work may simply still be running); evidence that exists but cannot be trusted is
    :data:`CORRELATION_AMBIGUOUS`. Neither ever reads as discharged.
    """
    if not _norm(row.action_id) or not _norm(row.journal) or not _norm(row.issue):
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            "the dispatch row carries a blank causal identity; no exact round can be named",
        )

    # (1) the AUTHORIZE this row came from must exist and match the row exactly.
    auth = authorize_journals.get(_norm(row.journal))
    if auth is None:
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            f"journal {row.journal} carries no valid AUTHORIZE for this row; the dispatch's "
            "own origin cannot be confirmed",
        )
    if (
        _norm(getattr(auth, "issue", "")) != _norm(row.issue)
        or _norm(getattr(auth, "workspace_id", "")) != _norm(row.workspace_id)
        or _norm(getattr(auth, "lane_id", "")) != _norm(row.lane_id)
        or _norm(getattr(auth, "target_assigned_name", ""))
        != _norm(row.target_assigned_name)
        or _norm(getattr(auth, "action_id", "")) != _norm(row.action_id)
    ):
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            "the AUTHORIZE at this row's journal names a different identity; refusing to "
            "correlate across a foreign dispatch",
        )

    order = _entry_order(entries)
    dispatch_pos = order.get(_norm(row.journal))
    if dispatch_pos is None:
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            f"the dispatch journal {row.journal} is not in the read history",
        )

    # (3) collect dispositions naming this exact action, de-duplicating identical claims.
    claims: dict[tuple, DispatchDisposition] = {}
    for entry in entries:
        for disp in parse_dispatch_dispositions(entry):
            if disp.causal_key != (
                _norm(row.issue),
                _norm(row.workspace_id),
                _norm(row.lane_id),
                _norm(row.target_assigned_name),
                _norm(row.journal),
                _norm(row.action_id),
            ):
                continue
            if not disp.fixed_fields_valid:
                return CorrelationVerdict(
                    CORRELATION_AMBIGUOUS,
                    "a disposition for this action carries an invalid fixed field "
                    f"(gate={disp.terminal_gate!r} conclusion={disp.conclusion!r} "
                    f"role={disp.recorded_by_role!r}); refusing to interpret it",
                )
            claims[disp.payload_key] = disp
    if not claims:
        return CorrelationVerdict(
            CORRELATION_OWED,
            "no disposition record claims this dispatch action was discharged",
        )
    if len(claims) > 1:
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            "conflicting dispositions name this action with different terminal journals or "
            "identities; refusing to pick one",
        )
    disp = next(iter(claims.values()))

    # (2) the named terminal journal must be a real, later review_request on this issue.
    if _norm(disp.terminal_journal) not in {_norm(j) for j in review_request_journals}:
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            f"the disposition names terminal journal {disp.terminal_journal}, which carries "
            "no canonical review_request gate on this issue",
        )
    terminal_pos = order.get(_norm(disp.terminal_journal))
    disp_pos = order.get(_norm(disp.journal))
    if terminal_pos is None or disp_pos is None:
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS, "the disposition's journals are not in the read history"
        )
    if not (dispatch_pos < terminal_pos < disp_pos):
        return CorrelationVerdict(
            CORRELATION_AMBIGUOUS,
            "the disposition's causal order is inverted (dispatch -> review_request -> "
            "disposition is required); refusing to read it as a discharge",
        )
    return CorrelationVerdict(CORRELATION_DISCHARGED)


__all__ = (
    "MARKER_CHANNEL_DISPATCH_DISPOSITION",
    "TERMINAL_GATE_REVIEW_REQUEST",
    "CONCLUSION_DISCHARGED",
    "RECORDED_BY_IMPLEMENTATION_GATEWAY",
    "DispatchDisposition",
    "DispatchRowIdentity",
    "CorrelationVerdict",
    "CORRELATION_DISCHARGED",
    "CORRELATION_OWED",
    "CORRELATION_AMBIGUOUS",
    "render_dispatch_disposition_marker",
    "parse_dispatch_dispositions",
    "correlate_dispatch_disposition",
)
