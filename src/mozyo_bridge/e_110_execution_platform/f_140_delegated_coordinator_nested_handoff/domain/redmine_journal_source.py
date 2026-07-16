"""Redmine journal read boundary: structured markers -> JournalMarker (Redmine #12672).

The #12672 watcher's purpose is that **Redmine issue/journal history is the event source**:
a recorded ``review_request`` / ``review_result`` / ``implementation_done`` on a Redmine
issue must become a pending workflow action. The intake front-end
(:mod:`...domain.redmine_event_intake`) folds :class:`JournalMarker` inputs into a pending
action, but on its own it only consumes markers a caller already supplied — it does not
*read Redmine* (review j#68992 finding 1). This module is that missing read boundary: it
reads a Redmine issue's journal entries and extracts the **structured gate markers** in them
into :class:`JournalMarker` inputs, so ``workflow watch`` ingests real Redmine history rather
than only hand-typed ``--marker`` strings.

The boundary holds the issue's design intent — *structured marker / gate schema, never a
natural-language guess*:

- a gate is read from a **machine token**, the same ``[mozyo:<channel>:k=v:...]`` marker the
  handoff path already standardizes on (:func:`...domain.handoff.build_marker` emits
  ``[mozyo:handoff:source=redmine:issue=…:journal=…:kind=<gate>:to=…]``). A note with no such
  token yields no marker — the watcher never infers a gate from prose;
- the gate vocabulary stays core-owned: only the gate-bearing kinds
  (:data:`GATE_BEARING_KINDS`, mirroring the adapter's ``WORKFLOW_GATE_KINDS``) become a
  marker; ``implementation_request`` / ``design_consultation`` / ``reply`` and other non-gate
  kinds are skipped;
- each Redmine journal **entry** is one durable event, keyed by its own
  ``redmine:<issue>:<journal_id>`` anchor (:func:`...domain.redmine_event_intake.redmine_event_id`),
  so re-reading the same journal is deduplicated downstream.

Scope boundary (kept deliberately narrow, like the #12857 first slice): this reads a
**supplied snapshot** of Redmine journal history (the ``/issues/<id>.json?include=journals``
shape an operator / MCP already fetched) via the pure
:class:`MappingRedmineJournalSource`, and exposes a :class:`RedmineJournalSource` port so a
live, credentialed auto-poll adapter (a network read using the existing read-only
``redmine_context`` machinery + a since/updated_on cursor) drops in behind the same port. That
live poll adapter now exists as the credential-gated
:class:`...application.live_redmine_journal_source.LiveRedmineJournalSource` (Redmine #13289,
wired into ``workflow watch --poll``); it reuses the read / extract / convert boundary
implemented and tested here rather than reimplementing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_event_intake import (
    JournalMarker,
    JournalMarkerError,
    build_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (
    REVIEW_PENDING,
)

# ---------------------------------------------------------------------------
# Structured marker schema (machine token, never prose). The watcher recognizes the
# existing handoff marker channel and a dedicated workflow-event channel; both are
# ``[mozyo:<channel>:key=value:key=value:...]`` with ':'-separated key=value fields.
# ---------------------------------------------------------------------------

#: The handoff marker channel (:func:`...domain.handoff.build_marker`). Its ``kind`` field
#: carries the gate; the source anchor is ``issue`` / ``journal``.
MARKER_CHANNEL_HANDOFF = "handoff"
#: A dedicated watcher channel a gate journal can embed to carry the full structured event
#: (gate + conclusion / callback / commit / integrated / open / blocker). Its gate field is
#: ``gate`` (``kind`` is also accepted as an alias).
MARKER_CHANNEL_WORKFLOW_EVENT = "workflow-event"

_RECOGNIZED_CHANNELS = frozenset(
    {MARKER_CHANNEL_HANDOFF, MARKER_CHANNEL_WORKFLOW_EVENT}
)

#: The **callback-required** gate kinds a marker may name — the states that must wake the
#: coordinator (``skills/mozyo-bridge-agent/references/workflow.md`` ``### coordinator callback
#: を要する state``: ``implementation_done | review_request | review_result |
#: owner_close_approval_waiting | blocked``). #13520 review F5: this is DELIBERATELY broader than
#: the provider review-gate vocabulary ``WORKFLOW_GATE_KINDS`` (which excludes owner-close because
#: "close approval is satisfied" is a core decision, not a provider-observable fact) — a callback
#: only *wakes the coordinator to read the journal*, it authorizes nothing, so ``blocked`` and
#: ``owner_close_approval_waiting`` legitimately trigger a callback. Kept local so this domain
#: stays inside its bounded context and does not import the e_140 adapter. A non-gate kind
#: (``implementation_request`` / ``design_consultation`` / ``reply`` / ``start`` / ``close`` …)
#: is skipped, never guessed. ``review_result`` / ``owner_close_approval_waiting`` are the
#: marker-facing names; :data:`...redmine_event_intake.MARKER_GATE_ALIASES` maps them onto the
#: runtime ``review`` / ``owner_close_approval`` gates.
GATE_BEARING_KINDS: frozenset[str] = frozenset(
    {
        "implementation_done",
        "review_request",
        "review_result",
        "owner_close_approval_waiting",
        "blocked",
    }
)

#: ``[mozyo:<channel>:<body>]`` — the body is the ':'-separated key=value field list.
_MARKER_RE = re.compile(r"\[mozyo:(?P<channel>[a-z0-9_-]+):(?P<body>[^\]]*)\]")


def _parse_marker_fields(body: str) -> dict[str, str]:
    """Parse a ``key=value:key=value`` marker body into a dict (pure; last write wins)."""
    fields: dict[str, str] = {}
    for token in body.split(":"):
        token = token.strip()
        if not token:
            continue
        key, eq, value = token.partition("=")
        if not eq:
            continue
        fields[key.strip()] = value.strip()
    return fields


def marker_fields_in_note(notes: str) -> tuple[tuple[str, dict[str, str]], ...]:
    """Every ``[mozyo:<channel>:...]`` marker in a note as ``(channel, fields)``, in note order (pure).

    The shared structured-token scan the marker readers are built on: it recognizes the token
    grammar and parses the field list, but applies **no** vocabulary policy — each reader decides
    which channel / kind it accepts. Unrecognized channels are dropped here so a reader never has
    to know the channel set. Prose is never inspected; a note with no token yields ``()``.
    """
    if not notes:
        return ()
    found: list[tuple[str, dict[str, str]]] = []
    for match in _MARKER_RE.finditer(notes):
        channel = match.group("channel")
        if channel not in _RECOGNIZED_CHANNELS:
            continue
        found.append((channel, _parse_marker_fields(match.group("body"))))
    return tuple(found)


@dataclass(frozen=True)
class RedmineJournalEntry:
    """One Redmine journal entry the source yields (the durable event unit).

    ``issue_id`` / ``journal_id`` are the durable anchor (the journal record's own id, not a
    pointer the note happens to mention); ``notes`` is the verbatim note body the structured
    marker is read from. A pure value object — the source builds it from a fetched / supplied
    Redmine journals array.
    """

    issue_id: str
    journal_id: str
    notes: str


def _gate_marker_from_fields(
    entry: RedmineJournalEntry, channel: str, fields: Mapping[str, str]
) -> JournalMarker | None:
    """Build a :class:`JournalMarker` from one parsed marker, or None for a non-gate (pure).

    The gate name is the ``gate`` field (workflow-event) or ``kind`` field (handoff). Only a
    gate-bearing kind (:data:`GATE_BEARING_KINDS`) produces a marker; anything else returns
    None so a dispatch / consult / reply marker never becomes a pending action. The marker is
    keyed by the **entry's own** anchor (``issue_id`` / ``journal_id``), so each journal entry
    is one durable, dedupable event. Structured sub-fields (conclusion / callback / commit /
    integrated / open / blocker) are carried through when the workflow-event channel supplies
    them; a malformed value fails closed (skipped) rather than guessed.
    """
    kind = (fields.get("gate") or fields.get("kind") or "").strip()
    if kind not in GATE_BEARING_KINDS:
        return None

    def _flag(key: str, default: bool) -> bool:
        raw = fields.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "y")

    try:
        return build_marker(
            entry.issue_id,
            entry.journal_id,
            kind,  # build_marker maps review_result -> review and validates the vocabulary
            review_conclusion=(fields.get("conclusion") or REVIEW_PENDING).strip()
            or REVIEW_PENDING,
            callback_state=(fields.get("callback") or "none").strip() or "none",
            commit_bearing=_flag("commit", False),
            integration_recorded=_flag("integrated", False),
            issue_open=_flag("open", True),
            blocker_recorded=_flag("blocker", False),
        )
    except (JournalMarkerError, ValueError):
        # A structured marker carrying an out-of-vocabulary conclusion / callback is skipped
        # (fail-closed) rather than aborting the whole sweep or being guessed.
        return None


def extract_markers_from_note(
    issue_id: str, journal_id: str, notes: str
) -> tuple[JournalMarker, ...]:
    """Extract every structured gate marker from one journal note (pure; never prose).

    Scans ``notes`` for ``[mozyo:<channel>:...]`` tokens on a recognized channel, parses each
    into a :class:`JournalMarker` when it names a gate-bearing kind, and returns them in
    note order. A note with no recognized marker token yields ``()`` — the watcher reads the
    structured token, never the surrounding narrative.
    """
    if not notes:
        return ()
    entry = RedmineJournalEntry(
        issue_id=str(issue_id).strip(),
        journal_id=str(journal_id).strip(),
        notes=notes,
    )
    markers: list[JournalMarker] = []
    for match in _MARKER_RE.finditer(notes):
        channel = match.group("channel")
        if channel not in _RECOGNIZED_CHANNELS:
            continue
        fields = _parse_marker_fields(match.group("body"))
        marker = _gate_marker_from_fields(entry, channel, fields)
        if marker is not None:
            markers.append(marker)
    return tuple(markers)


def extract_marker(entry: RedmineJournalEntry) -> JournalMarker | None:
    """The first structured gate marker in a journal entry, or None (pure)."""
    markers = extract_markers_from_note(entry.issue_id, entry.journal_id, entry.notes)
    return markers[0] if markers else None


def extract_markers(entries: Iterable[RedmineJournalEntry]) -> tuple[JournalMarker, ...]:
    """All structured gate markers across an ordered sequence of journal entries (pure).

    One entry may carry more than one structured marker (e.g. a combined Implementation Done
    / Review Request gate journal embedding both tokens); each becomes its own
    :class:`JournalMarker`. Entries are read in order so the result is replay-stable, and the
    intake's duplicate suppression (same ``redmine:<issue>:<journal>`` anchor) handles a
    re-read of the same entry.
    """
    markers: list[JournalMarker] = []
    for entry in entries:
        markers.extend(
            extract_markers_from_note(entry.issue_id, entry.journal_id, entry.notes)
        )
    return tuple(markers)


class RedmineJournalSource(Protocol):
    """The read port ``workflow watch`` depends on to read Redmine journal history.

    A source yields the journal entries for an issue; the watcher extracts structured markers
    from them. Declared as a Protocol so the use case stays testable with an in-memory source
    and a live, credentialed HTTP adapter (the follow-up operational layer) is a drop-in.
    """

    def read_entries(self, issue_id: str) -> Sequence[RedmineJournalEntry]: ...


@dataclass(frozen=True)
class MappingRedmineJournalSource:
    """A :class:`RedmineJournalSource` over an already-fetched Redmine issue-detail mapping.

    ``payload`` is the ``/issues/<id>.json?include=journals`` (or MCP ``get_issue_detail``)
    shape an operator / MCP fetched. Both real shapes are supported:

    - the **Redmine REST** shape nests journals under the issue: ``{"issue": {"id": …,
      "journals": [...]}}``;
    - the **MCP / export wrapper** shape lifts them to the top level:
      ``{"issue": {...}, "journals": [...]}`` (or a bare ``{"journals": [...]}``).

    A top-level ``journals`` list wins when present; otherwise ``issue.journals`` is read, so
    a direct REST fetch is not silently dropped (review j#69006 finding 1). Each journal
    object contributes a :class:`RedmineJournalEntry` (its ``id`` + ``notes``); field-only
    journals with an empty ``notes`` are dropped (no marker can live in an empty note). A
    valid empty journal list simply yields no entries. Pure — it reads a supplied snapshot
    and performs no network I/O. The live network read is the follow-up adapter behind the
    same port.
    """

    payload: Mapping[str, object]

    @staticmethod
    def _as_journal_list(raw: object) -> list[Mapping[str, object]] | None:
        """A list of journal mappings from a candidate value, or None if it is not a list.

        ``None`` means "this location had no journals list" (so the caller falls back to the
        other location); an **empty** list is a valid result (no events) and is returned as
        ``[]``. A bare string is never a journals list even though ``str`` is a ``Sequence``.
        """
        if isinstance(raw, str) or not isinstance(raw, Sequence):
            return None
        return [j for j in raw if isinstance(j, Mapping)]

    def _journals(self) -> Sequence[Mapping[str, object]]:
        # Top-level journals (MCP / export wrapper shape) win when present.
        top = self._as_journal_list(self.payload.get("journals"))
        if top is not None:
            return top
        # Fall back to the Redmine REST shape, which nests journals under the issue.
        issue = self.payload.get("issue")
        if isinstance(issue, Mapping):
            nested = self._as_journal_list(issue.get("journals"))
            if nested is not None:
                return nested
        return []

    def _issue_id(self, issue_id: str | None) -> str:
        if issue_id:
            return str(issue_id).strip()
        issue = self.payload.get("issue")
        if isinstance(issue, Mapping):
            return str(issue.get("id", "")).strip()
        return ""

    def read_entries(self, issue_id: str | None = None) -> list[RedmineJournalEntry]:
        resolved = self._issue_id(issue_id)
        entries: list[RedmineJournalEntry] = []
        for journal in self._journals():
            jid = str(journal.get("id", "")).strip()
            notes = str(journal.get("notes", "") or "").strip()
            if not jid or not notes:
                continue
            entries.append(
                RedmineJournalEntry(issue_id=resolved, journal_id=jid, notes=notes)
            )
        return entries


def markers_from_source(
    source: RedmineJournalSource, issue_id: str
) -> tuple[JournalMarker, ...]:
    """Read an issue's journal entries from ``source`` and extract its gate markers (pure)."""
    return extract_markers(source.read_entries(issue_id))


# ---------------------------------------------------------------------------
# Dispatch marker — a SEPARATE closed vocabulary from the gate-bearing kinds (Redmine #13758
# review R5-F3 / Design Answer j#79507 Q2). A dispatch (implementation_request) is NOT a
# callback-required gate, so it must NOT widen GATE_BEARING_KINDS or be mis-promoted to a
# callback event. The canonical Implementation Request writer embeds this marker in the IR
# journal body; the reconciler reads the marker's OWNING Redmine entry journal_id as the exact
# dispatch anchor (NOT the marker's self-reported ``journal=`` — no self-reference / chicken-
# and-egg). A legacy prose-only IR (no marker) is fail-closed: never parse-guessed.
# ---------------------------------------------------------------------------
DISPATCH_KIND_IMPLEMENTATION_REQUEST = "implementation_request"


def render_dispatch_marker(lane: str, lane_generation: object) -> str:
    """The structured dispatch marker for an IR journal (pure; the producer inverse of the reader).

    ``[mozyo:workflow-event:kind=implementation_request:lane=<lane>:lane_generation=<n>]`` — the
    canonical Implementation Request writer embeds this in the IR journal body so the reconciler
    can resolve the exact dispatch anchor from the owning entry's journal id (Design Answer
    j#79507 Q2). A separate closed vocabulary from :func:`render_workflow_event_marker` (which is
    for callback gate kinds); this token names ``implementation_request`` and carries no gate.
    """
    lane_s = str(lane or "").strip()
    gen_s = str(lane_generation if lane_generation is not None else "").strip()
    return (
        f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:"
        f"kind={DISPATCH_KIND_IMPLEMENTATION_REQUEST}:lane={lane_s}:lane_generation={gen_s}]"
    )


def render_dispatch_note(body: str, *, lane: str, lane_generation: object) -> str:
    """A canonical Implementation Request note: prose ``body`` + the embedded dispatch marker."""
    marker = render_dispatch_marker(lane, lane_generation)
    body = str(body or "").rstrip()
    return f"{body}\n\n{marker}" if body else marker


def dispatch_entry_journals(
    entries: "Iterable[RedmineJournalEntry]",
    *,
    lane: str,
    lane_generation: object,
) -> "tuple[str, ...]":
    """The DISTINCT owning entry journal ids that carry a current dispatch marker (pure, sorted).

    Scans the ``[mozyo:workflow-event:kind=implementation_request:...]`` markers whose ``lane`` /
    ``lane_generation`` match and returns each OWNING entry's ``journal_id`` (deduped, sorted). The
    length distinguishes the three cases the writer's idempotency needs (Redmine #13758 R7-F3):
    ``0`` — no current dispatch (a legacy prose-only IR — never guessed, and the point to write a
    fresh marker); ``1`` — the exact current dispatch (recover / anchor); ``>=2`` — an ambiguous /
    foreign duplicate (zero-send, and never add a further marker). A same-entry re-read dedups to
    one id (a same IR-journal retry is the same dispatch).
    """
    lane_s = str(lane or "").strip()
    gen_s = str(lane_generation if lane_generation is not None else "").strip()
    if not (lane_s and gen_s):
        return ()
    found: set[str] = set()
    for entry in entries or ():
        entry_journal = str(getattr(entry, "journal_id", "") or "").strip()
        if not entry_journal:
            continue
        for channel, fields in marker_fields_in_note(getattr(entry, "notes", "") or ""):
            if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
                continue
            if str(fields.get("kind", "")).strip() != DISPATCH_KIND_IMPLEMENTATION_REQUEST:
                continue
            if str(fields.get("lane", "")).strip() != lane_s:
                continue
            if str(fields.get("lane_generation", "")).strip() != gen_s:
                continue
            found.add(entry_journal)
    return tuple(sorted(found))


def dispatch_generations(entries: "Iterable[RedmineJournalEntry]", *, lane: str) -> "tuple[int, ...]":
    """Every lane_generation this lane has a dispatch marker for (pure, sorted ascending).

    The **round authority** a sweep needs to notice it is reasoning about a superseded round
    (Redmine #13889 review F3): :func:`resolve_dispatch_entry_journal` answers "where is round N's
    anchor" for a generation the caller already fixed, so it can never reveal that round N+1 has
    since opened. This scans the same durable dispatch markers WITHOUT fixing a generation, so a
    caller can compare the round it is sweeping against the newest round on the record. A
    non-numeric / blank generation is skipped (never guessed).
    """
    lane_s = str(lane or "").strip()
    if not lane_s:
        return ()
    found: set[int] = set()
    for entry in entries or ():
        for channel, fields in marker_fields_in_note(getattr(entry, "notes", "") or ""):
            if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
                continue
            if str(fields.get("kind", "")).strip() != DISPATCH_KIND_IMPLEMENTATION_REQUEST:
                continue
            if str(fields.get("lane", "")).strip() != lane_s:
                continue
            raw = str(fields.get("lane_generation", "")).strip()
            try:
                found.add(int(raw))
            except (TypeError, ValueError):
                continue
    return tuple(sorted(found))


def resolve_dispatch_entry_journal(
    entries: "Iterable[RedmineJournalEntry]",
    *,
    lane: str,
    lane_generation: object,
) -> str:
    """The Redmine entry journal id of the CURRENT dispatch for ``(lane, lane_generation)`` (pure).

    The exact dispatch anchor (Design Answer j#79507 Q2): the OWNING entry's ``journal_id`` of the
    single current ``kind=implementation_request`` marker — the anchor authority is the durable
    entry, not the marker's self-reported fields. Fail-closed to ``""`` (zero-send) unless EXACTLY
    ONE such entry exists (see :func:`dispatch_entry_journals`): zero matches (a legacy prose-only
    IR — never guessed) or two-or-more distinct entries (ambiguous / foreign) both return ``""``.
    """
    journals = dispatch_entry_journals(entries, lane=lane, lane_generation=lane_generation)
    return journals[0] if len(journals) == 1 else ""


def dispatch_entry_journal_from_source(
    source: RedmineJournalSource, issue_id: str, *, lane: str, lane_generation: object
) -> str:
    """Read the issue's entries and resolve the current dispatch entry journal (over ``source``)."""
    return resolve_dispatch_entry_journal(
        source.read_entries(issue_id), lane=lane, lane_generation=lane_generation
    )


def render_workflow_event_marker(
    gate: str,
    *,
    conclusion: str | None = None,
    callback: str | None = None,
    commit_bearing: bool | None = None,
    integration_recorded: bool | None = None,
    issue_open: bool | None = None,
    blocker_recorded: bool | None = None,
) -> str:
    """Render the structured ``[mozyo:workflow-event:...]`` gate marker for a gate journal (pure).

    This is the **producer** inverse of :func:`extract_markers_from_note` (#13520 review F1-R1):
    an agent recording a handoff-worthy gate journal (implementation_done / review_request /
    review_result) embeds the returned token in the journal notes so the callback watcher can
    **discover** the gate structurally later — the watcher reads the machine token, never the
    surrounding prose. Only the fields that are set are emitted (a bare marker carries just the
    gate). ``gate`` must be a gate-bearing kind (:data:`GATE_BEARING_KINDS`); anything else is a
    programming error and raises. The output round-trips through
    :func:`extract_markers_from_note` back to the same :class:`JournalMarker`.
    """
    gate_s = str(gate).strip()
    if gate_s not in GATE_BEARING_KINDS:
        raise ValueError(
            f"render_workflow_event_marker gate must be one of {sorted(GATE_BEARING_KINDS)}, "
            f"got {gate!r}"
        )
    fields = [f"gate={gate_s}"]
    if conclusion is not None:
        fields.append(f"conclusion={str(conclusion).strip()}")
    if callback is not None:
        fields.append(f"callback={str(callback).strip()}")
    for key, value in (
        ("commit", commit_bearing),
        ("integrated", integration_recorded),
        ("open", issue_open),
        ("blocker", blocker_recorded),
    ):
        if value is not None:
            fields.append(f"{key}={'1' if value else '0'}")
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{':'.join(fields)}]"


def render_gate_note(gate: str, *, body: str = "", **marker_fields: object) -> str:
    """Render a **canonical gate-record note**: prose body + the embedded gate marker (pure).

    The single canonical renderer for a callback-required gate journal (#13520 review F1a): a gate
    recorded through this path always carries the structured
    :func:`render_workflow_event_marker` token, so the callback watcher can **discover** it
    (:func:`markers_from_source`) instead of relying on a hand-written fixture marker or prose. The
    marker is appended after the human-readable ``body`` (blank body -> just the marker). ``gate``
    must be a callback-required kind (:data:`GATE_BEARING_KINDS`); ``marker_fields`` are forwarded
    to :func:`render_workflow_event_marker` (conclusion / callback / commit_bearing / …). Pure;
    the caller (the application writer) posts the returned text as a Redmine journal note.
    """
    marker = render_workflow_event_marker(gate, **marker_fields)  # type: ignore[arg-type]
    body_s = str(body or "").rstrip()
    return f"{body_s}\n\n{marker}" if body_s else marker


__all__ = (
    "MARKER_CHANNEL_HANDOFF",
    "MARKER_CHANNEL_WORKFLOW_EVENT",
    "GATE_BEARING_KINDS",
    "RedmineJournalEntry",
    "marker_fields_in_note",
    "extract_markers_from_note",
    "extract_marker",
    "extract_markers",
    "RedmineJournalSource",
    "MappingRedmineJournalSource",
    "markers_from_source",
    "render_workflow_event_marker",
    "render_gate_note",
    "DISPATCH_KIND_IMPLEMENTATION_REQUEST",
    "render_dispatch_marker",
    "render_dispatch_note",
    "dispatch_entry_journals",
    "dispatch_generations",
    "resolve_dispatch_entry_journal",
    "dispatch_entry_journal_from_source",
)
