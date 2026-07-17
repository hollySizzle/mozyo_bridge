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

#: The lane / gate shape does not call for a disposition. Nothing read, nothing written.
LEG_NOT_APPLICABLE = "not_applicable"
#: The writer ran. `detail` carries its state / reason verbatim.
LEG_ATTEMPTED = "attempted"
#: An unexpected error escaped the leg. Zero-write, and SAID so (review j#80659 R7-F1): a bare
#: `None` here made a swallowed exception indistinguishable from "nothing to do".
LEG_ERROR = "error"

REASON_LEG_RAISED = "leg_raised"
#: A dry run: reported, never appended.
REASON_DRY_RUN = "dry_run"

REASON_NOT_GATEWAY_LANE = "not_gateway_lane"
REASON_NO_VERIFIED_ANCHOR = "no_verified_anchor"
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

    @property
    def applicable(self) -> bool:
        """Did this step actually concern a disposition? (drives whether to report at all)"""
        return self.state != LEG_NOT_APPLICABLE or self.reason not in (
            REASON_NOT_GATEWAY_LANE,
            REASON_NO_VERIFIED_ANCHOR,
        )

    def envelope_fields(self) -> dict:
        """The step envelope's ``dispatch_disposition`` object."""
        return {
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
            "wrote": self.wrote,
        }

    def describe(self) -> str:
        """One operator-facing line for the text envelope."""
        if self.wrote:
            return "dispatch disposition: recorded"
        return (
            f"dispatch disposition: NOT recorded ({self.reason or self.state})"
            f"{' — ' + self.detail if self.detail else ''}"
        )


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
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.dispatch_disposition_writer import (  # noqa: E501
        record_dispatch_disposition,
    )

    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (  # noqa: E501
        ROLE_DELEGATED_COORDINATOR,
    )

    if _norm(getattr(outcome, "caller_role", "")) != ROLE_DELEGATED_COORDINATOR:
        # Only the same-lane gateway attests a discharge. A worker stepping its own lane is
        # not a producer, however truthful its own report.
        return DispositionLegResult(LEG_NOT_APPLICABLE, REASON_NOT_GATEWAY_LANE)

    anchor = _norm(getattr(outcome, "durable_anchor", ""))
    issue = _anchor_field(anchor, "issue")
    terminal_journal = _anchor_field(anchor, "journal")
    if not issue or not terminal_journal:
        # An unverified anchor names no round. (The anchor is verified live upstream; an
        # unverified one never reaches a review action either.)
        return DispositionLegResult(LEG_NOT_APPLICABLE, REASON_NO_VERIFIED_ANCHOR)

    if source is None:
        source = _live_source()
    if source is None:
        return DispositionLegResult(LEG_NOT_APPLICABLE, REASON_SOURCE_UNAVAILABLE)

    try:
        entries = list(source.read_entries(issue))
    except Exception as exc:  # noqa: BLE001 - never attest from an unread source
        return DispositionLegResult(
            LEG_NOT_APPLICABLE, REASON_SOURCE_UNREADABLE, detail=str(exc)
        )

    sender = _sender_identity(args)
    if sender is None:
        return DispositionLegResult(LEG_NOT_APPLICABLE, REASON_NO_VERIFIED_ANCHOR)

    round_ = resolve_round_dispatch(
        entries,
        workspace_id=sender.workspace_id,
        lane_id=sender.lane_id,
        terminal_journal=terminal_journal,
    )
    if not round_.ok:
        # Zero or many: either way this producer cannot name the exact action a discharge
        # would close, so it records nothing — but it says WHICH, so the operator can act.
        return DispositionLegResult(
            LEG_NOT_APPLICABLE, round_.reason, detail=round_.detail
        )
    auth = round_.auth

    if append_note is None:
        append_note = _live_append_note()
    if append_note is None:
        return DispositionLegResult(LEG_NOT_APPLICABLE, REASON_NO_WRITE_OPT_IN)

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
    return DispositionLegResult(
        LEG_ATTEMPTED,
        reason=result.reason,
        detail=result.detail or result.state,
        wrote=result.wrote,
    )


def maybe_record_gateway_disposition(
    args: argparse.Namespace, outcome, *, dry_run: bool
) -> Optional[DispositionLegResult]:
    """The ``workflow step`` boundary onto the leg (Redmine #13892 R6-F1).

    ``--dry-run`` reports without writing, like every other leg: a dry run that appended a
    durable marker would not be a dry run.

    Never raises. The leg already fails closed on every unreadable / ambiguous input; this
    boundary additionally refuses to let an unexpected error in a bookkeeping append take down
    the gateway's review action, which is the thing that actually matters.

    An escaped exception becomes a :data:`LEG_ERROR` result, never a bare ``None`` (review
    j#80659 R7-F1): swallowing it made a crashed writer look exactly like a step that had
    nothing to record, and the operator was never told the marker had not landed.
    """
    if dry_run:
        return DispositionLegResult(
            LEG_NOT_APPLICABLE,
            REASON_DRY_RUN,
            detail="a dry run reports without appending a durable marker",
        )
    try:
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
    "LEG_ATTEMPTED",
    "LEG_ERROR",
    "REASON_LEG_RAISED",
    "REASON_DRY_RUN",
    "RoundResolution",
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
