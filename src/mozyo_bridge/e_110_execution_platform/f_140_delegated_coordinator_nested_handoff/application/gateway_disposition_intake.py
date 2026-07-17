"""The gateway-side production caller of the canonical disposition writer (Redmine #13892).

Review j#80644 R6-F1 / its scope ruling: ``record_dispatch_disposition`` had no production
caller at all, so no ``dispatch-disposition`` marker could ever exist in live Redmine, so every
``delivered`` dispatch row stayed permanently ``owed`` and the over-block j#80629 was designed
to remove was never actually removed. A writer only tests can reach is not a rail.

This is the composition root the writer's docstring promised. It rides the **gateway-owned**
``workflow step`` path, which is where a same-lane ``implementation_gateway`` verifies its
lane's anchor against source-of-truth Redmine and proceeds toward its review action — the
integration point the ruling named. It is a **leg**, fired from ``cmd_workflow_step`` after the
outcome resolves, never from :func:`resolve_herdr_step_outcome`: that resolver's contract is
resolution-only ("never mutates a lane or delivers anything"), and a Redmine append is a
mutation. The forward / dispatch / startup-resume legs have the same shape.

Only the gateway records. A worker's own gate write, a CallbackOutbox delivery and a pane ACK
are all explicitly NOT producers (j#80629): none of them can attest that the round terminated.

Everything fails closed and zero-write:

* not a gateway lane, or no verified anchor -> nothing to record, no read;
* the anchor journal carries no canonical ``review_request`` -> the writer refuses
  (``terminal_gate_not_found``): ``implementation_done`` is routinely partial and does not
  terminate a round;
* the round's dispatch AUTHORIZE is missing, ambiguous, or of foreign identity -> refused
  rather than guessed;
* no live source / no write opt-in -> refused, never a marker this producer cannot justify.

The leg is fail-soft for the *step*: a refusal is surfaced, never raised, because the gateway's
review action must not be blocked by a bookkeeping append. But it is never fail-open for the
*record*: a refusal writes nothing, and the reader keeps blocking, which is the safe direction.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.dispatch_disposition_writer import (  # noqa: E501
    WRITE_ALREADY_RECORDED,
    WRITE_RECORDED,
    WRITE_REFUSED,
)

#: This step is not a gateway round at all — no disposition is owed. Silent (nothing read,
#: nothing written, no envelope field): the ONLY states that may be silent.
LEG_NOT_APPLICABLE = "not_applicable"
#: The marker was appended.
LEG_RECORDED = "recorded"
#: An identical marker already existed. The writer's contract calls this a **success**
#: (``WRITE_ALREADY_RECORDED`` / ``ok``), so it must not read as a refusal (review j#80667
#: R8-F2): a same-payload replay is exactly what an idempotent producer is supposed to do.
LEG_ALREADY_RECORDED = "already_recorded"
#: The leg or the writer refused. Zero-write, always reported.
LEG_REFUSED = "refused"
#: A gateway round on a dry run: reported, never appended.
LEG_DRY_RUN = "dry_run"
#: An unexpected error escaped the leg. Zero-write, and SAID so (review j#80659 R7-F1): a bare
#: `None` here made a swallowed exception indistinguishable from "nothing to do".
LEG_ERROR = "error"

REASON_LEG_RAISED = "leg_raised"
REASON_DRY_RUN = "dry_run"

REASON_NOT_GATEWAY_LANE = "not_gateway_lane"
REASON_NO_VERIFIED_ANCHOR = "no_verified_anchor"
#: The gateway round is real and the anchor verified — but the launch-time herdr identity that
#: supplies workspace / lane could not be resolved (review j#80667 R8-F1). Distinct from
#: :data:`REASON_NO_VERIFIED_ANCHOR`, which reports a precondition that is genuinely absent;
#: conflating them named the wrong cause AND made the refusal silent.
REASON_SENDER_UNRESOLVED = "sender_identity_unresolved"
REASON_SOURCE_UNAVAILABLE = "source_unavailable"
REASON_SOURCE_UNREADABLE = "source_unreadable"
REASON_NO_WRITE_OPT_IN = "write_opt_in_unset"
REASON_DISPATCH_NOT_FOUND = "dispatch_authorize_not_found"
REASON_DISPATCH_AMBIGUOUS = "dispatch_authorize_ambiguous"


def _norm(value) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DispositionLegResult:
    """What the leg did. Reported alongside the step outcome; never raised.

    This is surfaced on the ``workflow step`` envelope, not merely returned (review j#80659
    R7-F1). A refusal the operator cannot see is a silent zero-write: the marker never lands,
    the delivered row stays owed forever, and once the review result posts, the verified anchor
    moves past this round so nothing will ever retry it. Fail-closed is only safe when someone
    is told.
    """

    state: str
    reason: str = ""
    detail: str = ""
    wrote: bool = False
    #: Did this step concern a gateway round at all? An explicit FACT set by the one place that
    #: knows — never re-derived from `reason` (review j#80667 R8-F1). Inferring it from a reason
    #: allowlist meant any new refusal reason that happened to reuse a listed string silently
    #: vanished from the envelope, which is precisely the failure R7-F1 was meant to end.
    applicable: bool = True

    @property
    def ok(self) -> bool:
        """Did the round reach its intended durable state?

        A same-payload replay (:data:`LEG_ALREADY_RECORDED`) is a **success**, matching the
        writer's own ``ok`` (review j#80667 R8-F2). Reporting it as a refusal inverted the
        idempotency contract for every operator and automation reading this envelope.
        """
        return self.state in (LEG_RECORDED, LEG_ALREADY_RECORDED)

    def envelope_fields(self) -> dict:
        """The step envelope's ``dispatch_disposition`` object.

        ``state`` carries the writer's own semantic answer and ``ok`` makes an idempotent
        replay machine-checkable — a bare ``wrote`` bool could not distinguish "already
        recorded" (fine) from "refused" (not fine).
        """
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "wrote": self.wrote,
            "ok": self.ok,
        }

    def describe(self) -> str:
        """One operator-facing line for the text envelope."""
        tail = f" — {self.detail}" if self.detail else ""
        if self.state == LEG_RECORDED:
            return "dispatch disposition: recorded"
        if self.state == LEG_ALREADY_RECORDED:
            return f"dispatch disposition: already recorded (idempotent replay){tail}"
        if self.state == LEG_DRY_RUN:
            return "dispatch disposition: not recorded (dry run)"
        return f"dispatch disposition: NOT recorded ({self.reason or self.state}){tail}"


@dataclass(frozen=True)
class RoundResolution:
    """Which dispatch round a terminal gate closes — or WHY that could not be decided.

    Carries the reason rather than collapsing every failure to ``None`` (review j#80659
    R7-F1): "no dispatch opens this round" and "two do" are different operator situations,
    and reporting the first as an ambiguity sends them hunting a duplicate that isn't there.
    """

    auth: object = None
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.auth is not None


def _anchor_field(anchor_pointer: str, field: str) -> str:
    """Pull ``issue`` / ``journal`` out of a verified ``redmine:issue=<id>:journal=<id>``."""
    s = _norm(anchor_pointer)
    if not s or s == "none" or not s.startswith("redmine:"):
        return ""
    for part in s.split(":"):
        part = part.strip()
        if part.startswith(f"{field}="):
            return part[len(field) + 1 :].strip()
    return ""


def resolve_round_dispatch(
    entries: Sequence,
    *,
    workspace_id: str,
    lane_id: str,
    terminal_journal: str,
) -> RoundResolution:
    """The ONE dispatch AUTHORIZE the ``review_request`` at ``terminal_journal`` terminates.

    A lane runs many rounds, each shaped ``AUTHORIZE -> ... -> review_request``. So the round
    this terminal gate closes is the valid AUTHORIZE for this exact lane that opened **after
    the previous review_request** and **before this one**. Anything earlier belongs to a round
    its own review_request already closed.

    Cardinality is the answer, not an obstacle (review j#80644 R6-F2): exactly one candidate
    resolves. Never "pick the latest" — two AUTHORIZE markers in one round is a real ambiguity
    about which action a discharge would name.

    Returns a :class:`RoundResolution` carrying either the auth or the reason it could not be
    decided — :data:`REASON_DISPATCH_NOT_FOUND` for zero, :data:`REASON_DISPATCH_AMBIGUOUS` for
    two-plus. Both are zero-write, and they are NOT the same answer (review j#80659 R7-F1).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (  # noqa: E501
        parse_dispatch_authorizations,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
        extract_markers,
    )

    order = {_norm(getattr(e, "journal_id", "")): i for i, e in enumerate(entries)}
    terminal_pos = order.get(_norm(terminal_journal))
    if terminal_pos is None:
        return RoundResolution(
            reason=REASON_DISPATCH_NOT_FOUND,
            detail=f"the terminal journal {terminal_journal} is not in the read history",
        )

    prior_terminal_pos = -1
    for m in extract_markers(entries):
        if _norm(getattr(m, "gate", "")) != "review_request":
            continue
        pos = order.get(_norm(getattr(m, "journal", "")))
        if pos is None or pos >= terminal_pos:
            continue
        prior_terminal_pos = max(prior_terminal_pos, pos)

    candidates = []
    for auth in parse_dispatch_authorizations(entries):
        if not getattr(auth, "valid", False):
            continue
        if _norm(getattr(auth, "workspace_id", "")) != _norm(workspace_id):
            continue
        if _norm(getattr(auth, "lane_id", "")) != _norm(lane_id):
            continue
        pos = order.get(_norm(getattr(auth, "journal", "")))
        if pos is None or not (prior_terminal_pos < pos < terminal_pos):
            continue
        candidates.append(auth)
    # 0 and 2+ are DIFFERENT answers and must not collapse (review j#80659 R7-F1): "this round
    # has no dispatch to discharge" and "this round has two and I cannot tell which" are
    # distinct operator situations. Both are zero-write, but reporting the first as ambiguity
    # sends the operator looking for a duplicate that does not exist.
    if not candidates:
        return RoundResolution(
            reason=REASON_DISPATCH_NOT_FOUND,
            detail=(
                f"no valid dispatch AUTHORIZE for lane {lane_id} opens the round terminated "
                f"by j#{terminal_journal}"
            ),
        )
    if len(candidates) > 1:
        return RoundResolution(
            reason=REASON_DISPATCH_AMBIGUOUS,
            detail=(
                f"{len(candidates)} valid dispatch AUTHORIZE markers open the round "
                f"terminated by j#{terminal_journal}; refusing to guess which one it closes"
            ),
        )
    return RoundResolution(auth=candidates[0])


def gateway_round_of(outcome) -> tuple:
    """``(issue, terminal_journal)`` iff this step IS a gateway round, else ``("", "")``.

    Applicability is decided here and ONLY here, from the role and the anchor — the two facts
    that determine whether a disposition is owed at all. Everything downstream (dry run,
    source, sender, cardinality, write opt-in) concerns an applicable round and is therefore
    always reported (review j#80667 R8-F1 / R8-F3).
    """
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E501
        ROLE_DELEGATED_COORDINATOR,
    )

    if _norm(getattr(outcome, "caller_role", "")) != ROLE_DELEGATED_COORDINATOR:
        # Only the same-lane gateway attests a discharge. A worker stepping its own lane is
        # not a producer, however truthful its own report.
        return ("", "")
    anchor = _norm(getattr(outcome, "durable_anchor", ""))
    return (_anchor_field(anchor, "issue"), _anchor_field(anchor, "journal"))


def execute_gateway_disposition_leg(
    args: argparse.Namespace,
    outcome,
    *,
    source=None,
    append_note=None,
) -> DispositionLegResult:
    """Record the disposition of the round this gateway lane's ``review_request`` terminated.

    ``source`` / ``append_note`` default to the live, credential-gated compositions and are
    injectable so the contract can be driven end to end without a network.

    Past the applicability gate, EVERY return is ``applicable=True``: an applicable round that
    records nothing must say so, whatever the cause.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.dispatch_disposition_writer import (  # noqa: E501
        record_dispatch_disposition,
    )

    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E501
        ROLE_DELEGATED_COORDINATOR,
    )

    issue, terminal_journal = gateway_round_of(outcome)
    if not issue or not terminal_journal:
        is_gateway = (
            _norm(getattr(outcome, "caller_role", "")) == ROLE_DELEGATED_COORDINATOR
        )
        return DispositionLegResult(
            LEG_NOT_APPLICABLE,
            REASON_NO_VERIFIED_ANCHOR if is_gateway else REASON_NOT_GATEWAY_LANE,
            applicable=False,
        )

    def _refused(reason: str, detail: str = "") -> DispositionLegResult:
        """An applicable round that will not be recorded. Zero-write, never silent."""
        return DispositionLegResult(LEG_REFUSED, reason, detail=detail)

    if source is None:
        source = _live_source()
    if source is None:
        return _refused(
            REASON_SOURCE_UNAVAILABLE,
            "no credentialed live journal source; a discharge cannot be attested",
        )

    try:
        entries = list(source.read_entries(issue))
    except Exception as exc:  # noqa: BLE001 - never attest from an unread source
        return _refused(REASON_SOURCE_UNREADABLE, str(exc))

    sender = _sender_identity(args)
    if sender is None:
        # The anchor is verified and the round is real; what failed is the launch-time herdr
        # identity supplying workspace / lane. Reporting this as `no_verified_anchor` named the
        # wrong cause AND (because that reason was treated as non-applicable) hid the
        # zero-write entirely — R7-F1's defect in a second shape (review j#80667 R8-F1).
        return _refused(
            REASON_SENDER_UNRESOLVED,
            "the launch-time herdr sender identity (workspace / lane) could not be resolved",
        )

    round_ = resolve_round_dispatch(
        entries,
        workspace_id=sender.workspace_id,
        lane_id=sender.lane_id,
        terminal_journal=terminal_journal,
    )
    if not round_.ok:
        # Zero or many: either way this producer cannot name the exact action a discharge
        # would close, so it records nothing — but it says WHICH, so the operator can act.
        return _refused(round_.reason, round_.detail)
    auth = round_.auth

    if append_note is None:
        append_note = _live_append_note()
    if append_note is None:
        return _refused(
            REASON_NO_WRITE_OPT_IN,
            "the live Redmine write opt-in (MOZYO_REDMINE_DELIVERY_WRITE) is unset, so the "
            "marker cannot be appended and this round stays owed",
        )

    result = record_dispatch_disposition(
        issue=issue,
        dispatch_journal=_norm(auth.journal),
        terminal_journal=terminal_journal,
        workspace_id=_norm(auth.workspace_id),
        lane_id=_norm(auth.lane_id),
        target_assigned_name=_norm(auth.target_assigned_name),
        action_id=_norm(auth.action_id),
        source=source,
        append_note=append_note,
    )
    # Carry the writer's OWN semantic answer through (review j#80667 R8-F2). Collapsing its
    # three states into a `wrote` bool made an idempotent replay — which the writer's contract
    # calls a success — render as `NOT recorded`, inverting that contract on the envelope.
    return DispositionLegResult(
        _WRITER_STATE_TO_LEG.get(result.state, LEG_REFUSED),
        reason=result.reason,
        detail=result.detail,
        wrote=result.wrote,
    )


#: The writer's states are the authority on what happened; this leg only relabels them.
_WRITER_STATE_TO_LEG = {
    WRITE_RECORDED: LEG_RECORDED,
    WRITE_ALREADY_RECORDED: LEG_ALREADY_RECORDED,
    WRITE_REFUSED: LEG_REFUSED,
}


def maybe_record_gateway_disposition(
    args: argparse.Namespace, outcome, *, dry_run: bool
) -> Optional[DispositionLegResult]:
    """The ``workflow step`` boundary onto the leg (Redmine #13892 R6-F1).

    ``--dry-run`` reports without writing, like every other leg: a dry run that appended a
    durable marker would not be a dry run.

    **Applicability is decided before the dry run** (review j#80667 R8-F3). Answering dry_run
    first meant a worker's or default coordinator's dry run — steps that concern no disposition
    whatsoever — grew a `dispatch_disposition` field, breaking the additive contract this leg
    is supposed to honour. Whether a disposition is owed depends on the role and the anchor,
    never on how the step was invoked.

    Never raises. The leg already fails closed on every unreadable / ambiguous input; this
    boundary additionally refuses to let an unexpected error in a bookkeeping append take down
    the gateway's review action, which is the thing that actually matters.

    An escaped exception becomes a :data:`LEG_ERROR` result, never a bare ``None`` (review
    j#80659 R7-F1): swallowing it made a crashed writer look exactly like a step that had
    nothing to record, and the operator was never told the marker had not landed.
    """
    try:
        issue, terminal_journal = gateway_round_of(outcome)
        if not issue or not terminal_journal:
            return None  # not a gateway round: silent, and the same on a dry run
        if dry_run:
            return DispositionLegResult(
                LEG_DRY_RUN,
                REASON_DRY_RUN,
                detail="a dry run reports without appending a durable marker",
            )
        return execute_gateway_disposition_leg(args, outcome)
    except Exception as exc:  # noqa: BLE001 - never blocks the review action, but never silent
        return DispositionLegResult(LEG_ERROR, REASON_LEG_RAISED, detail=str(exc))


def disposition_payload_fields(result: Optional[DispositionLegResult]) -> dict:
    """The step JSON envelope's additive ``dispatch_disposition`` field, or ``{}``.

    Additive and always present once the leg applied, mirroring how the store reconcile
    contributes to the same envelope. A step that never concerned a disposition (a worker
    lane, an unverified anchor) contributes nothing, so ordinary output is unchanged.
    """
    if result is None or not result.applicable:
        return {}
    return {"dispatch_disposition": result.envelope_fields()}


def disposition_text_lines(result: Optional[DispositionLegResult]) -> list:
    """The step text envelope's disposition line(s), or ``()``.

    A NOT-recorded outcome must be legible to the operator reading the terminal — the whole
    point of R7-F1 is that a silent zero-write is unrecoverable once the anchor moves on.
    """
    if result is None or not result.applicable:
        return []
    return [result.describe()]


def _live_source():
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (  # noqa: E501
        LiveRedmineJournalSource,
    )

    try:
        return LiveRedmineJournalSource.from_environment()
    except Exception:  # noqa: BLE001 - no credentials -> no attestation
        return None


def _live_append_note():
    """The credential-gated one-shot note append, or ``None`` when the write opt-in is unset.

    Same transport and same opt-in (``MOZYO_REDMINE_DELIVERY_WRITE``) every other governed
    Redmine writer rides — this adds no second write path.
    """
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (  # noqa: E501
        redmine_delivery_transport_from_env,
    )

    try:
        transport = redmine_delivery_transport_from_env()
    except Exception:  # noqa: BLE001
        return None
    if transport is None:
        return None

    def _append(issue: str, note: str) -> None:
        transport.post_issue_note(str(issue), note)

    return _append


def _sender_identity(args: argparse.Namespace):
    import os

    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
        resolve_sender_identity,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.herdr_workflow_step import (  # noqa: E501
        _anchor_workspace_id,
    )

    try:
        repo_root = repo_root_from_args(args)
        res = resolve_sender_identity(
            os.environ, anchor_workspace_id=_anchor_workspace_id(repo_root)
        )
    except Exception:  # noqa: BLE001
        return None
    if not res.ok or res.identity is None:
        return None
    return res.identity


__all__ = (
    "LEG_NOT_APPLICABLE",
    "LEG_RECORDED",
    "LEG_ALREADY_RECORDED",
    "LEG_REFUSED",
    "LEG_DRY_RUN",
    "LEG_ERROR",
    "REASON_LEG_RAISED",
    "REASON_DRY_RUN",
    "REASON_SENDER_UNRESOLVED",
    "RoundResolution",
    "gateway_round_of",
    "REASON_NOT_GATEWAY_LANE",
    "REASON_NO_VERIFIED_ANCHOR",
    "REASON_SOURCE_UNAVAILABLE",
    "REASON_SOURCE_UNREADABLE",
    "REASON_NO_WRITE_OPT_IN",
    "REASON_DISPATCH_NOT_FOUND",
    "REASON_DISPATCH_AMBIGUOUS",
    "DispositionLegResult",
    "resolve_round_dispatch",
    "execute_gateway_disposition_leg",
    "maybe_record_gateway_disposition",
    "disposition_payload_fields",
    "disposition_text_lines",
)
