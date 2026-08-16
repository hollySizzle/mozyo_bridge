"""The PRODUCER for an audit-failure terminal declaration (Redmine #15166).

Split out of :mod:`.superseded_audit_failure_terminal` when the j#102184 ruling's typed-refusal
wiring pushed that module past the oversized-module gate. The gate's remedy is to reduce, and a
renderer is the natural seam: the terminal module READS declarations and decides, this one WRITES
the single marker a coordinator records. Pure move — the field order, the producer errors and the
emitted body are byte-identical, and the terminal module re-exports the name, so every import site
and every existing test is unchanged.

Why a renderer exists at all: an authority contract nobody can produce is an authority contract
nobody will use, and a renderer that emits what its own parser refuses produces durable records
that read back as a typed zero. Field order is
:data:`...superseded_audit_failure_terminal.SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER`, so what this
emits is what the strict reader accepts, by construction.

Boundary: pure. No IO, no Redmine, no git.
"""

from __future__ import annotations

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_envelope import (  # noqa: E501
    LaneEvidenceEnvelope,
    reject_marker_separator,
    render_lane_envelope,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
    MARKER_CHANNEL_WORKFLOW_EVENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_failure_correlation import (  # noqa: E501
    journal_ref,
)


def render_superseded_audit_failure_marker(
    *,
    issue: str,
    audit_journal: object,
    successor_issue: str,
    successor_review_journal: object,
    integration_branch: str,
    workspace: str,
    lane: str,
    lane_generation: object,
    head: str,
) -> str:
    """The exact marker a valid audit-failure terminal declaration must carry (pure).

    Field order is :data:`SUPERSEDED_AUDIT_FAILURE_FIELD_ORDER`, so what this emits is what the
    strict reader accepts, by construction.

    Every producer error raises ``ValueError`` rather than being written. A renderer that accepts
    what its own parser refuses is not a strict grammar: it produces durable records that read back
    as a typed zero, so the authority silently does not count. The envelope's own
    :func:`...hibernate_evidence_envelope.render_lane_envelope` enforces the workspace / lane /
    generation / head rules and the marker-separator rejection; this adds the identities.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.superseded_audit_failure_terminal import (  # noqa: E501
        SUPERSEDED_AUDIT_FAILURE_DECISION,
        SUPERSEDED_AUDIT_FAILURE_GATE,
        SUPERSEDED_AUDIT_FAILURE_VERSION,
    )

    issue_s = str(issue or "").strip()
    successor_s = str(successor_issue or "").strip()
    branch_s = str(integration_branch or "").strip()
    if not issue_s or not successor_s:
        raise ValueError(
            "an audit-failure terminal requires a non-empty issue and successor_issue"
        )
    if issue_s == successor_s:
        raise ValueError("an issue cannot supersede its own independent-audit failure")
    if not branch_s:
        raise ValueError("an audit-failure terminal requires the integration branch")
    for value, field in (
        (issue_s, "issue"),
        (successor_s, "successor_issue"),
        (branch_s, "integration_branch"),
    ):
        reject_marker_separator(value, field=field)

    supplied = {
        "audit_journal": audit_journal,
        "successor_review_journal": successor_review_journal,
    }
    references = {field: journal_ref(raw) for field, raw in supplied.items()}
    for field, value in references.items():
        if not value:
            raise ValueError(
                f"an audit-failure terminal requires a decimal {field}, got {supplied[field]!r}"
            )

    if isinstance(lane_generation, bool):
        raise ValueError("an audit-failure terminal requires an integer lane_generation")
    try:
        generation = int(lane_generation)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(
            "an audit-failure terminal requires an integer lane_generation, "
            f"got {lane_generation!r}"
        ) from None
    if not str(head or "").strip():
        raise ValueError(
            "an audit-failure terminal requires the lane head it was recorded at"
        )
    envelope_body = render_lane_envelope(
        LaneEvidenceEnvelope(
            workspace=str(workspace or "").strip(),
            lane=str(lane or "").strip(),
            lane_generation=generation,
            head=str(head or "").strip(),
        )
    )
    body = ":".join(
        [
            f"gate={SUPERSEDED_AUDIT_FAILURE_GATE}",
            f"version={SUPERSEDED_AUDIT_FAILURE_VERSION}",
            f"decision={SUPERSEDED_AUDIT_FAILURE_DECISION}",
            f"issue={issue_s}",
            f"audit_journal={references['audit_journal']}",
            f"successor_issue={successor_s}",
            f"successor_review_journal={references['successor_review_journal']}",
            f"integration_branch={branch_s}",
            envelope_body,
        ]
    )
    return f"[mozyo:{MARKER_CHANNEL_WORKFLOW_EVENT}:{body}]"



__all__ = ("render_superseded_audit_failure_marker",)
