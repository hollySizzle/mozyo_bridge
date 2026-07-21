"""Public guarded retirement of a session-start scratch pair (Redmine #13892).

The inverse of ``herdr session-start``. ``session-start`` mints an exact Claude/Codex pair
whose only durable identity is its herdr assigned name; it writes no lane lifecycle record,
so every recorded-lane retirement surface refuses it structurally and the pair leaks
capacity forever (live evidence: #13882 j#80060 / j#80066 — a preserved ``dogfood13882``
pair no public rail could retire, which blocked that ticket's F4 retry).

This module is the impure half of that surface: it resolves the exact pair from durable
identity, observes the unit at action time, asks the pure
:func:`...domain.scratch_pair_retire.decide_scratch_pair_retire` for a verdict, and — only
on a green verdict — closes the resolved locators and records the durable outcome.

It **composes reviewed parts** rather than re-deriving them (the #13847 pattern):

- ``plan_herdr_retire_close`` purely as a **unit scoper** (never for its own close targets):
  it is what fixes the targeted unit to this lane and structurally excludes the project
  workspace's default-lane coordinator pair and every other lane (#13377);
- ``expected_slot_rows`` — the RAW scan, read alongside the plan. The aggregated
  ``expected_live_slots`` role-set is deliberately NOT the authority here: it drops
  unexpected occupants, duplicate multiplicity and locator-less rows, so an empty aggregate
  means "no expected role is live", never "the unit is empty" (#13845 review j#80148);
- ``execute_herdr_retire_close`` — the reviewed, per-target non-fatal ``herdr pane close``,
  driven with the **verdict's** pin-matched targets (the #13842 ``pin_matched_close_plan``
  shape), so nothing outside the decided set can ever be closed.

Boundaries this surface keeps: no lifecycle row is ever created (acceptance 4 — a row minted
to pass a retire is fabrication, refused by #13882 j#80066); no store is mutated; no
worktree / branch is removed; no process is launched or resumed; no raw herdr / tmux is
driven by the caller. The only mutation is the pin-matched close of this pair's own slots.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire_ops import (  # noqa: E501
    LiveSessionRetireOps,
    SessionRetireOps,
    observe_scratch_pair,
)
from mozyo_bridge.core.state.scratch_retirement_fence import (  # noqa: E501
    RetirementUnit,
    ScratchRetirementBusy,
    ScratchRetirementFenceError,
    slot_digest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    _norm_lane,
    decode_assigned_name,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_slot_liveness import (  # noqa: E501
    SLOT_STALE,
    classify_named_slot,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.scratch_pair_retire import (  # noqa: E501
    STATE_ABSENT,
    STATE_BLOCKED,
    STATE_GREEN,
    ScratchPairObservation,
    ScratchPairRetireVerdict,
    ScratchSlotObservation,
    decide_scratch_pair_retire,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.composer_discard_approval import (  # noqa: E501
    ComposerDiscardApprovalError,
    ComposerDiscardApprovalEvidence,
)

#: The herdr runtime receiver-states a settled, drivable agent may be in. ``busy`` /
#: ``blocked`` / ``unknown`` are NOT settled — never close over an in-flight or unreadable
#: turn (the #13842 ``_SETTLED_RUNTIME_STATES`` contract).
_SETTLED_RUNTIME_STATES = frozenset({RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED})

#: Reasons the command itself refuses before any observation (zero-read, zero-write).
REASON_NO_REPO_ANCHOR = "no_repo_anchor"
REASON_WORKSPACE_UNRESOLVED = "workspace_unresolved"
REASON_LANE_REQUIRED = "lane_required"
REASON_LANE_IS_DEFAULT = "lane_is_default"
REASON_PROVIDER_UNRESOLVED = "provider_unresolved"
REASON_IDENTITY_UNENCODABLE = "identity_unencodable"
REASON_CLOSE_FAILED = "close_failed"
#: A durable obligation is owed to one of this pair's slots (Redmine #13892 review j#80506 F4).
#: A reserved / uncertain dispatch-outbox row means a send took the write lock against that
#: assigned name and its fate is unresolved. A runtime ``idle`` / ``turn_ended`` reading cannot
#: rule this out: receiver state and durable obligation are different axes, and the workflow
#: contract forbids promoting a runtime signal into a gate verdict.
REASON_WORK_OBLIGATION_PRESENT = "work_obligation_present"
#: The durable obligation store could not be read. Not observing an obligation is not the same
#: as there being none, so this fails closed rather than closing over unknown owed work.
REASON_OBLIGATION_UNREADABLE = "obligation_unreadable"
#: The post-close re-measure found the targeted unit still occupied (Redmine #13892 review
#: j#80506 F3). A close command's return code is not proof the unit is empty; capacity is
#: recovered only when the panes are actually gone.
REASON_POST_CLOSE_RESIDUE = "post_close_residue"
#: The post-close re-measure could not read a fresh inventory, so the unit's emptiness — the
#: whole point of the retire — is unproven. The closes that committed are still reported.
REASON_POST_CLOSE_UNREADABLE = "post_close_unreadable"
#: Zero expected slots are live, but nothing proves THIS command retired them (Redmine #13892
#: review j#80506 F1). A mistyped ``--lane`` and a never-launched pair are indistinguishable
#: from a completed retire by absence alone, so absence is refused rather than celebrated.
REASON_RETIRE_EVIDENCE_ABSENT = "retire_evidence_absent"
#: Another retirement transaction holds this unit. Zero-close; never wait, never steal.
REASON_RETIREMENT_BUSY = "retirement_busy"
#: The retirement authority is unavailable / unprovable (damaged artifacts, unknown schema,
#: seal mismatch, or a zero-slot run over an absent store). Fail closed (j#80526).
REASON_RETIREMENT_AUTHORITY_UNAVAILABLE = "retirement_authority_unavailable"
#: The close committed but the fence completion write failed. NOT a success — but the closes
#: that committed are reported truthfully, and the next run repairs from the pending attempt.
REASON_COMPLETION_UNPROVEN = "completion_unproven"
#: The surface's own signature (the unit has no lifecycle record) was lost mid-flight
#: (Redmine #13892 review j#80523 R2-F3). Re-verified before the completion, never assumed.
REASON_SIGNATURE_LOST = "signature_lost"
#: A pending attempt's pinned slot is live at a DIFFERENT locator: the assigned name matches
#: but the process does not, so it is a relaunched pair the old attempt cannot close
#: (Redmine #13892 review j#80523 R3-F2).
REASON_PIN_DRIFT = "pin_drift"
#: The only opt-in that may discard unsent composer input is a strict Redmine
#: ``ISSUE:JOURNAL`` pointer. Missing / malformed authority stays zero-close (#13918).
REASON_COMPOSER_DISCARD_APPROVAL_INVALID = "composer_discard_approval_invalid"
#: A pending destructive attempt may only resume under byte-identical verified evidence.
REASON_COMPOSER_DISCARD_APPROVAL_MISMATCH = "composer_discard_approval_mismatch"
#: A historical ``issue_<id>_...`` lane may only use an approval from that same issue.
REASON_COMPOSER_DISCARD_ISSUE_MISMATCH = "composer_discard_issue_mismatch"
#: Historical worktree evidence is action-time authority, never a caller assertion.
REASON_HISTORICAL_WORKTREE_UNREADABLE = "historical_worktree_unreadable"
REASON_HISTORICAL_WORKTREE_DIRTY = "historical_worktree_dirty"
REASON_HISTORICAL_BRANCH_MISMATCH = "historical_branch_mismatch"

#: The idempotent replay: this exact unit was proven retired by a prior run and no expected
#: slot is live now. Exit 0 (design j#80526 rejects an effect-only exit 1).
STATE_ALREADY_RETIRED = "already_retired"

#: The managed-event kinds this surface appends as its durable retirement outcome. It is
#: an audit record, NOT lifecycle authority — capacity is recovered by the panes ceasing to
#: exist (``enumerate_active_lanes`` folds live panes; a record-less unit has disposition
#: ``None`` and stays in the roster until its panes are gone), so this record explains a
#: retirement rather than causing one.
EVENT_COMMAND = "herdr session-retire"
EVENT_KIND_RETIRED = "scratch_pair_retired"

_APPROVAL_RE = re.compile(r"([1-9][0-9]*):([1-9][0-9]*)")
_ISSUE_LANE_RE = re.compile(r"issue_([1-9][0-9]*)(?:_|$)")


@dataclass(frozen=True)
class ComposerDiscardApproval:
    """The explicit durable pointer authorizing loss of unsent composer input."""

    issue: str
    journal: str

    @property
    def token(self) -> str:
        return f"{self.issue}:{self.journal}"


def _composer_discard_approval(
    args: argparse.Namespace,
) -> tuple[Optional[ComposerDiscardApproval], str]:
    # Unlike lane labels, an authority pointer is not a convenience string that may be
    # normalized.  The CLI must preserve one exact, auditable token; surrounding whitespace
    # or any other decoration is malformed rather than silently accepted.
    raw = getattr(args, "pending_composer_discard_approval", "") or ""
    if not raw:
        return None, ""
    match = _APPROVAL_RE.fullmatch(raw)
    if match is None:
        return None, (
            "--pending-composer-discard-approval must be an exact positive "
            "Redmine ISSUE:JOURNAL pointer"
        )
    return ComposerDiscardApproval(issue=match.group(1), journal=match.group(2)), ""


@dataclass(frozen=True)
class SessionRetireVerdict:
    """The fail-closed outcome of a scratch-pair retire.

    ``closed`` / ``durable_retirement`` carry what **actually happened** even when
    ``state`` is blocked: the close precedes the (best-effort) audit append, so a verdict
    must never claim "nothing was closed" once a close committed (the #13842 review j#79363
    R7 factuality rule).
    """

    state: str
    reason: str = ""
    detail: str = ""
    workspace_id: str = ""
    lane_id: str = ""
    expected_names: tuple[str, ...] = field(default_factory=tuple)
    foreign_names: tuple[str, ...] = field(default_factory=tuple)
    closed: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    failed: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    durable_retirement: str = ""
    audit_record: str = ""
    executed: bool = False

    @property
    def ok(self) -> bool:
        # `already_retired` is a proven idempotent replay: exact prior completion + a
        # live-zero measured now. Design j#80526 rejects an effect-only exit 1 for it.
        return self.state in (STATE_GREEN, STATE_ALREADY_RETIRED)

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "expected_names": list(self.expected_names),
            "foreign_names": list(self.foreign_names),
            "closed": [{"role": r, "locator": loc} for r, loc in self.closed],
            "failed": [
                {"role": r, "locator": loc, "detail": d} for r, loc, d in self.failed
            ],
            "durable_retirement": self.durable_retirement,
            "audit_record": self.audit_record,
            "executed": self.executed,
            "retire_ok": self.ok,
        }


def _blocked(reason: str, detail: str = "", **kw) -> SessionRetireVerdict:
    return SessionRetireVerdict(state=STATE_BLOCKED, reason=reason, detail=detail, **kw)


def run_session_retire(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    ops: Optional[SessionRetireOps] = None,
) -> SessionRetireVerdict:
    """Resolve, observe, decide and (only on ``--execute`` + green) close. Fail-closed.

    Read-only by default: without ``--execute`` this reports the verdict and closes
    nothing, so an operator can see what a retire *would* do before doing it.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.workflow_provider_resolution import (  # noqa: E501
        resolve_gateway_provider,
        resolve_worker_provider,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
        herdr_workspace_segment,
    )

    lane_id = _norm_lane(getattr(args, "lane", "") or "")
    if not _norm(getattr(args, "lane", "") or ""):
        return _blocked(
            REASON_LANE_REQUIRED,
            "a scratch pair is named by its lane; --lane is required so the retire can "
            "never resolve a unit the request did not name",
        )
    if lane_id == DEFAULT_LANE:
        return _blocked(
            REASON_LANE_IS_DEFAULT,
            "the default lane hosts the coordinator pair and is never a retire target",
        )

    approval, approval_error = _composer_discard_approval(args)
    if approval_error:
        return _blocked(REASON_COMPOSER_DISCARD_APPROVAL_INVALID, approval_error)

    try:
        resolved_root = Path(repo_root).expanduser().resolve()
    except (OSError, ValueError):
        return _blocked(REASON_NO_REPO_ANCHOR, "the repo root could not be resolved")

    try:
        workspace_id = herdr_workspace_segment(resolved_root)
    except Exception:  # noqa: BLE001
        workspace_id = ""
    if not workspace_id:
        return _blocked(
            REASON_WORKSPACE_UNRESOLVED,
            "the workspace segment could not be resolved from the repo root; the pair's "
            "identity cannot be built, so nothing is closed",
        )

    try:
        gateway = resolve_gateway_provider(str(resolved_root))
        worker = resolve_worker_provider(str(resolved_root))
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            f"the provider binding could not be resolved ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_id,
        )
    if not gateway or not worker:
        return _blocked(
            REASON_PROVIDER_UNRESOLVED,
            "the provider binding did not resolve both a gateway and a worker provider",
            workspace_id=workspace_id,
            lane_id=lane_id,
        )
    expected_roles = (gateway, worker)

    try:
        expected_names = tuple(
            encode_assigned_name(workspace_id, role, lane_id) for role in expected_roles
        )
    except Exception as exc:  # noqa: BLE001
        return _blocked(
            REASON_IDENTITY_UNENCODABLE,
            f"the pair's assigned names could not be encoded ({type(exc).__name__})",
            workspace_id=workspace_id,
            lane_id=lane_id,
        )

    try:
        unit = RetirementUnit(
            workspace_id=workspace_id,
            lane_id=lane_id,
            slot_digest=slot_digest(expected_names),
        )
    except ValueError as exc:
        return _blocked(
            REASON_IDENTITY_UNENCODABLE,
            str(exc),
            workspace_id=workspace_id,
            lane_id=lane_id,
            expected_names=expected_names,
        )

    live_ops = ops or LiveSessionRetireOps(repo_root=resolved_root)
    historical = _ISSUE_LANE_RE.match(lane_id)
    if approval is not None and historical is not None:
        lane_issue = historical.group(1)
        if approval.issue != lane_issue:
            return _blocked(
                REASON_COMPOSER_DISCARD_ISSUE_MISMATCH,
                f"historical lane {lane_id!r} belongs to Redmine #{lane_issue}, but the "
                f"composer-discard approval points to #{approval.issue}",
                workspace_id=workspace_id,
                lane_id=lane_id,
                expected_names=expected_names,
            )
        readable, clean, branch = live_ops.worktree_facts()
        if not readable:
            return _blocked(
                REASON_HISTORICAL_WORKTREE_UNREADABLE,
                "the historical lane's git status / branch could not be read at action time",
                workspace_id=workspace_id,
                lane_id=lane_id,
                expected_names=expected_names,
            )
        if not clean:
            return _blocked(
                REASON_HISTORICAL_WORKTREE_DIRTY,
                "the historical lane worktree is dirty; unsaved work must never be discarded "
                "with its composer",
                workspace_id=workspace_id,
                lane_id=lane_id,
                expected_names=expected_names,
            )
        if branch != lane_id:
            return _blocked(
                REASON_HISTORICAL_BRANCH_MISMATCH,
                f"the historical lane names branch {lane_id!r}, but the worktree is on "
                f"{branch or '<detached>'!r}",
                workspace_id=workspace_id,
                lane_id=lane_id,
                expected_names=expected_names,
            )
    observation = observe_scratch_pair(
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_roles=expected_roles,
    )
    verdict: ScratchPairRetireVerdict = decide_scratch_pair_retire(
        observation,
        expected_roles=expected_roles,
        allow_pending_composer=approval is not None,
    )

    base = dict(
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_names=expected_names,
        foreign_names=tuple(observation.foreign_names),
    )
    if verdict.state == STATE_BLOCKED:
        return _blocked(verdict.reason, verdict.detail, **base)

    live_targets = tuple(verdict.close_targets)
    pair_is_live = bool(live_targets)
    live_by_role = {role: locator for role, locator in live_targets}

    execute = bool(getattr(args, "execute", False))

    # The ISSUE:JOURNAL CLI value is only a locator.  Read the attempt first without writing,
    # choose the exact current/pending pins, then require a fresh credentialed Redmine read of
    # one structured owner approval for THIS unit.  No reserve or close occurs before this.
    prior_hint = None
    if approval is not None or not execute:
        try:
            prior_hint = live_ops.peek_retirement(unit)
        except ScratchRetirementFenceError as exc:
            return _blocked(REASON_RETIREMENT_AUTHORITY_UNAVAILABLE, str(exc), **base)

    approval_evidence: Optional[ComposerDiscardApprovalEvidence] = None
    if approval is not None:
        if prior_hint is not None and prior_hint.pending:
            approval_pins = prior_hint.pinned
        elif pair_is_live:
            approval_pins = live_targets
        elif prior_hint is not None:
            approval_pins = prior_hint.pinned
        else:
            approval_pins = ()
        try:
            approval_evidence = live_ops.composer_discard_approval(
                issue=approval.issue,
                journal=approval.journal,
                workspace_id=workspace_id,
                lane_id=lane_id,
                slot_digest=unit.slot_digest,
                pinned=approval_pins,
            )
        except ComposerDiscardApprovalError as exc:
            return _blocked(
                REASON_COMPOSER_DISCARD_APPROVAL_INVALID,
                str(exc),
                **base,
            )

    # A read-only preflight observes the authority and writes NOTHING — no lock file, no DB,
    # no seal (review j#80523 R3-F4). It must never bootstrap: an authority created by a
    # `--execute`-less run would both break the command's own contract and silently re-create
    # a *lost* store, erasing the evidence of prior retirements.
    if not execute:
        retry_refusal = _approval_retry_refusal(
            prior_hint,
            approval_evidence=approval_evidence,
            base=base,
        )
        if retry_refusal is not None:
            return retry_refusal
        return _preflight_verdict(
            live_ops,
            prior=prior_hint,
            pair_is_live=pair_is_live,
            live_targets=live_targets,
            workspace_id=workspace_id,
            expected_names=expected_names,
            base=base,
            approval_token=(
                approval_evidence.token if approval_evidence is not None else ""
            ),
        )

    # The retirement transaction (design j#80526). Everything from the authority read through
    # the close, the fresh re-measure and the completion happens under ONE exclusive,
    # non-blocking advisory lock: `BEGIN IMMEDIATE` cannot span the close, which is an external
    # process operation, so it alone would let a second caller enter the unit mid-flight.
    try:
        with live_ops.retirement_transaction(unit, live_pair_present=pair_is_live) as txn:
            prior = txn.current()

            retry_refusal = _approval_retry_refusal(
                prior,
                approval_evidence=approval_evidence,
                base=base,
            )
            if retry_refusal is not None:
                return retry_refusal

            if not pair_is_live:
                if prior is not None and prior.completed:
                    try:
                        prior_token = _attempt_approval_token(prior)
                    except ComposerDiscardApprovalError as exc:
                        return _blocked(
                            REASON_RETIREMENT_AUTHORITY_UNAVAILABLE, str(exc), **base
                        )
                    return SessionRetireVerdict(
                        state=STATE_ALREADY_RETIRED,
                        detail=(
                            "this exact pair was proven retired by a prior run and no slot is "
                            "live now"
                            + (
                                f" under Redmine {prior_token}"
                                if prior_token
                                else ""
                            )
                        ),
                        closed=prior.closed,
                        durable_retirement="already_completed",
                        executed=True,
                        **base,
                    )
                if prior is not None and prior.pending:
                    # A crash after the close but before the completion. Repair it: re-measure
                    # the whole unit and, if provably empty, finish the attempt (F2 repair).
                    return _finish_retirement(
                        live_ops,
                        txn,
                        attempt=prior,
                        closed=prior.closed,
                        workspace_id=workspace_id,
                        lane_id=lane_id,
                        expected_roles=expected_roles,
                        expected_names=expected_names,
                        base=base,
                        repaired=True,
                    )
                return _blocked(
                    REASON_RETIRE_EVIDENCE_ABSENT,
                    "no slot of this pair is live, and no durable attempt proves this command "
                    "retired it; refusing to report a retirement it cannot prove (a mistyped "
                    "--lane looks exactly like this). Nothing was closed or written",
                    **base,
                )

            # Resume an in-flight attempt, or open a new one. The reserve comes FIRST — before
            # the obligation read and before any close (review j#80523 R3-F1, design j#80526):
            # publishing the pending intent is what a concurrent dispatch reads to abort its
            # own send. Reading obligations first only ever produced a stale answer, because a
            # dispatch could reserve in the gap and nothing told it to stop.
            if prior is not None and prior.pending:
                attempt = prior
                plan = _resume_plan(prior, live_by_role, base)
                if isinstance(plan, SessionRetireVerdict):
                    return plan
                remaining = plan
            else:
                attempt = txn.reserve(
                    pinned=live_targets,
                    approval_evidence=(
                        approval_evidence.canonical_json()
                        if approval_evidence is not None
                        else ""
                    ),
                )
                remaining = live_targets

            # Now that the pending intent is durable and visible, read what is owed. A dispatch
            # that reserves after this point sees our pending and zero-sends, so a late
            # obligation cannot appear behind our back.
            blocked = _obligation_refusal(live_ops, workspace_id, expected_names, base)
            if blocked is not None:
                return blocked

            if not remaining:
                # Every pinned slot is already positively gone: nothing left to close.
                return _finish_retirement(
                    live_ops,
                    txn,
                    attempt=attempt,
                    closed=attempt.closed,
                    workspace_id=workspace_id,
                    lane_id=lane_id,
                    expected_roles=expected_roles,
                    expected_names=expected_names,
                    base=base,
                    repaired=True,
                )

            result = live_ops.close(workspace_id, lane_id, remaining)
            closed = tuple(attempt.closed) + tuple(getattr(result, "closed", ()) or ())
            failed = tuple(getattr(result, "failed", ()) or ())
            if closed:
                txn.record_progress(attempt_id=attempt.attempt_id, closed=closed)
            if failed:
                return SessionRetireVerdict(
                    state=STATE_BLOCKED,
                    reason=REASON_CLOSE_FAILED,
                    detail=(
                        "one or more slots did not close; the attempt stays pending and a "
                        "re-run resumes the remainder"
                    ),
                    closed=closed,
                    failed=failed,
                    executed=True,
                    **base,
                )
            return _finish_retirement(
                live_ops,
                txn,
                attempt=attempt,
                closed=closed,
                workspace_id=workspace_id,
                lane_id=lane_id,
                expected_roles=expected_roles,
                expected_names=expected_names,
                base=base,
                repaired=False,
            )
    except ScratchRetirementBusy as exc:
        return _blocked(REASON_RETIREMENT_BUSY, str(exc), **base)
    except ScratchRetirementFenceError as exc:
        return _blocked(REASON_RETIREMENT_AUTHORITY_UNAVAILABLE, str(exc), **base)


def _attempt_approval_token(attempt) -> str:
    """Recover the exact durable pointer from the load-bearing attempt evidence."""
    raw = getattr(attempt, "approval_evidence", "") or ""
    if not raw:
        return ""
    return ComposerDiscardApprovalEvidence.from_json(raw).token


def _approval_retry_refusal(prior, *, approval_evidence, base):
    """Require byte-identical verified evidence before resuming a pending attempt."""
    if prior is None or not prior.pending:
        return None
    stored = getattr(prior, "approval_evidence", "") or ""
    current = (
        approval_evidence.canonical_json() if approval_evidence is not None else ""
    )
    if stored:
        try:
            ComposerDiscardApprovalEvidence.from_json(stored)
        except ComposerDiscardApprovalError as exc:
            return _blocked(REASON_RETIREMENT_AUTHORITY_UNAVAILABLE, str(exc), **base)
    if stored == current:
        return None
    if not stored and not current:
        return None
    return _blocked(
        REASON_COMPOSER_DISCARD_APPROVAL_MISMATCH,
        "the pending retirement attempt is bound to different composer-discard approval "
        "evidence; a destructive retry requires the same freshly verified journal bytes",
        **base,
    )


def _resume_plan(attempt, live_by_role, base):
    """The exact slots a RESUMED attempt may close, or a blocked verdict. (R3-F2)

    A resumed attempt's authority is its own ``pinned`` locators — never the pair that happens
    to be live now. herdr assigned names are deterministic in ``(workspace, role, lane)``, so a
    relaunched pair occupies the SAME names at NEW locators; closing "whatever is live at those
    names" would destroy a running pair on the strength of an old attempt (review j#80523 R3-F2,
    reproduced: pins %1/%2, live %11/%22 -> the old attempt closed the new pair).

    Each still-open pin is therefore one of exactly three things:

    - **live at its pinned locator** — this attempt's own slot: closable;
    - **positively absent** (its name resolves to no live slot) — a prior run already closed
      it, or it died: nothing to do, and NOT a block (that is what makes a partial close
      replayable);
    - **live at a DIFFERENT locator** — a relaunch at the same name. The attempt has no
      authority over it, and silently re-pinning would be exactly the destruction above.
    """
    already = set(attempt.closed)
    remaining: list[tuple[str, str]] = []
    drifted: list[str] = []
    for role, locator in attempt.pinned:
        if (role, locator) in already:
            continue
        live_locator = live_by_role.get(role)
        if live_locator is None:
            continue  # positively absent: already gone
        if live_locator == locator:
            remaining.append((role, locator))
            continue
        drifted.append(f"{role}: pinned {locator}, live {live_locator}")
    if drifted:
        return _blocked(
            REASON_PIN_DRIFT,
            "a pending retirement attempt's pinned slot(s) are no longer at the pinned "
            f"locator ({'; '.join(drifted)}); the assigned name matches but the process does "
            "not, so this is a relaunched pair the old attempt has no authority to close",
            closed=attempt.closed,
            **base,
        )
    return tuple(remaining)


def _preflight_verdict(
    live_ops,
    *,
    prior,
    pair_is_live,
    live_targets,
    workspace_id,
    expected_names,
    base,
    approval_token,
):
    """The read-only verdict: report what an execute WOULD do, writing nothing."""
    if not pair_is_live:
        if prior is not None and prior.completed:
            return SessionRetireVerdict(
                state=STATE_ALREADY_RETIRED,
                detail="this exact pair is proven retired and no slot is live",
                closed=prior.closed,
                durable_retirement="already_completed",
                executed=False,
                **base,
            )
        if prior is not None and prior.pending:
            return SessionRetireVerdict(
                state=STATE_GREEN,
                detail=(
                    "a pending retirement attempt is repairable; re-run with --execute to "
                    "re-measure and complete it"
                ),
                executed=False,
                **base,
            )
        return _blocked(
            REASON_RETIRE_EVIDENCE_ABSENT,
            "no slot of this pair is live, and no durable attempt proves this command retired "
            "it; nothing was closed or written",
            **base,
        )
    blocked = _obligation_refusal(live_ops, workspace_id, expected_names, base)
    if blocked is not None:
        return blocked
    detail = f"the pair is retirable; re-run with --execute to close {len(live_targets)} slot(s)"
    if approval_token:
        detail += (
            f" and discard pending composer input under Redmine {approval_token}"
        )
    return SessionRetireVerdict(
        state=STATE_GREEN,
        detail=detail,
        executed=False,
        **base,
    )



def _obligation_refusal(live_ops, workspace_id, expected_names, base):
    """The durable-obligation gate across EVERY covered source, or ``None``.

    Covered (review j#80594 R4-F3): the dispatch outbox and the callback outbox for work owed
    TO these slots, and the forward fence for work owed BY them. `delivered` sends are
    correlated against the durable disposition of the work they handed over rather than being
    blocked forever (R4-F1) or waved through (a delivery ACK is not completion).
    """
    obligations = live_ops.open_obligations(workspace_id, expected_names)
    if obligations is None:
        return _blocked(
            REASON_OBLIGATION_UNREADABLE,
            "a durable obligation store could not be read; an obligation that cannot be "
            "observed is not an obligation that is absent, so nothing is closed",
            **base,
        )
    if obligations:
        shown = "; ".join(o.describe() for o in obligations)
        return _blocked(
            REASON_WORK_OBLIGATION_PRESENT,
            f"durable work is still owed for this pair ({shown})",
            **base,
        )
    return None


def _finish_retirement(
    live_ops,
    txn,
    *,
    attempt,
    closed,
    workspace_id,
    lane_id,
    expected_roles,
    expected_names,
    base,
    repaired,
):
    """Fresh whole-unit re-measure -> fence completion -> best-effort audit -> green.

    The re-measure re-checks the SURFACE SIGNATURE too, not only the inventory (review j#80523
    R2-F3): this rail exists only for a record-less unit, so if a lifecycle record appeared
    mid-flight the unit stopped being this surface's to retire and no completion may be claimed.
    """
    after = observe_scratch_pair(
        live_ops,
        workspace_id=workspace_id,
        lane_id=lane_id,
        expected_roles=expected_roles,
    )
    if not after.inventory_readable:
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_POST_CLOSE_UNREADABLE,
            detail=(
                "the close committed but a fresh inventory could not be read, so the unit's "
                "emptiness is unproven; the attempt stays pending and a re-run repairs it"
            ),
            closed=closed,
            executed=True,
            **base,
        )
    if not after.lifecycle_record_absent:
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_SIGNATURE_LOST,
            detail=(
                "the unit gained a durable lifecycle record during the retire, so it is no "
                "longer this surface's to retire (this rail is only for record-less pairs); "
                "no completion is claimed"
            ),
            closed=closed,
            executed=True,
            **base,
        )
    residue = [slot.assigned_name for slot in after.slots if not slot.absent]
    if residue or after.foreign_names or after.duplicate_slot_keys:
        bits = []
        if residue:
            bits.append(f"expected slot(s) still live: {', '.join(residue)}")
        if after.foreign_names:
            bits.append(f"foreign occupant(s): {', '.join(after.foreign_names)}")
        if after.duplicate_slot_keys:
            bits.append("duplicate canonical slot(s) appeared")
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_POST_CLOSE_RESIDUE,
            detail=(
                "the close committed but the unit is not empty, so this is not a retirement ("
                + "; ".join(bits)
                + ")"
            ),
            closed=closed,
            foreign_names=tuple(after.foreign_names),
            executed=True,
            **{k: v for k, v in base.items() if k != "foreign_names"},
        )
    # An obligation reserved DURING the close would be invisible to the pre-close read alone
    # (review j#80523 R2-F2), so re-read it here, still under the held transaction.
    blocked = _obligation_refusal(live_ops, workspace_id, expected_names, base)
    if blocked is not None:
        return replace(blocked, closed=closed, executed=True)

    try:
        completed = txn.mark_completed(
            attempt_id=attempt.attempt_id,
            closed=closed,
            detail=f"{len(closed)} slot(s) closed; unit re-measured empty",
        )
    except ScratchRetirementFenceError as exc:
        # The load-bearing durable outcome failed. NOT a success — but the closes that
        # committed are reported truthfully, and the pending attempt lets the next run repair.
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_COMPLETION_UNPROVEN,
            detail=(
                f"the panes closed and the unit re-measured empty, but the retirement could "
                f"not be recorded ({exc}); re-run to repair the pending attempt"
            ),
            closed=closed,
            executed=True,
            **base,
        )

    # The fence row IS the durable outcome (acceptance 4). `managed_events` is appended only
    # AFTER it, purely as lossy narrative audit: its failure is reported but never invalidates
    # a proven retirement, because the load-bearing authority is the fence, not the audit log.
    try:
        approval_token = _attempt_approval_token(completed)
    except ComposerDiscardApprovalError as exc:
        return SessionRetireVerdict(
            state=STATE_BLOCKED,
            reason=REASON_RETIREMENT_AUTHORITY_UNAVAILABLE,
            detail=str(exc),
            closed=closed,
            durable_retirement="fence_completed",
            executed=True,
            **base,
        )
    intent = {
        "lane_id": lane_id,
        "expected_names": list(expected_names),
        "closed": [{"role": r, "locator": loc} for r, loc in closed],
        "surface": EVENT_COMMAND,
    }
    if approval_token:
        intent["pending_composer_discard_approval"] = approval_token
    audit = live_ops.record_retirement(
        workspace_id=workspace_id,
        lane_id=lane_id,
        intent=intent,
    )
    detail = f"closed {len(closed)} slot(s); the unit re-measured empty on a fresh inventory"
    if repaired:
        detail = (
            f"repaired a pending attempt: {len(closed)} slot(s) were already closed and the "
            "unit re-measured empty"
        )
    if approval_token:
        detail += f"; pending composer discard approved by Redmine {approval_token}"
    return SessionRetireVerdict(
        state=STATE_GREEN,
        detail=detail,
        closed=closed,
        durable_retirement="fence_completed",
        audit_record=audit,
        executed=True,
        **base,
    )



def format_session_retire_text(result: SessionRetireVerdict) -> str:
    lines = [f"scratch pair retire: {result.state}"]
    if result.reason:
        lines.append(f"  reason: {result.reason}")
    if result.detail:
        lines.append(f"  detail: {result.detail}")
    lines.append(f"  workspace: {result.workspace_id or '<unresolved>'}")
    lines.append(f"  lane: {result.lane_id or '<unresolved>'}")
    for name in result.expected_names:
        lines.append(f"    expected slot: {name}")
    for name in result.foreign_names:
        lines.append(f"    foreign (never closed): {name}")
    for role, locator in result.closed:
        lines.append(f"    closed: {role} @ {locator}")
    for role, locator, detail in result.failed:
        lines.append(f"    close FAILED: {role} @ {locator}: {detail}")
    if result.durable_retirement:
        lines.append(f"  durable retirement: {result.durable_retirement}")
    if result.audit_record and result.audit_record != "recorded":
        # The fence proves the retirement; the audit log is narrative only, so a failed
        # append is reported without invalidating a proven close (design j#80526).
        lines.append(f"  audit record: {result.audit_record} (narrative only; the "
                     f"retirement itself is proven by the fence)")
    if not result.executed and result.state == STATE_GREEN:
        lines.append("  (read-only preflight; nothing was closed)")
    return "\n".join(lines)


__all__ = (
    "EVENT_COMMAND",
    "EVENT_KIND_RETIRED",
    "REASON_NO_REPO_ANCHOR",
    "REASON_WORKSPACE_UNRESOLVED",
    "REASON_LANE_REQUIRED",
    "REASON_LANE_IS_DEFAULT",
    "REASON_PROVIDER_UNRESOLVED",
    "REASON_IDENTITY_UNENCODABLE",
    "REASON_CLOSE_FAILED",
    "REASON_WORK_OBLIGATION_PRESENT",
    "REASON_OBLIGATION_UNREADABLE",
    "REASON_POST_CLOSE_RESIDUE",
    "REASON_POST_CLOSE_UNREADABLE",
    "REASON_RETIRE_EVIDENCE_ABSENT",
    "REASON_RETIREMENT_BUSY",
    "REASON_RETIREMENT_AUTHORITY_UNAVAILABLE",
    "REASON_COMPLETION_UNPROVEN",
    "REASON_SIGNATURE_LOST",
    "REASON_PIN_DRIFT",
    "STATE_ALREADY_RETIRED",
    "SessionRetireVerdict",
    "SessionRetireOps",
    "LiveSessionRetireOps",
    "observe_scratch_pair",
    "run_session_retire",
    "format_session_retire_text",
)
