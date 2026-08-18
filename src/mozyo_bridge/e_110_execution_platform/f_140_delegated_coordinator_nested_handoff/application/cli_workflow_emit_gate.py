"""The ``workflow callbacks --emit-gate`` / ``--emit-progress`` actions (Redmine #15699 split).

The canonical gate / progress **producer actions** of ``workflow callbacks``, moved out of
:mod:`.cli_workflow_callbacks` verbatim when the #15699 ``--render-note-only`` addition pushed
that module over the module-health line cap. Same feature ownership, same behavior; the parent
command passes its own collaborators (``_emit``, the marker-field builder, the approval write
fence, the supervisor wake) at call time so the established test seams on the parent module keep
working unchanged.

Boundaries are unchanged from the parent module:

- **opt-in / fail-closed writes.** A write lands only through the credential-gated
  ``MOZYO_REDMINE_DELIVERY_WRITE`` transport; opt-in unset is a recorded, non-zero-exit refusal.
- **producer refusals are typed data**, never tracebacks; a not-recorded gate never exits 0.
- **#15699 render-only posts nothing itself, but refuses like the writer.** ``--render-note-only``
  runs the FULL write-path refusal chain — producer grammar and, for an approval, the
  generation-admission fence (observation + consumer, identity exact-match, single-consumer
  lease + reread) — then prints the canonical note for hand-posting and emits no supervisor wake
  (#15699 review j#107904).
"""

from __future__ import annotations

import argparse
from typing import Callable, Optional


def run_emit_gate(
    args: argparse.Namespace,
    *,
    as_json: bool,
    emit: Callable[..., int],
    review_gate_marker_fields: Callable[[argparse.Namespace, str], "tuple[dict, Optional[str]]"],
    review_approval_refusal: Callable[..., Optional[str]],
    supervisor_wake: Callable[[argparse.Namespace, str], None],
) -> int:
    """Run ``--emit-gate``: validate, then write the canonical gate note (or render it, #15699)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (  # noqa: E501
        attempt_emit_gate_record,
        attempt_render_gate_record_note,
        review_findings_json_input,
    )
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (  # noqa: E501
        redmine_delivery_transport_from_env,
    )

    issue = (getattr(args, "issue", None) or "").strip()
    gate = (getattr(args, "gate", None) or "").strip()
    if not issue or not gate:
        raise SystemExit("--emit-gate requires --issue and --gate")
    # #13518 R3-F2: approval uses the durable generation lease + pre-write reread fence; a
    # duplicate consumer or stale observation is a zero-write, non-zero-exit refusal.
    # #13974 j#81487 F2: a review_request / review_result gate MUST carry the v2 marker fields
    # (exact full target_head; review_result also its answered review_request journal + conclusion).
    # The producer refuses malformed fields; approval also exact-matches the admission identity.
    marker_fields, marker_refusal = review_gate_marker_fields(args, gate)
    review_findings, finding_refusal = (
        (None, None)
        if marker_refusal is not None
        else review_findings_json_input(
            getattr(args, "review_findings_json", None), gate, marker_fields)
    )
    # Redmine #15699: --render-note-only is the render half alone, for a write-incapable
    # environment (opt-in unset) whose reviewer must hand-post the canonical note — the #14971
    # manifest sidecar is not hand-computable. The refusal chain is IDENTICAL to the write path,
    # including the approval generation-admission fence (#15699 review j#107904
    # finding_approvalfencebypass): the role profile sanctions pasting this output as the approval
    # journal, so the render step is the only mechanical place admission can be enforced for that
    # flow — an approval note is rendered only after the observation/consumer inputs, the
    # observation↔marker identity exact-match, and the single-consumer generation lease + reread
    # fence all admit. The rendering consumer holds the lease; their hand-post is that consumer's
    # write. Explicit non-approval decisions stay unfenced exactly as on the write path.
    render_note_only = bool(getattr(args, "render_note_only", False))
    refusal = (
        marker_refusal
        or finding_refusal
        or review_approval_refusal(args, issue, gate, marker_fields)
    )
    if refusal is not None:
        payload = {"action": "emit-gate", "issue": issue, "gate": gate,
                   "recorded": False, "reason": refusal}
        emit(payload, as_json=as_json, text_lines=[
            "action: emit-gate", f"issue: #{issue}", f"gate: {gate}",
            "recorded: False", f"reason: {refusal}",
        ])
        return 1
    if render_note_only:
        note, render_refusal = attempt_render_gate_record_note(
            issue, gate, body=(getattr(args, "body", None) or ""),
            marker_fields=marker_fields, review_findings=review_findings)
        if note is None:
            payload = {"action": "emit-gate", "issue": issue, "gate": gate,
                       "recorded": False, "reason": render_refusal}
            emit(payload, as_json=as_json, text_lines=[
                "action: emit-gate", f"issue: #{issue}", f"gate: {gate}",
                "recorded: False", f"reason: {render_refusal}",
            ])
            return 1
        # recorded is ALWAYS False here — rendering is not writing, and no supervisor wake is
        # emitted. Text mode prints the note alone so it can be pasted / piped verbatim.
        payload = {"action": "emit-gate", "issue": issue, "gate": gate,
                   "recorded": False, "rendered": True, "note": note}
        return emit(payload, as_json=as_json, text_lines=[note])
    # Credential-gated, opt-in production writer (MOZYO_REDMINE_DELIVERY_WRITE). None ->
    # write_optin_unset (nothing written, fail-closed — never a silent success).
    transport = redmine_delivery_transport_from_env()
    attempt = attempt_emit_gate_record(
        issue, gate, body=(getattr(args, "body", None) or ""), transport=transport,
        marker_fields=marker_fields, review_findings=review_findings)
    if attempt.refusal:
        emit(attempt.refusal_payload(issue, gate), as_json=as_json,
             text_lines=attempt.refusal_lines(issue, gate))
        return 1
    receipt = attempt.receipt
    assert receipt is not None
    payload = {"action": "emit-gate", "issue": issue, "gate": gate, **receipt.as_payload()}
    lines = [
        "action: emit-gate",
        f"issue: #{issue}",
        f"gate: {gate}",
        f"recorded: {receipt.recorded}",
        f"reason: {receipt.reason}",
    ]
    if receipt.location:
        lines.append(f"location: {receipt.location}")
    emit(payload, as_json=as_json, text_lines=lines)
    # #13683 review R1-F2: the canonical gate writer is the PRIMARY supervisor trigger — after a
    # gate is RECORDED, emit a best-effort local wake for (workspace, issue) so the workspace
    # callback supervisor re-reads that issue without waiting for the reconciliation interval.
    # Best-effort: a wake-store failure never fails the (already-recorded) gate; a lost wake is
    # recovered by the supervisor's bounded reconciliation.
    if receipt.recorded:
        supervisor_wake(args, issue)
    # #13520 review R2-F1: fail-closed at the PROCESS gate too — a not-recorded gate (opt-in
    # unset / transport failure) must NOT exit 0, so a caller that reads only the return code
    # can never treat an un-written gate as recorded. The structured receipt still prints above.
    return 0 if receipt.recorded else 1


def run_emit_progress(
    args: argparse.Namespace,
    *,
    as_json: bool,
    emit: Callable[..., int],
) -> int:
    """Run ``--emit-progress``: record a round-scoped worker progress marker (#13889 review F2)."""
    # #13889 review F2: the producer half of the sweep watermark. A worker-side progress gate
    # (review_finding_verdict / progress_log / start / design_consultation) recorded through
    # this path is marker-bearing, so the sweep can classify it structurally instead of
    # abstaining from every stall verdict on the issue. Same opt-in / fail-closed contract as
    # --emit-gate; the marker is round-scoped so it cannot be read as another round's progress.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.callback_gate_record import (  # noqa: E501
        emit_progress_record,
    )
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (  # noqa: E501
        redmine_delivery_transport_from_env,
    )

    issue = (getattr(args, "issue", None) or "").strip()
    kind = (getattr(args, "progress_kind", None) or "").strip()
    lane = (getattr(args, "lane", None) or "").strip()
    generation = (getattr(args, "lane_generation", None) or "").strip()
    if not (issue and kind and lane and generation):
        raise SystemExit(
            "--emit-progress requires --issue, --progress-kind, --lane and --lane-generation "
            "(an unscoped progress marker cannot be attributed to a dispatch round)"
        )
    transport = redmine_delivery_transport_from_env()
    try:
        receipt = emit_progress_record(
            issue, kind, lane=lane, lane_generation=generation,
            body=(getattr(args, "body", None) or ""), transport=transport,
        )
    except ValueError as exc:  # an out-of-vocabulary kind is a caller error, surfaced
        raise SystemExit(str(exc)) from exc
    payload = {"action": "emit-progress", "issue": issue, "kind": kind, "lane": lane,
               "lane_generation": generation, **receipt.as_payload()}
    lines = [
        "action: emit-progress",
        f"issue: #{issue}",
        f"kind: {kind}",
        f"lane: {lane} generation: {generation}",
        f"recorded: {receipt.recorded}",
        f"reason: {receipt.reason}",
    ]
    if receipt.location:
        lines.append(f"location: {receipt.location}")
    emit(payload, as_json=as_json, text_lines=lines)
    # A progress gate owes no coordinator callback (that is the whole point of the separate
    # vocabulary), so unlike --emit-gate this deliberately emits NO supervisor wake.
    return 0 if receipt.recorded else 1


__all__ = ("run_emit_gate", "run_emit_progress")
