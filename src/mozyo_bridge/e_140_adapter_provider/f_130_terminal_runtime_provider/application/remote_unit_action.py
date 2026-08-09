"""Preview-first routing of one remote Unit action (Redmine #15138).

A Unit observed on another Herdr server is not a pane this client may type
into.  Crossing a host boundary is a governance boundary crossing, so the only
sanctioned route is the target environment's **own** project gateway: the
action is delivered by running that host's ``project-gateway handoff`` against
its own registry, which resolves the project's Codex gateway semantically and
refuses ``--to claude`` outright.  The client never names a remote pane, never
sends to a remote worker, and never infers authority from where something is
drawn on screen.

The rail is preview-first and re-verifies at apply time.  A preview is an
explanation, not a permit: it records which source, workspace, and repository
identity it was computed from, and the apply step re-observes all three.  If the
Unit moved, the source stopped answering, the observation aged out, or the
registry now resolves the workspace differently, the apply refuses and sends
nothing.  Every refusal is a typed reason with a fixed message — never a
connection value, a remote path, or an exception body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    MAX_PRESENTATION_TEXT,
    SOURCE_LIVE,
    safe_text,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_aggregate import (
    DEFAULT_SOURCE_FRESHNESS_SECONDS,
    freshness_state,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSource,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
    MultiSourceUnitBoardRuntime,
    SourceUnitTarget,
    SourceWorkspace,
)


ACTION_APPLICABLE = "applicable"
ACTION_REFUSED = "refused"
ACTION_DELIVERED = "delivered"

REASON_OK = "ok"
REASON_UNIT_UNRESOLVED = "unit_unresolved"
REASON_LOCAL_SOURCE = "local_source_not_routed"
REASON_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REASON_PREVIEW_STALE = "preview_stale"
REASON_IDENTITY_CHANGED = "identity_changed"
REASON_INVALID_REQUEST = "invalid_request"
REASON_DELIVERY_FAILED = "delivery_failed"

#: Durable-anchor intents this rail may carry.  Deliberately the canonical
#: handoff vocabulary and nothing new: a remote Unit action is an ordinary
#: handoff that happens to cross a host boundary, not a new gate.
ACTION_KINDS = (
    "design_consultation",
    "implementation_request",
    "review_request",
    "reply",
    "custom",
)
DEFAULT_ACTION_KIND = "design_consultation"

#: The summary is bounded by the public-safe projection itself, not by a
#: separate limit.  A longer summary would be shown truncated in the preview and
#: sent in full, so the operator would be confirming something other than what
#: is delivered.
MAX_SUMMARY_LENGTH = MAX_PRESENTATION_TEXT

_DETAIL_BY_REASON = {
    REASON_UNIT_UNRESOLVED: (
        "the selected Unit could not be resolved to exactly one Unit on one live "
        "source; refresh the board and select it again"
    ),
    REASON_LOCAL_SOURCE: (
        "the selected Unit is on the local server; use the ordinary local handoff "
        "commands rather than the cross-source route"
    ),
    REASON_WORKSPACE_UNRESOLVED: (
        "the Unit's workspace does not resolve to exactly one registered "
        "repository on its own source"
    ),
    REASON_PREVIEW_STALE: (
        "the preview is older than the action freshness bound; preview again "
        "before applying"
    ),
    REASON_IDENTITY_CHANGED: (
        "the source, Unit, or repository identity changed between preview and "
        "apply; nothing was sent"
    ),
    REASON_INVALID_REQUEST: "the requested remote Unit action is not well formed",
    REASON_DELIVERY_FAILED: (
        "the target environment's project gateway did not accept the handoff; "
        "read its durable record before retrying"
    ),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RemoteUnitActionRequest:
    """What the operator asked for, before any of it is proven reachable."""

    unit_id: str
    issue: str
    journal: str
    summary: str
    kind: str = DEFAULT_ACTION_KIND

    def validated(self) -> Optional[str]:
        """Return the first structural problem, or ``None`` when well formed."""
        if not isinstance(self.unit_id, str) or not self.unit_id:
            return "a Unit selection is required"
        if not all(
            isinstance(value, str) and value.isdigit()
            for value in (self.issue, self.journal)
        ):
            return "a Redmine issue id and journal id are required"
        if self.kind not in ACTION_KINDS:
            return "the requested handoff kind is not supported by this route"
        if not isinstance(self.summary, str):
            return "a summary is required"
        summary = self.summary.strip()
        if not summary or len(summary) > MAX_SUMMARY_LENGTH:
            return (
                "a non-empty summary of at most "
                f"{MAX_SUMMARY_LENGTH} characters is required"
            )
        # What the operator confirms must be exactly what is delivered.  The
        # preview renders the summary through the public-safe projection, so a
        # summary that the projection would rewrite — a control character, an
        # absolute path, a credential shape, a form the projection normalizes —
        # is rejected rather than previewed as one string and sent as another.
        if safe_text(summary, fallback="") != summary:
            return (
                "the summary must survive the public-safe projection unchanged; "
                "remove control characters, absolute paths, and credential-shaped "
                "values and keep the durable record as the source of truth"
            )
        return None


@dataclass(frozen=True)
class _ActionEvidence:
    """The exact identity a preview was computed from, kept out of the payload.

    ``canonical_path`` is a path on the source host.  It lives here so the apply
    step can prove the repository identity did not move and can pass it as an
    argv value on that host; it never reaches a payload or a rendered line.
    """

    target: SourceUnitTarget
    workspace: SourceWorkspace


@dataclass(frozen=True)
class RemoteUnitActionPreview:
    """A public-safe explanation of the one command an apply would run."""

    state: str
    reason: str
    detail: str
    host_id: str = ""
    host_label: str = ""
    host_kind: str = ""
    project_label: str = ""
    lane_id: str = ""
    workspace_id: str = ""
    kind: str = ""
    issue: str = ""
    journal: str = ""
    summary: str = ""
    observed_at: str = ""
    evidence: Optional[_ActionEvidence] = None

    @property
    def applicable(self) -> bool:
        return self.state == ACTION_APPLICABLE

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": safe_text(self.detail, fallback=""),
            "host_id": safe_text(self.host_id, fallback=""),
            "host_label": safe_text(self.host_label, fallback=""),
            "host_kind": safe_text(self.host_kind, fallback=""),
            "project_label": safe_text(self.project_label, fallback=""),
            "lane_id": safe_text(self.lane_id, fallback=""),
            "workspace_id": safe_text(self.workspace_id, fallback=""),
            "kind": safe_text(self.kind, fallback=""),
            "issue": safe_text(self.issue, fallback=""),
            "journal": safe_text(self.journal, fallback=""),
            "summary": safe_text(self.summary, fallback=""),
            "observed_at": safe_text(self.observed_at, fallback=""),
            # Named, not spelled out: the route is fixed and the receiver is
            # fixed, so the operator confirms a boundary rather than a string.
            "route": "target-source project gateway",
            "receiver": "codex",
            "direct_worker_send": False,
        }


@dataclass(frozen=True)
class RemoteUnitActionResult:
    state: str
    reason: str
    detail: str
    preview: RemoteUnitActionPreview

    @property
    def delivered(self) -> bool:
        return self.state == ACTION_DELIVERED

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": safe_text(self.detail, fallback=""),
            "preview": self.preview.as_payload(),
        }


def _refused(reason: str, request: Optional[RemoteUnitActionRequest] = None, detail: str = "") -> RemoteUnitActionPreview:
    return RemoteUnitActionPreview(
        state=ACTION_REFUSED,
        reason=reason,
        detail=detail or _DETAIL_BY_REASON.get(reason, "the action was refused"),
        kind=request.kind if request else "",
        issue=request.issue if request else "",
        journal=request.journal if request else "",
        summary=request.summary if request else "",
    )


class RemoteUnitActionRail:
    """Resolve, explain, and — only on an explicit apply — deliver one action."""

    def __init__(
        self,
        runtime: MultiSourceUnitBoardRuntime,
        *,
        clock=_utc_now,
        freshness_seconds: int = DEFAULT_SOURCE_FRESHNESS_SECONDS,
    ) -> None:
        # Deliberately no runner of its own: every command that crosses a host
        # boundary goes through the runtime's single subprocess seam, so
        # observation and delivery can never diverge in argv, timeout, or the
        # seam a test injects.
        self._runtime = runtime
        self._clock = clock
        self._freshness_seconds = freshness_seconds

    def preview(self, request: RemoteUnitActionRequest) -> RemoteUnitActionPreview:
        problem = request.validated()
        if problem is not None:
            return _refused(REASON_INVALID_REQUEST, request, problem)
        target = self._runtime.resolve_unit_target(request.unit_id)
        if target is None:
            return _refused(REASON_UNIT_UNRESOLVED, request)
        if target.source.is_local:
            return _refused(REASON_LOCAL_SOURCE, request)
        workspace = self._runtime.resolve_source_workspace(
            target.source, target.workspace_id
        )
        if workspace is None:
            return _refused(REASON_WORKSPACE_UNRESOLVED, request)
        return RemoteUnitActionPreview(
            state=ACTION_APPLICABLE,
            reason=REASON_OK,
            detail=(
                "one handoff will be delivered through the target environment's "
                "own project gateway; the remote worker is never direct-sent"
            ),
            host_id=target.source.host_id,
            host_label=target.source.label,
            host_kind=target.source.kind,
            project_label=target.project_label,
            lane_id=target.lane_id,
            workspace_id=target.workspace_id,
            kind=request.kind,
            issue=request.issue,
            journal=request.journal,
            summary=request.summary.strip(),
            observed_at=target.observed_at,
            evidence=_ActionEvidence(target=target, workspace=workspace),
        )

    def _gateway_args(
        self,
        preview: RemoteUnitActionPreview,
        workspace: SourceWorkspace,
    ) -> tuple[str, ...]:
        return (
            "project-gateway",
            "handoff",
            "--to",
            "codex",
            "--source",
            "redmine",
            "--issue",
            preview.issue,
            "--journal",
            preview.journal,
            "--kind",
            preview.kind,
            "--target-repo",
            workspace.canonical_path,
            "--target-project",
            workspace.project_name,
            "--mode",
            "standard",
            "--summary",
            preview.summary,
            "--json",
        )

    def apply(self, preview: RemoteUnitActionPreview) -> RemoteUnitActionResult:
        """Re-prove the preview, then deliver exactly once.

        Ordering matters: freshness first (cheap, and a stale preview should not
        cause a round trip), then identity, then delivery.  Nothing is sent
        until all three hold.
        """
        if not preview.applicable or preview.evidence is None:
            return RemoteUnitActionResult(
                ACTION_REFUSED,
                REASON_INVALID_REQUEST,
                "a fresh applicable preview is required before applying",
                preview,
            )
        if (
            freshness_state(
                preview.observed_at,
                self._clock(),
                max_age_seconds=self._freshness_seconds,
            )
            != SOURCE_LIVE
        ):
            return self._refuse(preview, REASON_PREVIEW_STALE)

        target = self._runtime.resolve_unit_target(preview.evidence.target.unit_id)
        if target is None:
            return self._refuse(preview, REASON_UNIT_UNRESOLVED)
        previewed = preview.evidence.target
        if (
            target.source.host_id != previewed.source.host_id
            or target.remote_unit_id != previewed.remote_unit_id
            or target.workspace_id != previewed.workspace_id
            or target.lane_id != previewed.lane_id
        ):
            return self._refuse(preview, REASON_IDENTITY_CHANGED)

        workspace = self._runtime.resolve_source_workspace(
            target.source, target.workspace_id
        )
        if workspace is None:
            return self._refuse(preview, REASON_WORKSPACE_UNRESOLVED)
        if (
            workspace.canonical_path != preview.evidence.workspace.canonical_path
            or workspace.project_name != preview.evidence.workspace.project_name
        ):
            return self._refuse(preview, REASON_IDENTITY_CHANGED)

        return self._deliver(preview, target.source, workspace)

    def _refuse(
        self, preview: RemoteUnitActionPreview, reason: str
    ) -> RemoteUnitActionResult:
        return RemoteUnitActionResult(
            ACTION_REFUSED,
            reason,
            _DETAIL_BY_REASON.get(reason, "the action was refused"),
            preview,
        )

    def _deliver(
        self,
        preview: RemoteUnitActionPreview,
        source: UnitBoardSource,
        workspace: SourceWorkspace,
    ) -> RemoteUnitActionResult:
        completed = self._runtime.run_source_command(
            source, self._gateway_args(preview, workspace)
        )
        if completed is None or completed.returncode != 0:
            return self._refuse(preview, REASON_DELIVERY_FAILED)
        return RemoteUnitActionResult(
            ACTION_DELIVERED,
            REASON_OK,
            _delivery_detail(completed.stdout),
            preview,
        )


def _delivery_detail(stdout: object) -> str:
    """Summarize the gateway's own answer without echoing it.

    The gateway prints a delivery record that can carry a pane id and a repo
    root.  Only its ``result`` token is reflected here; the record itself stays
    on the host that produced it, where the durable anchor already points.
    """
    default = "the target environment's project gateway accepted the handoff"
    if not isinstance(stdout, str) or not stdout.strip():
        return default
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return default
    if not isinstance(payload, dict):
        return default
    result = payload.get("result")
    if not isinstance(result, str) or not result:
        return default
    return f"{default} (result={safe_text(result)})"


def render_preview(preview: RemoteUnitActionPreview) -> Sequence[str]:
    """Operator-facing lines for one preview.  Public-safe by construction."""
    if not preview.applicable:
        return (
            f"remote Unit action: refused ({preview.reason})",
            f"  {preview.detail}",
        )
    return (
        "remote Unit action: preview",
        f"  source:  {preview.host_label} [{preview.host_kind}]",
        f"  project: {preview.project_label}",
        f"  lane:    {preview.lane_id}",
        f"  route:   target-source project gateway -> codex",
        f"  anchor:  Redmine #{preview.issue} j#{preview.journal} ({preview.kind})",
        f"  summary: {preview.summary}",
        "  the remote worker is never direct-sent; apply re-verifies source, "
        "Unit, and repository identity",
    )


__all__ = (
    "ACTION_APPLICABLE",
    "ACTION_DELIVERED",
    "ACTION_KINDS",
    "ACTION_REFUSED",
    "DEFAULT_ACTION_KIND",
    "MAX_SUMMARY_LENGTH",
    "REASON_DELIVERY_FAILED",
    "REASON_IDENTITY_CHANGED",
    "REASON_INVALID_REQUEST",
    "REASON_LOCAL_SOURCE",
    "REASON_OK",
    "REASON_PREVIEW_STALE",
    "REASON_UNIT_UNRESOLVED",
    "REASON_WORKSPACE_UNRESOLVED",
    "RemoteUnitActionPreview",
    "RemoteUnitActionRail",
    "RemoteUnitActionRequest",
    "RemoteUnitActionResult",
    "render_preview",
)
