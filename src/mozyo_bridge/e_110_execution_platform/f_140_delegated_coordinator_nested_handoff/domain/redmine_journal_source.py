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
``redmine_context`` machinery + a since/updated_on cursor) drops in later. That live poll
loop is an operational, credential-gated layer carried as an explicit follow-up; the read /
extract / convert boundary it would feed is implemented and tested here.
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

#: The gate-bearing kinds a marker may name (mirrors the adapter's core-owned
#: ``WORKFLOW_GATE_KINDS``; kept local so this domain stays inside its bounded context and
#: does not import the e_140 adapter). A non-gate kind (``implementation_request`` /
#: ``design_consultation`` / ``reply`` / ``start`` / ``close`` …) is skipped, never guessed.
GATE_BEARING_KINDS: frozenset[str] = frozenset(
    {"implementation_done", "review_request", "review_result"}
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


__all__ = (
    "MARKER_CHANNEL_HANDOFF",
    "MARKER_CHANNEL_WORKFLOW_EVENT",
    "GATE_BEARING_KINDS",
    "RedmineJournalEntry",
    "extract_markers_from_note",
    "extract_marker",
    "extract_markers",
    "RedmineJournalSource",
    "MappingRedmineJournalSource",
    "markers_from_source",
)
