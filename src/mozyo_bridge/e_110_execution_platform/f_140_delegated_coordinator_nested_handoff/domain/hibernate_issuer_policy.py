"""Issuer POLICY binding for hibernate evidence (Redmine #14219 T2c Fork A, ruling j#86718).

In a single-Redmine-author workspace every role posts through the same account, so a journal's
writer role cannot be resolved from author identity. The ruling adopts the one thing the
workspace CAN durably express: the **canonical gate structure -> contractual writer role**
mapping the producer ruling itself defines (j#85530 Q3), bound to the committed role/provider
configuration by exact git blob.

**This is a policy binding, NOT identity authentication** (the ruling's own words). It answers
"which role is contracted to write this gate kind", never "who actually typed it": a forged
record with the right structure resolves to the same role as a genuine one, and the layered
defenses that actually reject forgeries — the exact lane-envelope match, the request
correlation, the head binding, the corroborating receipts — stay mandatory and untouched.
Resolution therefore deliberately takes NO author metadata at all: with one shared account any
author-derived confidence would be theater, and pretending otherwise is the failure mode the
T2b reviews spent thirteen rounds burning out.

Fail-closed edges: a note with no authority-bearing gate resolves to the unknown issuer (the
producer's unresolved refusal); a note claiming TWO different authority gates proves neither
(conflict); a lane-scoped gate whose lane envelope is missing / malformed / self-conflicting
resolves unbound (the producer's lane check then refuses it).
"""

from __future__ import annotations

from typing import Optional

from .hibernate_evidence_authority import (
    ISSUER_UNKNOWN,
    ResolvedIssuer,
    contract_writer_role,
)
from .glance_integration_disposition import canonical_marker_value
from .hibernate_evidence_envelope import EnvelopeParseError, parse_lane_envelope
from .redmine_journal_source import (
    MARKER_CHANNEL_WORKFLOW_EVENT,
    marker_components_in_note,
    marker_logical_gates,
    strict_marker_fields,
)

#: The ruling that defines the gate->writer-role contract this policy binds to.
POLICY_RULING_POINTER = "redmine:#14219:j#85530:Q3"

#: The committed configuration file whose exact blob the binding is anchored to.
CONFIG_RELPATH = ".mozyo-bridge/config.yaml"

#: The roles whose authority is scoped to a lane, so their binding needs the evidence's own
#: exact envelope (the workspace-scoped coordinator binds without one).
_LANE_SCOPED = frozenset({"review_gateway", "lane_worker"})


def config_policy_pointer(blob_sha: str) -> str:
    """The ``git:<relpath>@<blob>`` component of the authority anchor (pure)."""
    return f"git:{CONFIG_RELPATH}@{blob_sha}"


def resolve_journal_issuer(
    journal_id: str, notes: str, *, policy_pointer: str
) -> ResolvedIssuer:
    """Resolve one journal's writer role from its canonical gate structure (pure).

    ``policy_pointer`` is the committed-config component (:func:`config_policy_pointer`) the
    wiring computed from the repository; an empty pointer means the policy basis itself could
    not be established, and EVERY resolution is then unknown/unanchored (fail-closed) — a
    binding that cannot name its own basis record binds nothing.

    No author parameter exists on purpose: the resolution is policy, not authentication (see
    the module docstring), and the tests pin that the same structure resolves identically
    regardless of who posted it.
    """
    if not str(policy_pointer or "").strip():
        return ResolvedIssuer()

    gates: dict[str, list[dict]] = {}
    for channel, components in marker_components_in_note(notes or ""):
        # ONLY the workflow-event channel is authority. The handoff channel is a delivery
        # NOTIFICATION (the same F5 boundary the glance grammar holds) and it carries a ``kind``
        # field, so once ``kind`` became an authority alias a delivery record sitting in the same
        # journal as an evidence marker would have read as a second gate claim and unresolved a
        # perfectly good issuer. Restricting the channel is what makes the alias union safe.
        if channel != MARKER_CHANNEL_WORKFLOW_EVENT:
            continue
        # The SHARED strict reader, over UNCOLLAPSED components (Redmine #14539 review j#91896
        # finding 2). Reading the folded dict lost two things at once: a repeated ``gate`` key was
        # erased by last-write-wins, and surrounding whitespace was normalized away. Either let a
        # marker the canonical producer could not render resolve to a clean coordinator issuer.
        # The SAME canonicalizer every other authority consumer passes: two spellings of one
        # governed token are one declaration, so a canonically-equal duplicate does not make the
        # body ambiguous. Without this the three consumers would disagree about the same marker.
        fields = strict_marker_fields(components, canonicalize=canonical_marker_value)
        if fields is None:
            # A marker whose body is not renderable declares nothing — and "nothing" must not mean
            # "skip it and read the next one", so a note carrying one is left unresolved below
            # unless some OTHER marker establishes exactly one gate. That is the same fail-closed
            # shape as the conflict case: it never promotes, only withholds.
            return ResolvedIssuer()
        declared = marker_logical_gates(fields)
        for gate in declared:
            # An UNRECOGNIZED gate token still counts as a claim (review j#91896 finding 2):
            # skipping it let ``gate=integration_disposition:kind=unknown_gate`` resolve as if only
            # one contract had been named. Its role is unknown, so the note ends up with two gates
            # and proves neither.
            gates.setdefault(gate, []).append(fields)

    if len(gates) != 1:
        # Zero authority-bearing gates -> unknown; two DIFFERENT authority gates in one note
        # claim two contracts at once and prove neither (the marker-conflict rule's shape).
        return ResolvedIssuer()
    (gate, marker_list), = gates.items()
    role = contract_writer_role(gate)
    if role == ISSUER_UNKNOWN:
        # The one gate the note names has no contractual writer, so nothing is resolved. Returning
        # an ANCHORED unknown would be a resolution-shaped value for an unresolved question.
        return ResolvedIssuer()

    anchor = (
        f"{POLICY_RULING_POINTER} {policy_pointer} "
        f"evidence:redmine:j#{str(journal_id).strip()}:gate={gate}"
    )

    if role not in _LANE_SCOPED:
        return ResolvedIssuer(role=role, authority_anchor=anchor)

    envelopes = []
    for fields in marker_list:
        bound = parse_lane_envelope(fields, require_head=False)
        if isinstance(bound, EnvelopeParseError):
            return ResolvedIssuer()
        envelopes.append(bound)
    distinct = {
        (env.workspace, env.lane, env.lane_generation) for env in envelopes
    }
    if len(distinct) != 1:
        return ResolvedIssuer()
    workspace, lane, generation = distinct.pop()
    return ResolvedIssuer(
        role=role,
        workspace=workspace,
        lane=lane,
        lane_generation=generation,
        authority_anchor=f"{anchor}:workspace={workspace}:lane={lane}:lane_generation={generation}",
    )


__all__ = [
    "CONFIG_RELPATH",
    "POLICY_RULING_POINTER",
    "config_policy_pointer",
    "resolve_journal_issuer",
]
