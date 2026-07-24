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
from .hibernate_evidence_envelope import EnvelopeParseError, parse_lane_envelope
from .redmine_journal_source import marker_fields_in_note

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
    for _channel, fields in marker_fields_in_note(notes or ""):
        gate = str(fields.get("gate", "") or "").strip()
        if not gate:
            continue
        role = contract_writer_role(gate)
        if role == ISSUER_UNKNOWN:
            continue
        gates.setdefault(gate, []).append(fields)

    if len(gates) != 1:
        # Zero authority-bearing gates -> unknown; two DIFFERENT authority gates in one note
        # claim two contracts at once and prove neither (the marker-conflict rule's shape).
        return ResolvedIssuer()
    (gate, marker_list), = gates.items()
    role = contract_writer_role(gate)

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
