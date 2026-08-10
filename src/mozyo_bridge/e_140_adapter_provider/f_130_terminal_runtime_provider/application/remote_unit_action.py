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

What crossing the boundary does **not** change is how the handoff is sent or how
its outcome is read (Redmine #15198).  The route is special; the delivery is not.
So the mode comes from the same shared default an ordinary agent handoff uses
instead of being pinned here, and the answer to "did it arrive?" comes from the
same shared injection-stage authority — including its third answer.  A submission
that was never confirmed is reported as uncertain, not rewritten into a success
or into a non-delivery, because those two readings license the two failures that
matter: closing an unread request, and delivering it twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (
    is_canonical_positive_decimal,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    MODES,
    QUEUE_ENTER_RETRY_WINDOW_SECONDS,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff_send_semantics import (
    MODE_QUEUE_ENTER,
    effective_send_mode,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (
    STAGE_NOT_SENT,
    STAGE_SUBMITTED_CONFIRMED,
    STAGE_UNCERTAIN_PARTIAL,
    blind_retry_prohibited,
    injection_stage_for,
    stage_from_telemetry,
)
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
    UntrustedJsonError,
    loads_untrusted_json,
    SourceUnitTarget,
    SourceWorkspace,
)


ACTION_APPLICABLE = "applicable"
ACTION_REFUSED = "refused"
ACTION_DELIVERED = "delivered"
#: Redmine #15198.  The third answer this rail previously could not give.  A
#: delivery that reached the receiver's composer without a confirmed submission
#: is neither ``delivered`` nor ``refused``: calling it delivered claims a
#: submission nobody observed, and calling it refused claims a zero-send that
#: would license a duplicate retry.  The shared authority already names the
#: distinction (``uncertain_partial``); this state is where it surfaces.
ACTION_UNCERTAIN = "uncertain"

REASON_OK = "ok"
REASON_UNIT_UNRESOLVED = "unit_unresolved"
REASON_LOCAL_SOURCE = "local_source_not_routed"
REASON_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REASON_PREVIEW_STALE = "preview_stale"
REASON_IDENTITY_CHANGED = "identity_changed"
REASON_INVALID_REQUEST = "invalid_request"
REASON_DELIVERY_FAILED = "delivery_failed"
REASON_DELIVERY_UNCERTAIN = "delivery_uncertain"
REASON_CONNECTION_VALUE_DISCLOSED = "connection_value_disclosed"
REASON_PREVIEW_MISMATCH = "preview_mismatch"

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

#: The send rails an operator may name explicitly, and nothing invented here:
#: the canonical ``handoff send`` vocabulary, imported so this rail cannot drift
#: into offering a mode the gateway does not have (Redmine #15198).
ACTION_DELIVERY_MODES: tuple[str, ...] = tuple(sorted(MODES))

#: An unset delivery mode means "whatever an ordinary agent handoff would do",
#: resolved through the shared authority rather than restated as a literal.  This
#: rail used to pin ``--mode standard`` at the caller, which is why a Unit Board
#: action took a different rail from every other handoff to the same receiver
#: (Redmine #15198): the pin was a caller-side policy decision that no longer
#: tracked the default it was forked from.  ``standard`` and ``pending`` stay
#: reachable — as an explicit operator choice for strict landing observation or
#: debugging — but they are chosen, never defaulted into.
DEFAULT_DELIVERY_MODE = ""

#: The summary is bounded by the public-safe projection itself, not by a
#: separate limit.  A longer summary would be shown truncated in the preview and
#: sent in full, so the operator would be confirming something other than what
#: is delivered.
MAX_SUMMARY_LENGTH = MAX_PRESENTATION_TEXT

#: A Redmine id is judged by the repository's shared canonical-decimal
#: predicate, not by a bound invented here.  ``str.isdigit`` is true for
#: full-width digits and the projection folds those to ASCII while the delivery
#: sends the raw string, so the shape does have to be checked (review j#102018
#: finding_4) — but the shape is already defined, and a narrower local rule
#: rejected ids the rest of the repository accepts (review j#102129 finding_6).

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
    REASON_CONNECTION_VALUE_DISCLOSED: (
        "the request text repeats a configured connection value; the preview and "
        "the delivered handoff are public surfaces and must not carry one"
    ),
    REASON_PREVIEW_MISMATCH: (
        "the preview does not match the request it was computed from; apply "
        "delivers the request that was validated, not a preview handed back to it"
    ),
    REASON_DELIVERY_FAILED: (
        "the target environment's project gateway refused the handoff before "
        "anything was typed at its receiver; read that environment's durable "
        "record, clear the named refusal, then re-issue the same anchor"
    ),
    REASON_DELIVERY_UNCERTAIN: (
        "the handoff may already have reached the target environment's receiver "
        "and the submission was not confirmed; read that environment's durable "
        "record to establish what arrived before re-issuing — a blind retry can "
        "deliver the same request twice"
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
    #: The adopted project scope of the target repository, as declared by the
    #: operator.  It is NOT derived from the board or the registry: the registry
    #: ``project_name`` is display metadata and must not become a scope
    #: authority (Redmine #15138 review j#101787 f1), and a board label has
    #: already been through the public-safe projection.  Requiring it keeps the
    #: client from synthesizing an authority it does not hold.
    target_project: str = ""
    kind: str = DEFAULT_ACTION_KIND
    #: Empty means the canonical default rail; a named mode is an explicit
    #: operator choice for strict observation or debugging.  Deliberately not a
    #: default of ``standard``: see :data:`DEFAULT_DELIVERY_MODE`.
    delivery_mode: str = DEFAULT_DELIVERY_MODE

    @property
    def effective_delivery_mode(self) -> str:
        """The rail this request will actually be delivered on (pure).

        Resolved through the shared ``handoff send`` default so the preview names
        the same rail the argv selects, and so a change to that default reaches
        this route without an edit here.
        """
        return effective_send_mode(self.delivery_mode or None)

    def validated(self) -> Optional[str]:
        """Return the first structural problem, or ``None`` when well formed."""
        if not isinstance(self.unit_id, str) or not self.unit_id:
            return "a Unit selection is required"
        if not all(
            is_canonical_positive_decimal(value)
            for value in (self.issue, self.journal)
        ):
            return (
                "a Redmine issue id and journal id are required as canonical "
                "positive decimal numbers, without a leading zero and within the "
                "width every runtime can convert"
            )
        if self.kind not in ACTION_KINDS:
            return "the requested handoff kind is not supported by this route"
        if not isinstance(self.delivery_mode, str) or (
            self.delivery_mode and self.delivery_mode not in ACTION_DELIVERY_MODES
        ):
            return (
                "the delivery mode must be omitted — taking the same default rail "
                "as an ordinary agent handoff — or name one of: "
                + ", ".join(ACTION_DELIVERY_MODES)
            )
        if not isinstance(self.target_project, str) or not self.target_project.strip():
            return (
                "the target repository's adopted project scope is required; the "
                "board label and the registry project name are display values and "
                "cannot stand in for it"
            )
        scope = self.target_project.strip()
        if safe_text(scope, fallback="") != scope:
            return "the target project scope must be plain, public-safe text"
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
    """What a preview was computed from, kept out of the payload and the repr.

    Three things, and the third is the one that makes ``apply`` an act of
    re-proving rather than of trusting: the identity the preview resolved
    (``target`` / ``workspace``) **and the validated request it described**.

    A preview is a public object this package exports.  Reading its fields back
    at apply time would let a caller hand over a preview describing one action
    and have another delivered — a different anchor, a different intent, or a
    summary that never passed validation (review j#102159 finding_1).  So the
    request that was actually checked travels here, and delivery is built from
    it.

    ``workspace.canonical_path`` is a path on the source host.  It lives here so
    apply can prove the repository identity did not move and can pass it as an
    argv value there; it never reaches a payload, a rendered line, or a repr.
    """

    target: SourceUnitTarget
    workspace: SourceWorkspace
    request: RemoteUnitActionRequest


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
    target_project: str = ""
    lane_id: str = ""
    workspace_id: str = ""
    kind: str = ""
    issue: str = ""
    journal: str = ""
    summary: str = ""
    observed_at: str = ""
    #: The rail the apply will select, already resolved through the shared
    #: default — so the operator confirms the rail rather than inferring it from
    #: the absence of a flag.  Part of the compared object, so a substituted
    #: preview cannot swap the rail either.
    delivery_mode: str = ""
    #: Kept out of the repr as well as the payload: it holds the source's
    #: connection values and the remote repository path, and an object that is
    #: safe to render but not to print is only half safe (review j#102159
    #: finding_2).
    evidence: Optional[_ActionEvidence] = field(default=None, repr=False)

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
            "target_project": safe_text(self.target_project, fallback=""),
            "lane_id": safe_text(self.lane_id, fallback=""),
            "workspace_id": safe_text(self.workspace_id, fallback=""),
            "kind": safe_text(self.kind, fallback=""),
            "issue": safe_text(self.issue, fallback=""),
            "journal": safe_text(self.journal, fallback=""),
            "summary": safe_text(self.summary, fallback=""),
            "observed_at": safe_text(self.observed_at, fallback=""),
            "delivery_mode": safe_text(self.delivery_mode, fallback=""),
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
    #: The shared three-token verdict (Redmine #15198).  Every path that returns
    #: without running the gateway command is a genuine zero-send, so
    #: :data:`STAGE_NOT_SENT` is the honest default rather than a convenient one.
    injection_stage: str = STAGE_NOT_SENT

    @property
    def delivered(self) -> bool:
        return self.state == ACTION_DELIVERED

    @property
    def uncertain(self) -> bool:
        """True when the request may have reached the receiver unconfirmed.

        Separate from :attr:`delivered` on purpose: the two questions "did it
        arrive?" and "may I send it again?" have different answers here, and
        collapsing them is what turned an unconfirmed submission into a reported
        non-delivery.
        """
        return self.state == ACTION_UNCERTAIN

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": safe_text(self.detail, fallback=""),
            # The token and the retry verdict both come from the shared
            # authority; this rail publishes them rather than re-deriving a
            # local opinion about what the target gateway's outcome meant.
            "injection_stage": self.injection_stage,
            "blind_retry_prohibited": blind_retry_prohibited(self.injection_stage),
            "preview": self.preview.as_payload(),
        }


def _refused(
    reason: str,
    request: Optional[RemoteUnitActionRequest] = None,
    detail: str = "",
) -> RemoteUnitActionPreview:
    """Build a refusal that carries the anchor but never the operator's free text.

    A refused preview is still a payload, so echoing the request back would make
    the refusal itself a disclosure surface — including for the refusal whose
    whole purpose is that the text disclosed a connection value.  The durable
    anchor and the kind identify which request was refused; the operator already
    has what they typed.
    """
    return RemoteUnitActionPreview(
        state=ACTION_REFUSED,
        reason=reason,
        detail=detail or _DETAIL_BY_REASON.get(reason, "the action was refused"),
        kind=request.kind if request else "",
        issue=request.issue if request else "",
        journal=request.journal if request else "",
    )


def _applicable_preview(evidence: _ActionEvidence) -> RemoteUnitActionPreview:
    """The one applicable preview a given evidence set describes (pure).

    Built in exactly one place so ``apply`` can rebuild it and compare the whole
    object.  Comparing a hand-listed set of fields would leave the next field
    someone adds unchecked; comparing the objects cannot.
    """
    target = evidence.target
    request = evidence.request
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
        target_project=request.target_project.strip(),
        lane_id=target.lane_id,
        workspace_id=target.workspace_id,
        kind=request.kind,
        issue=request.issue,
        journal=request.journal,
        summary=request.summary.strip(),
        observed_at=target.observed_at,
        delivery_mode=request.effective_delivery_mode,
        evidence=evidence,
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
        # Operator-typed text reaches the preview payload and the delivered
        # handoff, both public surfaces.  A configured ssh destination or
        # container name repeated there re-exposes exactly what the operator
        # source file exists to keep off them (Redmine #15138 review j#101787
        # f8).  Checked against every configured source, not just the target:
        # disclosing another host's connection value is no better.
        for text in (request.summary, request.target_project):
            disclosed = self._runtime.config.disclosed_connection_value(text)
            if disclosed is not None:
                return _refused(REASON_CONNECTION_VALUE_DISCLOSED, request)
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
        return _applicable_preview(
            _ActionEvidence(target=target, workspace=workspace, request=request)
        )

    def _gateway_args(
        self,
        request: RemoteUnitActionRequest,
        workspace: SourceWorkspace,
    ) -> tuple[str, ...]:
        """The delivered argv, built from the VALIDATED request.

        Reading the preview here would make the comparison above the only thing
        standing between a substituted field and the wire.  Building from the
        request means a substitution has to get past both.

        The mode is resolved, not pinned (Redmine #15198).  A Unit Board action
        is an ordinary handoff that happens to cross a host boundary, so it takes
        the same rail an ordinary handoff to that receiver would take; the
        operator can still name ``standard`` or ``pending`` when strict landing
        observation or debugging calls for one.  Sending the mode explicitly
        rather than omitting the flag keeps the selection deterministic across
        gateway versions — the value comes from the shared default, so it cannot
        become a second opinion about what that default is.
        """
        return (
            "project-gateway",
            "handoff",
            "--to",
            "codex",
            "--source",
            "redmine",
            "--issue",
            request.issue,
            "--journal",
            request.journal,
            "--kind",
            request.kind,
            "--target-repo",
            workspace.canonical_path,
            "--target-project",
            request.target_project.strip(),
            "--mode",
            request.effective_delivery_mode,
            "--summary",
            request.summary.strip(),
            # The gateway's own ``--json`` only shapes a *fail-closed resolution*
            # payload; a successful handoff still uses the ``both`` record format
            # by default and prints a markdown record before the JSON.  Asking
            # for the single-line shape makes the answer deterministic instead
            # of leaving the reader to cope with two of them (review j#101891
            # finding_1).
            "--record-format",
            "json",
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
        # Re-prove the request itself, not just the identity it resolved.  The
        # preview is a public object; every check `preview()` ran is re-run here
        # against the request that travelled with the evidence, and the preview
        # is then required to be exactly the one that request describes.
        evidence = preview.evidence
        problem = evidence.request.validated()
        if problem is not None:
            return self._refuse(preview, REASON_INVALID_REQUEST)
        for text in (evidence.request.summary, evidence.request.target_project):
            if self._runtime.config.disclosed_connection_value(text) is not None:
                return self._refuse(preview, REASON_CONNECTION_VALUE_DISCLOSED)
        expected = _applicable_preview(evidence)
        if replace(preview, evidence=None) != replace(expected, evidence=None):
            return self._refuse(preview, REASON_PREVIEW_MISMATCH)
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
        if workspace.canonical_path != preview.evidence.workspace.canonical_path:
            return self._refuse(preview, REASON_IDENTITY_CHANGED)

        return self._deliver(preview, target.source, workspace, evidence.request)

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
        request: RemoteUnitActionRequest,
    ) -> RemoteUnitActionResult:
        """Run the one gateway command and report what the shared authority saw.

        Exactly one invocation, whatever comes back.  The body is handed to the
        gateway once; any Enter-only retry belongs to the rail on the far side,
        under its own bounded policy, and re-running this command would re-type
        the body rather than re-press Enter (Redmine #15198).
        """
        result = self._runtime.run_source_command(
            source,
            self._gateway_args(request, workspace),
            completion_window_seconds=(
                QUEUE_ENTER_RETRY_WINDOW_SECONDS
                if request.effective_delivery_mode == MODE_QUEUE_ENTER
                else 0.0
            ),
        )
        stage = _delivery_stage(
            result,
            expected_mode=request.effective_delivery_mode,
        )
        if stage == STAGE_SUBMITTED_CONFIRMED:
            return RemoteUnitActionResult(
                ACTION_DELIVERED,
                REASON_OK,
                "the target environment's project gateway confirmed the submission",
                preview,
                injection_stage=stage,
            )
        if stage == STAGE_NOT_SENT:
            return self._refuse(preview, REASON_DELIVERY_FAILED)
        # Everything the authority cannot place as confirmed or as a zero-send.
        # Reporting it as a refusal would be the #15198 defect: the operator reads
        # "nothing was sent", re-issues, and the receiver gets the request twice.
        return RemoteUnitActionResult(
            ACTION_UNCERTAIN,
            REASON_DELIVERY_UNCERTAIN,
            _DETAIL_BY_REASON[REASON_DELIVERY_UNCERTAIN],
            preview,
            injection_stage=STAGE_UNCERTAIN_PARTIAL,
        )


def _delivery_outcome_record(stdout: object) -> Optional[dict]:
    """The structured delivery outcome from a handoff CLI's stdout, or ``None``.

    The documented scrape target is the **last JSON-looking line**: the default
    ``record_format=both`` prints a human-readable record first, a blank line,
    and the single-line outcome last, precisely so that callers reading the last
    JSON line keep working (``handoff_delivery_command``).  Parsing the whole
    stdout as one document therefore fails on the shape the CLI actually emits,
    which is how a real delivery came to be reported as a failure (review
    j#101891 finding_1).

    Every existing consumer of this contract scans the lines in reverse locally;
    there is no shared export to import, and adding one would create a fourth
    place that answers this question.  So the same documented scan lives here,
    while the *verdict* stays with the shared authority below.
    """
    if not isinstance(stdout, str):
        return None
    candidates = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not candidates:
        return None
    # Exactly one candidate: the last non-empty line.  Scanning further back for
    # something parseable would let output that follows the outcome be ignored,
    # so a stale success line would survive whatever came after it — a
    # fail-open reading of a fail-closed contract (review j#101928 finding_4).
    line = candidates[-1]
    if not (line.startswith("{") and line.endswith("}")):
        return None
    try:
        payload = loads_untrusted_json(line)
    except UntrustedJsonError:
        return None
    if not isinstance(payload, dict):
        return None
    if "status" not in payload or "reason" not in payload:
        return None
    return payload


def _gateway_injection_stage(stdout: object, *, expected_mode: str) -> str:
    """The shared stage token for the target gateway's own outcome.

    A zero exit code is **not** proof of delivery — ``delivery_outcome_gate``
    documents the two rc-0 shapes that never reached a receiver (a ``pending``
    send that parks the body in the composer, and a marker-unobserved
    ``queue-enter``).  So the structured outcome is read, and it is read through
    the *shared* :func:`injection_stage_for` authority rather than by re-testing
    status/reason tokens locally: #14232 records what happened when three places
    answered "was it delivered?" with their own private tables.

    The gateway serialises its whole ``DeliveryOutcome``, including the effective
    mode, both turn-start telemetries, and the ``injection_stage`` the producer
    derived from that full context.  This boundary must not trust any one of
    those claims in isolation.  The gateway mode has to equal the mode this
    caller requested, and the carried stage (when present) has to equal a fresh
    full-context classification by the shared authority.  A missing/mismatched
    mode or a contradictory carried stage is therefore uncertain rather than a
    confirmation or a zero-send (review j#103034, UBRA-RESULT-001).

    Everything unreadable — absent output, no JSON line, a non-object — resolves
    to :data:`STAGE_UNCERTAIN_PARTIAL`, the same direction the authority itself
    takes for an outcome it cannot see (review j#101846 finding_1).  Unreadable
    is *not* the same as not-sent: the command did run.
    """
    payload = _delivery_outcome_record(stdout)
    if payload is None:
        return STAGE_UNCERTAIN_PARTIAL
    record_mode = payload.get("mode")
    if not isinstance(record_mode, str) or record_mode != expected_mode:
        return STAGE_UNCERTAIN_PARTIAL

    recomputed = injection_stage_for(
        payload.get("status"),
        payload.get("reason"),
        mode=record_mode,
        queue_enter_turn_start_observation=payload.get(
            "queue_enter_turn_start_observation"
        ),
        turn_start_outcome=payload.get("turn_start_outcome"),
    )
    carried = stage_from_telemetry(payload.get("injection_stage"))
    if "injection_stage" in payload and carried is None:
        return STAGE_UNCERTAIN_PARTIAL
    if carried is not None and carried != recomputed:
        return STAGE_UNCERTAIN_PARTIAL
    return recomputed


def _delivery_stage(result, *, expected_mode: str) -> str:
    """The stage for one whole gateway invocation, exit status included (pure).

    The structured outcome is the verdict; the exit status is only allowed to
    *withhold* a confirmation, never to grant one.  A gateway that printed a
    confirmed submission and then exited non-zero is emitting a shape its own CLI
    does not produce, and the fail-closed reading of a contradiction is that the
    submission is unconfirmed — not that it succeeded, and not that it never
    happened.
    """
    completed = result.completed
    if not result.ok or completed is None:
        # The command could not be run to completion.  Whether the remote reached
        # its receiver before the transport gave up is exactly what nobody knows.
        return STAGE_UNCERTAIN_PARTIAL
    stage = _gateway_injection_stage(
        completed.stdout,
        expected_mode=expected_mode,
    )
    if stage == STAGE_SUBMITTED_CONFIRMED and completed.returncode != 0:
        return STAGE_UNCERTAIN_PARTIAL
    return stage


def render_preview(preview: RemoteUnitActionPreview) -> Sequence[str]:
    """Operator-facing lines for one preview.  Public-safe by construction.

    Rendered from the payload, never from the raw attributes.  Reading the
    attributes directly meant the JSON surface was projected while the terminal
    surface was not, so a value the projection would have redacted printed
    verbatim (review j#102018 finding_1).  Taking both from one place makes them
    agree by construction.
    """
    payload = preview.as_payload()
    if not preview.applicable:
        return (
            f"remote Unit action: refused ({payload['reason']})",
            f"  {payload['detail']}",
        )
    return (
        "remote Unit action: preview",
        f"  source:  {payload['host_label']} [{payload['host_kind']}]",
        f"  project: {payload['project_label']}",
        f"  scope:   {payload['target_project']}",
        f"  lane:    {payload['lane_id']}",
        "  route:   target-source project gateway -> codex",
        f"  anchor:  Redmine #{payload['issue']} j#{payload['journal']} "
        f"({payload['kind']})",
        f"  rail:    --mode {payload['delivery_mode']}",
        f"  summary: {payload['summary']}",
        "  the remote worker is never direct-sent; apply re-verifies source, "
        "Unit, and repository identity",
    )


__all__ = (
    "ACTION_APPLICABLE",
    "ACTION_DELIVERED",
    "ACTION_DELIVERY_MODES",
    "ACTION_KINDS",
    "ACTION_REFUSED",
    "ACTION_UNCERTAIN",
    "DEFAULT_ACTION_KIND",
    "DEFAULT_DELIVERY_MODE",
    "MAX_SUMMARY_LENGTH",
    "REASON_CONNECTION_VALUE_DISCLOSED",
    "REASON_DELIVERY_FAILED",
    "REASON_DELIVERY_UNCERTAIN",
    "REASON_IDENTITY_CHANGED",
    "REASON_INVALID_REQUEST",
    "REASON_LOCAL_SOURCE",
    "REASON_OK",
    "REASON_PREVIEW_MISMATCH",
    "REASON_PREVIEW_STALE",
    "REASON_UNIT_UNRESOLVED",
    "REASON_WORKSPACE_UNRESOLVED",
    "RemoteUnitActionPreview",
    "RemoteUnitActionRail",
    "RemoteUnitActionRequest",
    "RemoteUnitActionResult",
    "render_preview",
)
