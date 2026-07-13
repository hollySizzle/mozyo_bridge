"""Standard ticketless no-anchor callback / hands-off payload (Redmine #12703).

GK3500 exploratory smoke #12698 (rerun after docs commit `0da8e476`) surfaced the
fourth ticketless blocker: a receiver that had read the workflow contract docs
produced a structured ``no_dispatch`` hands-off result, but had **no
product-standard primitive to return it to the caller lane**. The standard
``handoff reply`` / ``reply`` rail requires a Redmine anchor (`--issue` +
`--journal`), so the ticketless consultation callback failed closed with
``invalid_anchor``; the only fallback was the low-level ``mozyo-bridge message``
operator/debug transport, which is not a product-standard callback route.

This module is the pure, fail-closed source of truth for the **structured
ticketless callback payload** that the standard no-anchor callback rail carries.
It is the workflow *result* (classification / dispatch decision / next owner /
reason / read-contract), recorded distinctly from the *transport* outcome
(``DeliveryOutcome``). Design boundaries (Redmine #12703 description / j#66959):

- A ticketless callback never carries — and never requires — a Redmine
  issue/journal anchor. Fabricating an anchor to satisfy the reply rail is the
  issue's explicit prohibition. The structured fields below ARE the durable
  record of the consultation result.
- The child -> grandchild worker-dispatch anchor requirement is **not relaxed**:
  a ``dispatch_decision`` that names an actual worker execution / domain probe /
  implementation dispatch (:data:`ANCHOR_REQUIRED_DISPATCH_DECISIONS`) fails
  closed here with a pointer to the anchored ``handoff send`` rail. Only the
  consultation-phase / hands-off decisions in
  :data:`TICKETLESS_DISPATCH_DECISIONS` may ride this rail.
- Every field is a fixed lower-snake-case token (or a derived bool) with no
  operator free text, so the whole payload is durable-record safe and may be
  persisted verbatim, like the :mod:`...domain.transition_role` /
  :mod:`...domain.workflow_contract` boundaries it travels beside. ``summary``
  free text stays on the transport notification body, never in this payload.
- Construction fails closed: an unknown classification / dispatch / owner /
  reason / read-contract token raises :class:`TicketlessCallbackError`, and an
  ``redmine_anchor_required`` value that contradicts the classification/dispatch
  cannot be built. Omitting the payload is the explicit fallback of no callback
  binding.

``redmine_anchor_required`` is *derived* from the classification + dispatch so a
caller cannot build an incoherent payload: it is ``True`` when the result class
is ``anchor_required`` or the dispatch is
``anchor_required_before_worker_dispatch`` (the consultation decided an
implementation/worker phase is needed next, which the caller must mint a real
Redmine anchor for), and ``False`` otherwise. The bool is still a carried field
(round-trips through :func:`ticketless_callback_from_payload`) so a receiver
reads it without re-deriving it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)


class TicketlessCallbackError(ValueError):
    """A ticketless callback payload could not be built or is malformed."""


# --- Classification tokens — the workflow-result class the callback carries. ---
# These are the four ticketless consultation-phase results the issue calls out as
# returnable without a Redmine anchor.
CLASSIFICATION_CONSULTATION_RESULT = "consultation_result"
CLASSIFICATION_NO_DISPATCH = "no_dispatch"
CLASSIFICATION_BLOCKED = "blocked"
CLASSIFICATION_ANCHOR_REQUIRED = "anchor_required"

CLASSIFICATIONS: tuple[str, ...] = (
    CLASSIFICATION_CONSULTATION_RESULT,
    CLASSIFICATION_NO_DISPATCH,
    CLASSIFICATION_BLOCKED,
    CLASSIFICATION_ANCHOR_REQUIRED,
)

# --- Dispatch-decision tokens. ---
# Safe on the ticketless no-anchor rail: no worker execution is started and no
# Redmine anchor is minted. ``anchor_required_before_worker_dispatch`` is the
# decision "an implementation/worker phase IS needed next, but the caller must
# create the Redmine anchor first" — the callback itself stays anchor-free.
DISPATCH_NO_DISPATCH = "no_dispatch"
DISPATCH_HAND_BACK_TO_CALLER = "hand_back_to_caller"
DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER = "anchor_required_before_worker_dispatch"

TICKETLESS_DISPATCH_DECISIONS: tuple[str, ...] = (
    DISPATCH_NO_DISPATCH,
    DISPATCH_HAND_BACK_TO_CALLER,
    DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER,
)

# Forbidden on the ticketless rail: each is an actual child -> grandchild worker
# execution / domain probe / implementation dispatch, which the boundary requires
# a real Redmine anchor for. A ticketless callback naming one fails closed; the
# caller must use the anchored ``handoff send`` rail instead (#12703 禁止: do not
# relax the child -> grandchild worker-dispatch anchor requirement).
DISPATCH_REDMINE_ANCHORED_WORKER = "dispatch_redmine_anchored_worker"
DISPATCH_DOMAIN_PROBE = "domain_probe"
DISPATCH_IMPLEMENTATION = "implementation_dispatch"

ANCHOR_REQUIRED_DISPATCH_DECISIONS: tuple[str, ...] = (
    DISPATCH_REDMINE_ANCHORED_WORKER,
    DISPATCH_DOMAIN_PROBE,
    DISPATCH_IMPLEMENTATION,
)

# --- Workflow next-action owner tokens. ---
# The owner of the WORKFLOW next step, distinct from the transport-layer
# ``next_action_owner`` on ``DeliveryOutcome`` (receiver/sender/operator). Kept
# separate so the transport outcome and the workflow result never collapse.
OWNER_CALLER = "caller"
OWNER_GATEWAY = "gateway"
OWNER_WORKER = "worker"
OWNER_OPERATOR = "operator"

NEXT_ACTION_OWNERS: tuple[str, ...] = (
    OWNER_CALLER,
    OWNER_GATEWAY,
    OWNER_WORKER,
    OWNER_OPERATOR,
)

# --- Callback reason tokens (fixed, durable-record safe). ---
REASON_CONSULTATION_CLASSIFIED = "consultation_classified"
REASON_NO_DISPATCH_DECIDED = "no_dispatch_decided"
REASON_BLOCKED_PENDING_DECISION = "blocked_pending_decision"
REASON_ANCHOR_REQUIRED_FOR_WORKER = "anchor_required_for_worker_dispatch"

CALLBACK_REASONS: tuple[str, ...] = (
    REASON_CONSULTATION_CLASSIFIED,
    REASON_NO_DISPATCH_DECIDED,
    REASON_BLOCKED_PENDING_DECISION,
    REASON_ANCHOR_REQUIRED_FOR_WORKER,
)

# --- Read-contract tokens: which workflow-contract set the receiver should have
# read / re-read. Pinned to the #12700 / #12706 transition-role tokens so a
# receiver can resolve the named contract bundle (the callback names WHICH
# contract governed the result, not a doc body). ---
READ_CONTRACT_TOKENS: tuple[str, ...] = (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)


def _clean_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketlessCallbackError(
            f"ticketless callback {field} must be a non-empty token; got {value!r}"
        )
    return value.strip()


def _clean_choice(value: object, choices: tuple[str, ...], *, field: str) -> str:
    token = _clean_token(value, field=field)
    if token not in choices:
        raise TicketlessCallbackError(
            f"unknown ticketless callback {field}: {token!r}; expected one of "
            f"{list(choices)}"
        )
    return token


def _derive_anchor_required(classification: str, dispatch_decision: str) -> bool:
    """Derive ``redmine_anchor_required`` from the classification + dispatch.

    ``True`` exactly when the consultation decided an implementation / worker
    phase is needed next (the result class ``anchor_required`` or the dispatch
    ``anchor_required_before_worker_dispatch``). The callback itself stays
    anchor-free; this flag tells the caller the *next* phase needs a real anchor.
    """
    return (
        classification == CLASSIFICATION_ANCHOR_REQUIRED
        or dispatch_decision == DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER
    )


@dataclass(frozen=True)
class TicketlessCallback:
    """Structured ticketless no-anchor callback / hands-off result (#12703).

    The five required tokens name the workflow result; ``redmine_anchor_required``
    is derived (and re-validated on round-trip). All fields are fixed tokens / a
    bool with no operator free text, so a payload is durable-record safe in full
    and is recorded distinctly from the transport ``DeliveryOutcome``.

    Construction fails closed on an unknown token, on a ``dispatch_decision`` that
    is an anchored worker dispatch (which may not ride this rail), and on an
    explicit ``redmine_anchor_required`` that contradicts the derived value.
    """

    classification: str
    dispatch_decision: str
    next_action_owner: str
    callback_reason: str
    read_contract: str
    redmine_anchor_required: Optional[bool] = None
    #: Echoed opaque forward generation correlation id (Redmine #13583 R1-F1); ``""`` for a plain
    #: ticketless callback. When a herdr forward carried a ``forward_action_id``, the returning
    #: callback echoes it so the forward's exact generation can be completed on positive delivery.
    forward_action_id: str = ""

    def __post_init__(self) -> None:
        classification = _clean_choice(
            self.classification, CLASSIFICATIONS, field="classification"
        )
        object.__setattr__(self, "classification", classification)

        decision = _clean_token(self.dispatch_decision, field="dispatch_decision")
        if decision in ANCHOR_REQUIRED_DISPATCH_DECISIONS:
            raise TicketlessCallbackError(
                f"dispatch_decision {decision!r} is a Redmine-anchored worker "
                "dispatch and cannot ride the ticketless no-anchor callback rail; "
                "mint a Redmine issue/journal and use `handoff send --kind "
                "implementation_request --source redmine --issue <id> --journal "
                "<id>` (the child -> grandchild worker-dispatch anchor requirement "
                "is not relaxed)"
            )
        if decision not in TICKETLESS_DISPATCH_DECISIONS:
            raise TicketlessCallbackError(
                f"unknown ticketless dispatch_decision {decision!r}; expected one "
                f"of {list(TICKETLESS_DISPATCH_DECISIONS)}"
            )
        object.__setattr__(self, "dispatch_decision", decision)

        object.__setattr__(
            self,
            "next_action_owner",
            _clean_choice(
                self.next_action_owner, NEXT_ACTION_OWNERS, field="next_action_owner"
            ),
        )
        object.__setattr__(
            self,
            "callback_reason",
            _clean_choice(
                self.callback_reason, CALLBACK_REASONS, field="callback_reason"
            ),
        )
        object.__setattr__(
            self,
            "read_contract",
            _clean_choice(
                self.read_contract, READ_CONTRACT_TOKENS, field="read_contract"
            ),
        )

        derived = _derive_anchor_required(classification, decision)
        if self.redmine_anchor_required is None:
            object.__setattr__(self, "redmine_anchor_required", derived)
        else:
            if not isinstance(self.redmine_anchor_required, bool):
                raise TicketlessCallbackError(
                    "ticketless callback redmine_anchor_required must be a bool; "
                    f"got {self.redmine_anchor_required!r}"
                )
            if self.redmine_anchor_required != derived:
                raise TicketlessCallbackError(
                    "ticketless callback redmine_anchor_required="
                    f"{self.redmine_anchor_required} is incoherent with "
                    f"classification={classification!r} / "
                    f"dispatch_decision={decision!r} (derived {derived})"
                )

        object.__setattr__(
            self,
            "forward_action_id",
            str(self.forward_action_id).strip() if self.forward_action_id is not None else "",
        )

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free fields for the handoff callback payload."""
        payload: dict[str, object] = {
            "classification": self.classification,
            "dispatch_decision": self.dispatch_decision,
            "next_action_owner": self.next_action_owner,
            "callback_reason": self.callback_reason,
            "read_contract": self.read_contract,
            "redmine_anchor_required": bool(self.redmine_anchor_required),
        }
        if self.forward_action_id:
            payload["forward_action_id"] = self.forward_action_id
        return payload

    def marker_fields(self) -> list[tuple[str, str]]:
        """Marker key/value pairs for the ticketless landing marker.

        Only the classification + dispatch ride the greppable landing marker (the
        full structured result is in the durable delivery record); both are fixed
        tokens, so the marker stays deterministic and durable-record safe.
        """
        return [
            ("classification", self.classification),
            ("dispatch", self.dispatch_decision),
        ]

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full structured result stays in the durable delivery record. Names
        the classification, dispatch, anchor obligation, owner, reason, and the
        read contract, and points at the durable record.
        """
        anchor_state = (
            "redmine anchor required for the next worker phase"
            if self.redmine_anchor_required
            else "no redmine anchor required"
        )
        return (
            f"ticketless callback: classification {self.classification}, dispatch "
            f"{self.dispatch_decision} ({anchor_state}); workflow next owner "
            f"{self.next_action_owner}; reason {self.callback_reason}; read "
            f"contract {self.read_contract}; the structured callback fields are "
            "the durable delivery record (no Redmine anchor was fabricated)"
        )

    def record_lines(self) -> list[str]:
        """Full durable-record block: every structured callback field.

        Fixed tokens only, so it is rendered in place and the receiver reads the
        consultation result it must act on without re-reading the pane.
        """
        return [
            f"- Ticketless callback: classification `{self.classification}`, "
            f"dispatch `{self.dispatch_decision}`",
            "  - Redmine anchor required (next worker phase): "
            f"`{str(bool(self.redmine_anchor_required)).lower()}`",
            f"  - Workflow next-action owner: `{self.next_action_owner}`",
            f"  - Callback reason: `{self.callback_reason}`",
            f"  - Read contract: `{self.read_contract}`",
        ]


def ticketless_callback_from_payload(
    payload: Mapping[str, object],
) -> TicketlessCallback:
    """Rebuild a callback from a structured payload (round-trips to_structured_dict).

    Fails closed (:class:`TicketlessCallbackError`) on a missing / malformed
    field, so a receiver that parses the callback payload cannot silently accept a
    partial result. The ``redmine_anchor_required`` value is re-validated against
    the classification/dispatch, so a tampered payload cannot smuggle an
    incoherent anchor obligation.
    """
    try:
        classification = payload["classification"]
        dispatch_decision = payload["dispatch_decision"]
        next_action_owner = payload["next_action_owner"]
        callback_reason = payload["callback_reason"]
        read_contract = payload["read_contract"]
    except KeyError as exc:
        raise TicketlessCallbackError(
            f"ticketless callback payload missing required field: {exc.args[0]!r}"
        ) from exc
    redmine_anchor_required = payload.get("redmine_anchor_required")
    return TicketlessCallback(
        classification=classification,  # type: ignore[arg-type]
        dispatch_decision=dispatch_decision,  # type: ignore[arg-type]
        next_action_owner=next_action_owner,  # type: ignore[arg-type]
        callback_reason=callback_reason,  # type: ignore[arg-type]
        read_contract=read_contract,  # type: ignore[arg-type]
        redmine_anchor_required=redmine_anchor_required,  # type: ignore[arg-type]
        forward_action_id=payload.get("forward_action_id", ""),  # type: ignore[arg-type]
    )


__all__: Iterable[str] = (
    "TicketlessCallbackError",
    "CLASSIFICATION_CONSULTATION_RESULT",
    "CLASSIFICATION_NO_DISPATCH",
    "CLASSIFICATION_BLOCKED",
    "CLASSIFICATION_ANCHOR_REQUIRED",
    "CLASSIFICATIONS",
    "DISPATCH_NO_DISPATCH",
    "DISPATCH_HAND_BACK_TO_CALLER",
    "DISPATCH_ANCHOR_REQUIRED_BEFORE_WORKER",
    "TICKETLESS_DISPATCH_DECISIONS",
    "DISPATCH_REDMINE_ANCHORED_WORKER",
    "DISPATCH_DOMAIN_PROBE",
    "DISPATCH_IMPLEMENTATION",
    "ANCHOR_REQUIRED_DISPATCH_DECISIONS",
    "OWNER_CALLER",
    "OWNER_GATEWAY",
    "OWNER_WORKER",
    "OWNER_OPERATOR",
    "NEXT_ACTION_OWNERS",
    "REASON_CONSULTATION_CLASSIFIED",
    "REASON_NO_DISPATCH_DECIDED",
    "REASON_BLOCKED_PENDING_DECISION",
    "REASON_ANCHOR_REQUIRED_FOR_WORKER",
    "CALLBACK_REASONS",
    "READ_CONTRACT_TOKENS",
    "TicketlessCallback",
    "ticketless_callback_from_payload",
)
