"""The ``ruling_issuers`` port #14971's legacy finding authority leaves for its consumer (#14755).

#14971 fixed the append-only migration contract (Design Ruling j#99084) and left exactly one thing
to whoever consumes it: :func:`...review_finding_legacy_authority.resolve_legacy_review_findings`
takes a ``ruling_issuers`` mapping and refuses every ruling it has no anchored coordinator for. The
port is unfilled there on purpose — "which durable record binds a writer to a role is the PORT's
decision" (:mod:`.hibernate_evidence_authority`), not the grammar's — and #14755 is the first
production consumer, so filling it is this route's obligation. It is a PORT, not a new contract:
the gate token, the writer role and the ruling pointer are all read from #14971's own exports.

**Where the role comes from, and why not out of the ruling marker.** j#99084 fixes it: "role は
marker 自己申告ではなく durable authority anchor から解決する". The ruling marker already carries
``approval_source=direct_owner``; reading a role out of that same marker as well would make the
record its own authority — the defect #14755 review j#99065 measured one level down, where an
enumeration's only corroborating record was the one that wrote it. So this uses the repo's existing
issuer POLICY shape (:func:`...hibernate_issuer_policy.resolve_journal_issuer`, #14661 j#92601 F1):
a journal declares a GATE, and the contract — source the journal did not write — says whose gate
that is. #14971 exports ``GATE_REVIEW_FINDING_LEGACY_RULING`` / :func:`legacy_ruling_writer_role` /
:func:`legacy_ruling_pointer` as a triple and names no other resolution; a gate token nothing reads
would otherwise be a contract with no consumer.

**Stated rather than implied: what this does NOT establish.** Not who wrote the journal. Nothing in
this workspace can — every role posts under one source-system account (ruling #14219 j#86718), and
comparing author ids is precisely the shape #14661 j#92601 F1 refused. What it establishes is
narrower and worth naming exactly: a ruling journal must ALSO declare, in its canonical gate
structure, the one gate whose writer contract j#99084 fixed, and must declare NOTHING else — so a
ruling cannot be carried inside a journal written to be something else, and a note claiming two
authority contracts at once proves neither. The authority this route ultimately rests on is the
direct-owner ruling itself; this resolution only decides which records are eligible to be read as
one. :mod:`.superseded_failure_terminal` states that in one place for the whole route.

Deliberately NOT registered in :mod:`.hibernate_evidence_authority`'s gate map. #14971 kept this
authority inside the review-finding migration context so an unrelated recovery runbook does not
become the accidental catalog for every future durable authority, and a port that honours that
boundary must live beside the contract it serves rather than inside the map it was kept out of.

Boundary: pure. A total function over journal entries; no IO, no Redmine, no git.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .canonical_note_scan import heading_gate_declarations
from .glance_integration_disposition import canonical_marker_value
from .hibernate_evidence_authority import ResolvedIssuer
from .redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    RedmineJournalEntry,
    marker_components_in_note,
    marker_logical_gates,
    strict_marker_fields,
)
from .review_finding_legacy_authority import (
    GATE_REVIEW_FINDING_LEGACY_RULING,
    legacy_ruling_pointer,
    legacy_ruling_writer_role,
)


def _declared_gates(notes: str) -> "frozenset[str] | None":
    """Every gate one note declares on EITHER surface, or ``None`` if a marker is unreadable (pure).

    BOTH surfaces, and the heading is the load-bearing one here. ``review_finding_legacy_ruling``
    is not a gate-bearing kind, so :func:`...redmine_journal_source.render_workflow_event_marker`
    cannot render a marker for it and no producer in this repo can — the canonical surface a
    governed author writes it on is the ``## Gate:`` heading. That is not an exception invented for
    this gate: ``review_finding_verdict`` is in exactly the same position, and
    :func:`...superseded_failure_correlation.fold_finding_verdicts` already qualifies it by heading
    for the same reason (#14695 j#93576 F1). Requiring a marker instead would be a contract with no
    producer, which is what #14755 review j#99065 faulted one level down.

    Markers are still read — to ADD claims, never to satisfy one. A note that carries the ruling
    heading and also a ``workflow-event`` marker for some other gate is claiming two contracts at
    once and proves neither (ruling #14219 j#86718). ``None`` — a marker the canonical producer
    could not render — resolves NOTHING rather than being skipped so a clean sibling decides, the
    same fail-closed reading :func:`...hibernate_issuer_policy.resolve_journal_issuer` applies. The
    strict reader is used over UNCOLLAPSED components because the dict fold is last-write-wins, so
    a body repeating ``gate=`` reaches a consumer looking well-formed while its meaning depends on
    which occurrence came last (#14539 review j#91797 finding 4).

    Only the ``workflow-event`` channel is authority. The handoff channel is a delivery
    NOTIFICATION and carries a ``kind`` field, which is a gate alias — so a delivery record sitting
    in the same journal as a ruling would otherwise read as a second gate claim.
    """
    gates: set[str] = set(heading_gate_declarations(notes or ""))
    for channel, components in marker_components_in_note(notes or ""):
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        fields = strict_marker_fields(components, canonicalize=canonical_marker_value)
        if fields is None:
            return None
        gates.update(marker_logical_gates(fields))
    return frozenset(gates)


def resolve_legacy_ruling_issuers(
    entries: Sequence[RedmineJournalEntry],
) -> "Mapping[str, ResolvedIssuer]":
    """The ``ruling_issuers`` mapping #14971's legacy resolver consumes (pure).

    A journal is eligible when its canonical gate structure declares EXACTLY the one gate #14971
    fixed a writer contract for — no second gate on either surface, and no marker the canonical
    producer could not render. Every other journal is simply absent from the mapping, which
    :func:`...review_finding_legacy_authority.resolve_legacy_review_findings` reads as an
    unresolved issuer and refuses — the fail-closed direction. Absence is the only outcome besides
    resolution: this never returns a partially-filled issuer, because a partially-filled issuer is
    not a partially-satisfied authority (:mod:`.hibernate_evidence_authority`).

    The issuer carries no lane identity because the role is the workspace-level coordinator, whose
    authority is not lane-scoped. The anchor is #14971's ruling pointer verbatim, which is what
    that module's exact-equality check requires; producing any other string here — including the
    richer composite :func:`...hibernate_issuer_policy.resolve_journal_issuer` builds — would leave
    every ruling unauthorized.
    """
    resolved: "dict[str, ResolvedIssuer]" = {}
    for entry in entries or ():
        journal = str(getattr(entry, "journal_id", "") or "").strip()
        if not journal:
            continue
        gates = _declared_gates(str(getattr(entry, "notes", "") or ""))
        if gates != frozenset({GATE_REVIEW_FINDING_LEGACY_RULING}):
            continue
        resolved[journal] = ResolvedIssuer(
            role=legacy_ruling_writer_role(),
            authority_anchor=legacy_ruling_pointer(),
        )
    return resolved


__all__ = ("resolve_legacy_ruling_issuers",)
