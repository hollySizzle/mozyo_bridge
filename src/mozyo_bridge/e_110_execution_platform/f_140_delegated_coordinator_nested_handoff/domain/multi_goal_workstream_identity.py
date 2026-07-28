"""The two identity digests of the multi-goal workstream dispatch plan (#14636).

Separate from :mod:`.multi_goal_workstream_plan` because they are a separate consumer
surface: #14637 builds delegated-coordinator create-or-adopt idempotency on
:func:`workstream_identity_digest`, and a restart compares
:func:`plan_content_digest` before it re-dispatches anything.

The two answer deliberately different questions. The **workstream** digest covers identity
only (schema, project identity, member goals), so a workstream that is serialized today and
dispatched tomorrow keeps one key and cannot be created twice. The **plan** digest covers
identity *and* every disposition, so "the plan changed" is detectable.

Both use a length-prefixed canonical encoding under a domain tag (the
:mod:`.callback_recovery_key` idiom). Plain delimiter joining is not injective — two
different field tuples can join to one string — and two requests that digested identically
would have the second one silently suppressed as a replay.
"""

from __future__ import annotations

import hashlib
from typing import Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.multi_goal_workstream_records import (
    PLAN_SCHEMA_VERSION,
    IntakeDefect,
    PlannedWorkstream,
    RejectedGoal,
    normalized_sequence,
)


#: Domain separation for the two digest spaces. A digest minted here cannot collide with one
#: minted by a sibling authority, whatever the field values, and a workstream digest cannot
#: collide with a plan digest.
_PLAN_DIGEST_DOMAIN = "mozyo.multi_goal_workstream_plan"
_WORKSTREAM_DIGEST_DOMAIN = "mozyo.multi_goal_workstream_identity"


# ---------------------------------------------------------------------------
# Canonical encoding / digest (pure).
# ---------------------------------------------------------------------------


def _encode_field(name: str, value: str) -> str:
    """One length-prefixed canonical field.

    ``name=<len>:<value>`` is injective for *any* value; plain ``name=value`` joining is not
    once the same name repeats, which is exactly what :func:`_encode_seq` does per item. Two
    goals ``"a"`` and ``"b"`` join to ``i=ai=b``, and so does one goal literally named
    ``"ai=b"`` — so the second request would share the first one's digest and be silently
    suppressed as a replay. The length prefix closes that, whatever the value contains.
    """
    return f"{name}={len(value)}:{value}"


def _encode_seq(name: str, values: Sequence[str]) -> str:
    """A length-prefixed canonical sequence (each item itself length-prefixed)."""
    body = "".join(_encode_field("i", value) for value in values)
    return _encode_field(name, body)


def _digest(domain: str, parts: Sequence[str]) -> str:
    """sha256 over the domain-tagged canonical encoding (pure)."""
    canonical = _encode_field("domain", domain) + "".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def workstream_identity_digest(
    workstream_key: str, goal_ids: Sequence[str], *, schema_version: int = PLAN_SCHEMA_VERSION
) -> str:
    """The stable identity key of one workstream (pure, #14637 idempotency foundation).

    Covers the schema version, the project identity and the member goals — and nothing that
    can change while the work stays the same. A workstream that is serialised today and
    dispatched tomorrow keeps one key, so create-or-adopt cannot produce a second coordinator
    for it on replay.
    """
    return _digest(
        _WORKSTREAM_DIGEST_DOMAIN,
        [
            _encode_field("schema_version", str(schema_version)),
            _encode_field("workstream_key", workstream_key),
            _encode_seq("goal_ids", normalized_sequence(goal_ids)),
        ],
    )


def plan_content_digest(
    workstreams: Sequence[PlannedWorkstream],
    rejected: Sequence[RejectedGoal],
    plan_defects: Sequence[IntakeDefect],
    collapsed: Sequence[str],
) -> str:
    """The digest of the whole plan: identities **and** dispositions (pure).

    Distinct from :func:`workstream_identity_digest` on purpose — this one changes when the
    plan's *answer* changes, which is what a restart compares before re-dispatching.
    """
    parts = [_encode_field("schema_version", str(PLAN_SCHEMA_VERSION))]
    for workstream in workstreams:
        relation_items = [
            f"{relation.relation}|{relation.peer}|{','.join(relation.shared)}"
            for relation in workstream.relations
        ]
        defect_items = [
            f"{defect.reason}|{defect.subject}" for defect in workstream.intake_defects
        ]
        parts.append(
            _encode_field(
                "workstream",
                "".join(
                    [
                        _encode_field("digest", workstream.workstream_digest),
                        _encode_field("key", workstream.workstream_key),
                        _encode_seq("goals", workstream.goal_ids),
                        _encode_field("disposition", workstream.disposition),
                        _encode_field("admission", workstream.admission_decision),
                        _encode_field("reuse_target", workstream.reuse_target),
                        _encode_seq("relations", relation_items),
                        _encode_seq("risks", workstream.risk_reasons),
                        _encode_seq("nonreasons", workstream.rejected_nonreasons),
                        _encode_seq("defects", defect_items),
                        _encode_seq("blocked_by", workstream.blocked_by),
                    ]
                ),
            )
        )
    parts.append(
        _encode_seq(
            "rejected",
            [f"{goal.goal_id}|{goal.project_identity}|{goal.reason}" for goal in rejected],
        )
    )
    parts.append(
        _encode_seq(
            "plan_defects", [f"{defect.reason}|{defect.subject}" for defect in plan_defects]
        )
    )
    parts.append(_encode_seq("collapsed", collapsed))
    return _digest(_PLAN_DIGEST_DOMAIN, parts)


__all__ = (
    "workstream_identity_digest",
    "plan_content_digest",
)
