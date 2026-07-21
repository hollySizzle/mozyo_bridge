"""Public read-only quarantine inspection surface (Redmine #14234).

``sublane quarantine --execute`` demands five exact tokens — ``--assigned-name``,
``--locator``, ``--action-generation``, ``--approved-revision``, ``--approval-observed-at`` —
that no public read-only surface returned. The quarantine preflight observed every one of them
internally but collapsed them into a single classification label on the way out, and
``sublane list`` returns the lane locator without the assigned name, agent revision or attested
generation. An operator could therefore only assemble a positive generation-bound approval from
raw Herdr, the internal Python API, the pane body, or a guess. The #14163 six-lane drain
actually stalled on this.

``mozyo-bridge sublane quarantine-inspect --issue <id> --lane <lane> --role <role>`` closes the
loop: it discovers the exact receiver from the managed inventory, reports the tokens, and — only
when the observation can support one — renders the pasteable owner-approval record plus the
exact ``--execute`` command line.

Design notes:

- **One read seam, one snapshot.** The classification is NOT re-implemented. The inventory is
  read once and fed to :class:`...sublane_prepare_readonly_projection.SnapshotQuarantineOps`,
  the existing subclass that runs the #13763 quarantine inspector against an already-read
  inventory. Discovery and classification therefore observe the SAME rows, so the reported
  revision cannot drift from the classified one between two reads. This is also why the command
  does not duplicate ``sublane list``: that surface answers "what lanes exist", this one answers
  "what exact generation would an approval bind", through the quarantine inspector itself.
- **Discovery, not derivation.** The assigned name is resolved by decoding the live rows'
  identities and matching ``(workspace_id, lane, role)`` — it is read from what is actually
  running rather than re-minted from the naming scheme, so a recycled or foreign name cannot be
  silently approved. Zero or several matches are typed refusals, never a pick.
- **Read-only.** No store write, no close, no launch, no Herdr mutation. The command is a
  projection; ``--execute`` lives on ``sublane quarantine`` and is unchanged.
- **Value non-exposure.** Only identity / revision / generation tokens and a classification
  leave this module, enforced by the shape of :class:`...domain.quarantine_approval.ApprovalFacts`.
  The composer body never crosses ``observe_composer_text``, and no path / hash / length / raw
  ANSI / credential is emitted.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.core.state.lane_replacement_model import quarantine_action_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
    list_herdr_agent_rows,
    repo_scope_workspace_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    QuarantineInspection,
    QuarantineRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.quarantine_approval import (  # noqa: E501
    APPROVAL_DUPLICATE_RECEIVER,
    APPROVAL_INVENTORY_UNREADABLE,
    APPROVAL_READY,
    APPROVAL_RECEIVER_ABSENT,
    APPROVAL_WORKSPACE_UNRESOLVED,
    ApprovalFacts,
    approval_command,
    decide_approval_readiness,
    render_approval_template,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    INVENTORY_UNREADABLE,
    PendingComposerClassification,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
)

#: The synthetic request fields the inspection uses to drive the #13763 inspector. The inspector
#: needs a request shaped for the *approval* path; an inspection has no approval yet. These are
#: inert placeholders, never emitted and never compared: the action generation is recomputed from
#: the DISCOVERED locator below, and ``approved_revision`` is set to the observed revision so the
#: inspector reports the receiver's real generation state instead of a fabricated mismatch. The
#: same technique the composer-discard rail already uses to borrow this inspector.
_INSPECTION_JOURNAL = "inspection-only"
_INSPECTION_EPOCH = "1970-01-01T00:00:00+00:00"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class QuarantineInspectRequest:
    """What the operator must supply: the durable lane coordinates, nothing exact."""

    issue: str
    lane: str
    role: str


@dataclass(frozen=True)
class QuarantineInspectOutcome:
    """The public projection: exact tokens + typed approval readiness (+ template when ready)."""

    request: QuarantineInspectRequest
    facts: ApprovalFacts
    classification: PendingComposerClassification
    approval_reason: str
    receiver_present: Optional[bool] = None
    inspection_detail: str = ""
    approval_template: str = ""

    @property
    def approval_ready(self) -> bool:
        return self.approval_reason == APPROVAL_READY

    @property
    def is_blocked(self) -> bool:
        """A non-ready inspection exits non-zero so a script cannot read a refusal as success."""
        return not self.approval_ready

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "action": "quarantine-inspect",
            **self.facts.as_payload(),
            **self.classification.as_payload(),
            "receiver_present": self.receiver_present,
            "inspection_detail": self.inspection_detail,
            "approval_ready": self.approval_ready,
            "approval_reason": self.approval_reason,
            "is_blocked": self.is_blocked,
        }
        # The template and its argv are emitted ONLY for a ready inspection, so a refusal can
        # never be copy-pasted into an execute that the fence would reject.
        payload["approval_template"] = self.approval_template or None
        payload["approval_command"] = (
            list(approval_command(self.facts)) if self.approval_ready else None
        )
        return payload


def _decoded_identity_matches(
    row: Mapping[str, object], *, workspace_id: str, lane: str, role: str
) -> bool:
    """Does this live row's DECODED identity name the exact (workspace, lane, role) slot?"""
    decoded = decode_assigned_name(row.get(AGENT_KEY_NAME))
    if not decoded.ok or decoded.identity is None:
        return False
    identity = decoded.identity
    return (
        _norm(identity.workspace_id) == _norm(workspace_id)
        and _norm_lane(identity.lane_id) == _norm_lane(lane)
        and _norm(identity.role) == _norm(role)
    )


def _row_revision(row: Mapping[str, object]) -> int:
    """The row's integer revision, or ``-1`` when absent / non-integer (mirrors the inspector)."""
    raw = row.get("revision")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return -1


@dataclass
class SublaneQuarantineInspectUseCase:
    """Read-only: discover the exact receiver, classify it, decide approval readiness."""

    repo_root: Path
    #: Injected inventory reader (the live default reads the managed agent rows). A test supplies
    #: its own; nothing else in this use case touches Herdr.
    rows_reader: Any = None
    #: Injected quarantine-inspector factory: ``(rows) -> ops`` exposing ``inspect(request)``.
    #: Defaults to the existing snapshot subclass so the classification comes from the ONE
    #: #13763 read seam rather than a second implementation.
    ops_factory: Any = None
    env: Optional[Mapping[str, str]] = field(default=None)

    def _rows(self) -> Sequence[Mapping[str, object]]:
        if self.rows_reader is not None:
            return self.rows_reader()
        return list_herdr_agent_rows(self.env)

    def _ops(self, rows: Sequence[Mapping[str, object]]):
        if self.ops_factory is not None:
            return self.ops_factory(rows)
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_prepare_readonly_projection import (  # noqa: E501
            SnapshotQuarantineOps,
        )

        return SnapshotQuarantineOps(repo_root=self.repo_root, snapshot_rows=tuple(rows))

    def _refuse(
        self,
        request: QuarantineInspectRequest,
        reason: str,
        *,
        facts: ApprovalFacts,
        classification: Optional[PendingComposerClassification] = None,
        receiver_present: Optional[bool] = None,
        detail: str = "",
    ) -> QuarantineInspectOutcome:
        return QuarantineInspectOutcome(
            request=request,
            facts=facts,
            classification=classification
            or PendingComposerClassification(INVENTORY_UNREADABLE),
            approval_reason=reason,
            receiver_present=receiver_present,
            inspection_detail=detail,
        )

    def run(self, request: QuarantineInspectRequest) -> QuarantineInspectOutcome:
        observed_at = _utc_now()
        base = ApprovalFacts(
            issue=_norm(request.issue),
            lane=_norm_lane(request.lane),
            role=_norm(request.role),
            observed_at=observed_at,
        )

        try:
            workspace_id = _norm(repo_scope_workspace_id(self.repo_root))
        except Exception:  # noqa: BLE001 - an unresolvable scope is a fixed refusal
            workspace_id = ""
        if not workspace_id:
            return self._refuse(request, APPROVAL_WORKSPACE_UNRESOLVED, facts=base)
        base = replace(base, workspace_id=workspace_id)

        try:
            rows = list(self._rows())
        except Exception:  # noqa: BLE001 - an unreadable inventory proves nothing
            return self._refuse(
                request,
                APPROVAL_INVENTORY_UNREADABLE,
                facts=base,
                detail="inventory_unreadable",
            )

        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _decoded_identity_matches(
                row, workspace_id=workspace_id, lane=request.lane, role=request.role
            )
        ]
        # Zero and several are both refusals. Picking one of several would be exactly the guess
        # this surface exists to remove, and a duplicate identity is itself the anomaly. An
        # EMPTY inventory is a positive absence (nothing is running), not an unreadable one —
        # only a raised read is unreadable, and that is handled above.
        if len(matches) != 1:
            duplicate = len(matches) > 1
            reason = APPROVAL_DUPLICATE_RECEIVER if duplicate else APPROVAL_RECEIVER_ABSENT
            return self._refuse(
                request,
                reason,
                facts=base,
                receiver_present=True if duplicate else False,
                detail=reason,
            )

        row = matches[0]
        assigned_name = _norm(row.get(AGENT_KEY_NAME))
        locator = _agent_locator(row)
        revision = _row_revision(row)

        action_generation = ""
        if assigned_name and locator:
            try:
                action_generation = quarantine_action_id(
                    lane_id=request.lane, role=request.role, locator=locator
                )
            except ValueError:
                action_generation = ""

        inspection = self._inspect(
            request,
            assigned_name=assigned_name,
            locator=locator,
            revision=revision,
            rows=rows,
        )

        facts = ApprovalFacts(
            issue=_norm(request.issue),
            lane=_norm_lane(request.lane),
            role=_norm(request.role),
            workspace_id=workspace_id,
            assigned_name=assigned_name,
            locator=locator,
            agent_revision=inspection.row_revision if inspection.row_revision >= 0 else revision,
            attested_at=_norm(inspection.attested_at),
            action_generation=action_generation,
            observed_at=observed_at,
        )
        classification = inspection.classification
        reason = decide_approval_readiness(
            facts=facts,
            classification=classification,
            receiver_present=inspection.receiver_present,
            inventory_readable=True,
            composer_readable=bool(inspection.signal.inventory_readable),
            duplicate_receiver=False,
        )
        return QuarantineInspectOutcome(
            request=request,
            facts=facts,
            classification=classification,
            approval_reason=reason,
            receiver_present=inspection.receiver_present,
            inspection_detail=_norm(inspection.detail),
            approval_template=(
                render_approval_template(facts) if reason == APPROVAL_READY else ""
            ),
        )

    def _inspect(
        self,
        request: QuarantineInspectRequest,
        *,
        assigned_name: str,
        locator: str,
        revision: int,
        rows: Sequence[Mapping[str, object]],
    ) -> QuarantineInspection:
        """Classify the discovered receiver through the ONE #13763 inspector (same snapshot).

        ``approved_revision`` is seeded with the OBSERVED revision so the inspector reports the
        receiver's real state. This is an observation, not an approval: nothing is being
        authorized here, and the execute-time fence still re-compares an operator-supplied
        revision against live state, so seeding cannot weaken that check.
        """
        synthetic = QuarantineRequest(
            issue=_norm(request.issue),
            lane=_norm_lane(request.lane),
            journal=_INSPECTION_JOURNAL,
            role=_norm(request.role),
            assigned_name=assigned_name,
            locator=locator,
            action_generation=_INSPECTION_JOURNAL,
            approval_observed_at=_INSPECTION_EPOCH,
            approved_revision=revision,
        )
        return self._ops(rows).inspect(synthetic)


def format_inspect_text(outcome: QuarantineInspectOutcome) -> str:
    """Human rendering: the exact tokens, then either the template or the typed refusal."""
    facts = outcome.facts
    lines = [
        f"issue: {facts.issue}",
        f"lane: {facts.lane}",
        f"role: {facts.role}",
        f"workspace_id: {facts.workspace_id or '-'}",
        f"assigned_name: {facts.assigned_name or '-'}",
        f"locator: {facts.locator or '-'}",
        f"agent_revision: {facts.agent_revision if facts.revision_readable else '-'}",
        f"attested_at: {facts.attested_at or '-'}",
        f"action_generation: {facts.action_generation or '-'}",
        f"observed_at: {facts.observed_at}",
        f"classification: {outcome.classification.label}",
        f"q_enter_recommended: {outcome.classification.q_enter_recommended}",
        f"receiver_present: {outcome.receiver_present}",
        f"inspection_detail: {outcome.inspection_detail or '-'}",
        f"approval_ready: {outcome.approval_ready}",
        f"approval_reason: {outcome.approval_reason}",
    ]
    if outcome.approval_ready:
        lines += ["", "--- paste into the approval journal ---", outcome.approval_template]
    else:
        lines += [
            "",
            f"positive owner approval cannot be built: {outcome.approval_reason}",
        ]
    return "\n".join(lines)


def cmd_sublane_quarantine_inspect(args: argparse.Namespace) -> int:
    repo_root = Path(getattr(args, "repo", None) or ".").expanduser().resolve()
    request = QuarantineInspectRequest(
        issue=str(getattr(args, "issue", "") or ""),
        lane=str(getattr(args, "lane", "") or ""),
        role=str(getattr(args, "role", "") or ""),
    )
    outcome = SublaneQuarantineInspectUseCase(repo_root=repo_root).run(request)
    if bool(getattr(args, "as_json", False)):
        print(json.dumps(outcome.as_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_inspect_text(outcome))
    return 1 if outcome.is_blocked else 0


def register_sublane_quarantine_inspect_parser(sublane_sub: Any) -> None:
    parser = sublane_sub.add_parser(
        "quarantine-inspect",
        help=(
            "Redmine #14234: read-only. Report the exact assigned name / locator / agent "
            "revision / attested generation / quarantine action id for one lane role, and "
            "render the pasteable generation-bound owner approval when the observation "
            "supports one. Emits no composer body, hash, length, path or credential."
        ),
    )
    for flag, dest, help_text in (
        ("--issue", "issue", "Redmine issue id owning the lane"),
        ("--lane", "lane", "Exact lane id/label"),
        ("--role", "role", "Exact provider role of the receiver"),
    ):
        parser.add_argument(flag, dest=dest, required=True, help=help_text)
    add_repo_option(parser)
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON")
    parser.set_defaults(func=cmd_sublane_quarantine_inspect)


__all__ = (
    "QuarantineInspectRequest",
    "QuarantineInspectOutcome",
    "SublaneQuarantineInspectUseCase",
    "cmd_sublane_quarantine_inspect",
    "format_inspect_text",
    "register_sublane_quarantine_inspect_parser",
)
