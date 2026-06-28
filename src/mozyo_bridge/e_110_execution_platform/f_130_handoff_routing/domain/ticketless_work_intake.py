"""Forward ticketless no-anchor parent -> child work-intake payload (Redmine #12748).

`#12740 Root-to-project ticketless handoff needs no-anchor delivery primitive`
gave the *grandparent -> parent* leg a no-anchor forward primitive
(`project-gateway consult`, carrying the
:mod:`...domain.ticketless_consultation` envelope). The next GK3500 rerun hit the
mirror gap one step down: the parent ``project_gateway`` must NOT answer a
domain/design consultation itself; it must route the consultation to the child /
implementation gateway (``delegated_coordinator``) as **ticketless work-intake**,
and the child owns the Redmine issue/journal create/select/blocked decision. But
the only thing in the workflow for the ``parent -> child`` row was a concept-level
"ticketless work-intake route"; there was no product-standard no-anchor envelope
for it, so receivers started exploring command families at runtime and smoke
evidence went noisy.

This module is the pure, fail-closed source of truth for the **forward ticketless
work-intake payload** that the no-anchor parent -> child rail carries. It is the
*envelope* the project gateway delegates to the child coordinator: what shape of
work is being forwarded, which role the child returns the result to, which
product callback primitives it may return via, and which role contract governs
the child's action. It is the sibling of :mod:`...domain.ticketless_consultation`
(grandparent -> parent), specialized for the ``parent -> child`` boundary. Design
boundaries (Redmine #12748 description / runtime-ux `親 -> 子` row):

- A forward work-intake never carries — and never requires — a Redmine
  issue/journal anchor. The parent does NOT mint one and does NOT return
  ``anchor_required`` merely because none exists; the child owns the
  create/select/block decision (:data:`CHILD_OWNS_ANCHOR_DECISION`). Fabricating
  an anchor to satisfy the anchored send rail is the issue's explicit
  prohibition. The structured fields below ARE the durable forward record.
- The Redmine-anchor gate for worker dispatch / implementation / domain probe is
  **not relaxed** (:data:`WORKER_DISPATCH_REQUIRES_ANCHOR`). This rail forwards a
  *work-intake* only; an actual worker dispatch is not expressible here (there is
  no anchor and no dispatch token), so the child must mint a real Redmine anchor
  before dispatching the grandchild worker.
- The parent is a traffic-control actor, not a domain/design decision actor
  (:data:`PARENT_MUST_NOT_ANSWER_DOMAIN`). The payload restates this so a run
  where the parent answered the consultation itself is classified
  ``insufficient`` / ``failed_acceptance``, not green.
- Every field is a fixed lower-snake-case token (or a fixed token tuple) with no
  operator free text, so the whole payload is durable-record safe and may be
  persisted verbatim, like the :mod:`...domain.transition_role` /
  :mod:`...domain.workflow_contract` / :mod:`...domain.ticketless_consultation`
  boundaries it travels beside. Free-text narrative stays on the transport
  notification body, never in this payload.
- The forward payload carries NO ``#12709`` rerun gate, prior smoke history,
  prior journals, expected route, or pane ids (Redmine #12748 constraints). The
  callback return contract is expressed as ROLES and PRODUCT PRIMITIVES, never a
  volatile ``%pane`` — the child resolves the caller lane by semantic identity,
  the same way the forward leg resolved the child.
- Construction fails closed: an unknown work shape / callback role / read
  contract, or an empty / unknown callback-method set, raises
  :class:`TicketlessWorkIntakeError`. Omitting the payload is the explicit
  fallback of no forward work-intake binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.ticketless_consultation import (
    CALLBACK_METHODS,
    CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK,
    CALLBACK_VIA_TICKETLESS_CALLBACK,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_PROJECT_GATEWAY,
)


class TicketlessWorkIntakeError(ValueError):
    """A forward ticketless work-intake payload could not be built or is malformed."""


# The child / implementation gateway role the parent routes work-intake to. Shares
# the project gateway's live identity (a strong project-scoped Codex) but is a
# distinct relative lane (`delegated_coordinator`). Named here so the read-contract
# and anchor-decision owner are durable-record literals, not inferred at runtime.
ROLE_DELEGATED_COORDINATOR = "delegated_coordinator"


# --- Invariant: this no-anchor forward rail does NOT relax the Redmine-anchor gate
# for any actual worker dispatch / implementation / domain probe. The child must
# mint a real Redmine anchor before dispatching the grandchild worker. ---
WORKER_DISPATCH_REQUIRES_ANCHOR = True

# --- Invariant: the parent project gateway is a traffic-control actor and must not
# answer the domain/design consultation itself; it forwards the work-intake to the
# child. A run where the parent absorbs the domain/design decision is not green. ---
PARENT_MUST_NOT_ANSWER_DOMAIN = True

# --- Invariant: the CHILD owns the Redmine issue/journal create / select / blocked
# decision. The parent does not return `anchor_required` merely because no anchor
# exists; only the child returns `anchor_required` when it cannot secure one. ---
CHILD_OWNS_ANCHOR_DECISION = True

# Who owns the anchor create/select/block decision (the child coordinator). Fixed,
# so the receiver reads the ownership boundary verbatim from the durable record.
ANCHOR_DECISION_OWNER = ROLE_DELEGATED_COORDINATOR

# --- Work-shape tokens — the class of work the parent forwards to the child for
# triage. Deliberately a small fixed set of consultation-phase shapes; NONE of
# them authorizes a worker dispatch (that stays anchor-gated and is the child's
# decision after it mints an anchor). The shape only tells the child what kind of
# work to triage into an anchor. ---
WORK_SHAPE_DOMAIN_DESIGN = "domain_design_work_intake"
WORK_SHAPE_PILOT_FEASIBILITY = "pilot_feasibility_work_intake"
WORK_SHAPE_IMPLEMENTATION = "implementation_work_intake"

WORK_SHAPES: tuple[str, ...] = (
    WORK_SHAPE_DOMAIN_DESIGN,
    WORK_SHAPE_PILOT_FEASIBILITY,
    WORK_SHAPE_IMPLEMENTATION,
)

# --- Callback-return method tokens — the product primitives the child may return
# its result via. Pinned to the same two no-anchor return rails the gateway forward
# leg uses (#12703 ticketless-callback / #12705 q-enter consultation_callback), so
# the child -> parent and parent -> grandparent callback path needs no fake anchor
# (Redmine #12748 required behavior). Reused from the consultation envelope
# (imported above) so there is one source of truth for the no-anchor return rails;
# re-exported in ``__all__`` for callers that build the payload. ---

# --- Callback-target role tokens — which lane role the child returns the result
# to. The child returns to its parent, the project gateway (runtime-ux `子 -> 親`
# row); the parent then returns up to the grandparent via its own callback leg.
# Expressed as a role, never a `%pane`: the child resolves the caller lane by
# semantic identity (Redmine #12748 constraint: no pane ids to ticketless
# receivers). ---
CALLBACK_TO_ROLES: tuple[str, ...] = (ROLE_PROJECT_GATEWAY,)

# --- Read-contract tokens: which contract set governs the RECEIVER's action. The
# work-intake's receiver is the child coordinator, so this names that it must act
# under the `delegated_coordinator` role contract (own the anchor decision,
# dispatch the grandchild worker only against a Redmine anchor). ---
READ_CONTRACT_TOKENS: tuple[str, ...] = (ROLE_DELEGATED_COORDINATOR,)


def _clean_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketlessWorkIntakeError(
            f"ticketless work-intake {field} must be a non-empty token; got {value!r}"
        )
    return value.strip()


def _clean_choice(value: object, choices: tuple[str, ...], *, field: str) -> str:
    token = _clean_token(value, field=field)
    if token not in choices:
        raise TicketlessWorkIntakeError(
            f"unknown ticketless work-intake {field}: {token!r}; expected one of "
            f"{list(choices)}"
        )
    return token


def _clean_methods(value: object) -> tuple[str, ...]:
    """Validate the callback-method set into a deduped, order-preserving tuple.

    Fails closed on a non-sequence, an empty set, or any unknown / blank token, so
    a forward work-intake always names at least one valid return primitive the
    child can use to call back without a Redmine anchor.
    """
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TicketlessWorkIntakeError(
            "ticketless work-intake callback_methods must be a non-empty sequence "
            f"of method tokens; got {value!r}"
        )
    cleaned: list[str] = []
    for item in value:
        token = _clean_choice(item, CALLBACK_METHODS, field="callback_method")
        if token not in cleaned:
            cleaned.append(token)
    if not cleaned:
        raise TicketlessWorkIntakeError(
            "ticketless work-intake callback_methods must name at least one return "
            f"primitive; expected a non-empty subset of {list(CALLBACK_METHODS)}"
        )
    return tuple(cleaned)


@dataclass(frozen=True)
class TicketlessWorkIntake:
    """Structured forward ticketless no-anchor parent -> child work-intake (#12748).

    Names the shape of work the project gateway forwards to the child coordinator,
    which role the child returns the result to, the product primitives it may
    return via, and which role contract governs the child's action. All fields are
    fixed tokens with no operator free text, so a payload is durable-record safe in
    full and is recorded distinctly from the transport ``DeliveryOutcome``.

    Construction fails closed on an unknown token or an empty / unknown
    callback-method set. The :data:`WORKER_DISPATCH_REQUIRES_ANCHOR`,
    :data:`PARENT_MUST_NOT_ANSWER_DOMAIN`, and :data:`CHILD_OWNS_ANCHOR_DECISION`
    invariants are carried so the child reads — verbatim — that the no-anchor
    forward rail does not relax the worker-dispatch gate, that the parent must not
    answer the domain/design itself, and that the child owns the anchor decision.
    """

    work_shape: str
    callback_to_role: str
    callback_methods: tuple[str, ...]
    read_contract: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "work_shape",
            _clean_choice(self.work_shape, WORK_SHAPES, field="work_shape"),
        )
        object.__setattr__(
            self,
            "callback_to_role",
            _clean_choice(
                self.callback_to_role, CALLBACK_TO_ROLES, field="callback_to_role"
            ),
        )
        object.__setattr__(
            self, "callback_methods", _clean_methods(self.callback_methods)
        )
        object.__setattr__(
            self,
            "read_contract",
            _clean_choice(
                self.read_contract, READ_CONTRACT_TOKENS, field="read_contract"
            ),
        )

    @property
    def worker_dispatch_requires_anchor(self) -> bool:
        """Fixed-true invariant: worker dispatch still needs a Redmine anchor."""
        return WORKER_DISPATCH_REQUIRES_ANCHOR

    @property
    def parent_must_not_answer_domain(self) -> bool:
        """Fixed-true invariant: the parent forwards, it does not answer domain/design."""
        return PARENT_MUST_NOT_ANSWER_DOMAIN

    @property
    def child_owns_anchor_decision(self) -> bool:
        """Fixed-true invariant: the child owns the anchor create/select/block decision."""
        return CHILD_OWNS_ANCHOR_DECISION

    @property
    def anchor_decision_owner(self) -> str:
        """The role that owns the Redmine anchor decision (the child coordinator)."""
        return ANCHOR_DECISION_OWNER

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free fields for the handoff work-intake payload."""
        return {
            "work_shape": self.work_shape,
            "callback_to_role": self.callback_to_role,
            "callback_methods": list(self.callback_methods),
            "read_contract": self.read_contract,
            "anchor_decision_owner": ANCHOR_DECISION_OWNER,
            "worker_dispatch_requires_anchor": WORKER_DISPATCH_REQUIRES_ANCHOR,
            "parent_must_not_answer_domain": PARENT_MUST_NOT_ANSWER_DOMAIN,
            "child_owns_anchor_decision": CHILD_OWNS_ANCHOR_DECISION,
        }

    def marker_fields(self) -> list[tuple[str, str]]:
        """Marker key/value pairs for the ticketless forward work-intake marker.

        Only the work shape + callback-target role ride the greppable landing
        marker (the full structured payload is in the durable delivery record);
        both are fixed tokens, so the marker stays deterministic and durable-record
        safe, and distinct from the consultation rail's ``consultation`` marker and
        the callback rail's ``classification`` / ``dispatch`` marker.
        """
        return [
            ("work_intake", self.work_shape),
            ("callback_to", self.callback_to_role),
        ]

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full structured payload stays in the durable delivery record. Names
        the work shape, the callback-return contract (role + primitives), the read
        contract, that the child owns the anchor decision, and the preserved
        worker-dispatch anchor rule.
        """
        methods = " or ".join(self.callback_methods)
        return (
            f"ticketless work-intake: shape {self.work_shape}; you (the "
            f"{self.read_contract}) own the Redmine anchor create/select/blocked "
            f"decision — the parent does not answer the domain/design and did not "
            f"mint an anchor; return the result to {self.callback_to_role} via "
            f"{methods}; worker dispatch / implementation / domain probe still "
            "requires a Redmine anchor (mint one and use `handoff send --source "
            "redmine`); the structured work-intake fields are the durable delivery "
            "record (no Redmine anchor was fabricated)"
        )

    def record_lines(self) -> list[str]:
        """Full durable-record block: every structured forward work-intake field.

        Fixed tokens only, so it is rendered in place and the child reads the
        forward work-intake it must act on — and how to return the result —
        without re-reading the pane.
        """
        methods = ", ".join(f"`{m}`" for m in self.callback_methods)
        return [
            f"- Ticketless work-intake: shape `{self.work_shape}`",
            f"  - Anchor decision owner: `{ANCHOR_DECISION_OWNER}` "
            "(the child owns create/select/blocked; the parent does not mint one)",
            f"  - Return result to role: `{self.callback_to_role}`",
            f"  - Return via: {methods}",
            f"  - Read contract: `{self.read_contract}`",
            "  - Parent must not answer domain/design: "
            f"`{str(PARENT_MUST_NOT_ANSWER_DOMAIN).lower()}`",
            "  - Worker dispatch requires Redmine anchor: "
            f"`{str(WORKER_DISPATCH_REQUIRES_ANCHOR).lower()}` "
            "(this no-anchor forward rail does not relax the worker-dispatch gate)",
        ]


def ticketless_work_intake_from_payload(
    payload: Mapping[str, object],
) -> TicketlessWorkIntake:
    """Rebuild a work-intake from a structured payload (round-trips to_structured_dict).

    Fails closed (:class:`TicketlessWorkIntakeError`) on a missing / malformed
    field, so a child that parses the work-intake payload cannot silently accept a
    partial forward request. The invariant fields
    (``worker_dispatch_requires_anchor`` / ``parent_must_not_answer_domain`` /
    ``child_owns_anchor_decision`` / ``anchor_decision_owner``) are re-asserted by
    construction (they are fixed constants, not carried variables), so a tampered
    payload cannot smuggle a relaxed anchor obligation or a domain-answering parent.
    """
    try:
        work_shape = payload["work_shape"]
        callback_to_role = payload["callback_to_role"]
        callback_methods = payload["callback_methods"]
        read_contract = payload["read_contract"]
    except KeyError as exc:
        raise TicketlessWorkIntakeError(
            f"ticketless work-intake payload missing required field: {exc.args[0]!r}"
        ) from exc
    return TicketlessWorkIntake(
        work_shape=work_shape,  # type: ignore[arg-type]
        callback_to_role=callback_to_role,  # type: ignore[arg-type]
        callback_methods=callback_methods,  # type: ignore[arg-type]
        read_contract=read_contract,  # type: ignore[arg-type]
    )


__all__: Iterable[str] = (
    "TicketlessWorkIntakeError",
    "ROLE_DELEGATED_COORDINATOR",
    "WORKER_DISPATCH_REQUIRES_ANCHOR",
    "PARENT_MUST_NOT_ANSWER_DOMAIN",
    "CHILD_OWNS_ANCHOR_DECISION",
    "ANCHOR_DECISION_OWNER",
    "WORK_SHAPE_DOMAIN_DESIGN",
    "WORK_SHAPE_PILOT_FEASIBILITY",
    "WORK_SHAPE_IMPLEMENTATION",
    "WORK_SHAPES",
    "CALLBACK_VIA_TICKETLESS_CALLBACK",
    "CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK",
    "CALLBACK_METHODS",
    "CALLBACK_TO_ROLES",
    "READ_CONTRACT_TOKENS",
    "TicketlessWorkIntake",
    "ticketless_work_intake_from_payload",
)
