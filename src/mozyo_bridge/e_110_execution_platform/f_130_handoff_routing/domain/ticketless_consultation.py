"""Forward ticketless no-anchor consultation payload (Redmine #12740).

`#12703 ticketless no-anchor callback transport` and `#12705 LLM-facing q-enter
primitive` gave the project gateway a way to *return* a consultation result to its
caller without a Redmine anchor (the :mod:`...domain.ticketless_callback` rail).
But the GK3500 rerun (after `#12739 Cockpit must allow root and project-scoped
units to coexist`) hit the mirror gap on the *forward* leg: the department-root
coordinator classified a consultation to a project, found exactly one project
Codex gateway by semantic identity, and then had no product-standard no-anchor
primitive to hand the consultation *to* that gateway. The anchored
``handoff send --source redmine --kind custom`` failed closed with
``invalid_anchor`` because Redmine source requires both ``--issue`` and
``--journal``, and root correctly refused raw pane typing.

This module is the pure, fail-closed source of truth for the **forward ticketless
consultation payload** that the no-anchor forward rail carries. It is the
*envelope* the root coordinator delegates to the project gateway: what class of
consultation is being forwarded, which role the gateway returns the result to, and
which product callback primitives it may return via. It is the symmetric
counterpart of :mod:`...domain.ticketless_callback` (which carries the *result*);
this carries the forward *request + return contract*. Design boundaries
(Redmine #12740 description / j#67274):

- A forward consultation never carries — and never requires — a Redmine
  issue/journal anchor. Fabricating an anchor to satisfy the anchored send rail is
  the issue's explicit prohibition. The structured fields below ARE the durable
  forward record.
- The Redmine-anchor gate for worker dispatch / implementation / domain probe is
  **not relaxed**. This rail forwards a *consultation* only; an actual worker
  dispatch is not expressible here (there is no anchor and no dispatch token), and
  the payload restates the invariant (:data:`WORKER_DISPATCH_REQUIRES_ANCHOR`) so
  the receiver gateway knows it must mint a real Redmine anchor before dispatching
  a worker — exactly the boundary the issue requires kept.
- Every field is a fixed lower-snake-case token (or a fixed token tuple) with no
  operator free text, so the whole payload is durable-record safe and may be
  persisted verbatim, like the :mod:`...domain.transition_role` /
  :mod:`...domain.workflow_contract` / :mod:`...domain.ticketless_callback`
  boundaries it travels beside. Free-text narrative stays on the transport
  notification body, never in this payload.
- The forward payload carries NO ``#12709`` rerun gate, prior smoke history, prior
  journals, expected route, or pane ids (Redmine #12740 constraints). The callback
  return contract is expressed as ROLES and PRODUCT PRIMITIVES, never a volatile
  ``%pane`` — the gateway resolves the caller lane by semantic identity, the same
  way the forward leg resolved the gateway.
- Construction fails closed: an unknown consultation kind / callback role / read
  contract, or an empty / unknown callback-method set, raises
  :class:`TicketlessConsultationError`. Omitting the payload is the explicit
  fallback of no forward consultation binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)


class TicketlessConsultationError(ValueError):
    """A forward ticketless consultation payload could not be built or is malformed."""


# --- Invariant: this no-anchor forward rail does NOT relax the Redmine-anchor
# gate for any actual worker dispatch / implementation / domain probe. It forwards
# a consultation only; the receiver gateway must mint a real Redmine anchor before
# dispatching a worker. Carried as a fixed-true field so the receiver reads the
# boundary verbatim from the durable record. ---
WORKER_DISPATCH_REQUIRES_ANCHOR = True

# --- Consultation-kind tokens — the class of forward consultation the root
# delegates to the project gateway. Deliberately a small fixed set of
# consultation-phase classes; no worker-dispatch token is expressible here. ---
CONSULTATION_PROJECT_DOMAIN = "project_domain_consultation"
CONSULTATION_ROUTING = "routing_consultation"

CONSULTATION_KINDS: tuple[str, ...] = (
    CONSULTATION_PROJECT_DOMAIN,
    CONSULTATION_ROUTING,
)

# --- Callback-return method tokens — the product primitives the gateway may
# return its result via. Pinned to the two no-anchor return rails the gateway
# already owns (#12703 ticketless-callback / #12705 q-enter consultation_callback),
# matching the #12737 gateway callback-return obligation. ---
CALLBACK_VIA_TICKETLESS_CALLBACK = "ticketless_callback"
CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK = "q_enter_consultation_callback"

CALLBACK_METHODS: tuple[str, ...] = (
    CALLBACK_VIA_TICKETLESS_CALLBACK,
    CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK,
)

# --- Callback-target role tokens — which lane role the gateway returns the result
# to (the caller's transition role). Expressed as a role, never a `%pane`: the
# gateway resolves the caller lane by semantic identity (Redmine #12740 constraint:
# no pane ids to ticketless receivers). Pinned to the #12706 transition-role
# vocabulary so the gateway resolves the same role tokens it already reads. ---
CALLBACK_TO_ROLES: tuple[str, ...] = (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)

# --- Read-contract tokens: which contract set governs the RECEIVER's action. The
# forward consultation's receiver is the project gateway, so this names which role
# contract it must act under. Pinned to the #12700 / #12706 transition-role
# tokens. ---
READ_CONTRACT_TOKENS: tuple[str, ...] = (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)


def _clean_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketlessConsultationError(
            f"ticketless consultation {field} must be a non-empty token; got {value!r}"
        )
    return value.strip()


def _clean_choice(value: object, choices: tuple[str, ...], *, field: str) -> str:
    token = _clean_token(value, field=field)
    if token not in choices:
        raise TicketlessConsultationError(
            f"unknown ticketless consultation {field}: {token!r}; expected one of "
            f"{list(choices)}"
        )
    return token


def _clean_methods(value: object) -> tuple[str, ...]:
    """Validate the callback-method set into a deduped, order-preserving tuple.

    Fails closed on a non-sequence, an empty set, or any unknown / blank token, so
    a forward consultation always names at least one valid return primitive the
    gateway can use to call back.
    """
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TicketlessConsultationError(
            "ticketless consultation callback_methods must be a non-empty sequence "
            f"of method tokens; got {value!r}"
        )
    cleaned: list[str] = []
    for item in value:
        token = _clean_choice(item, CALLBACK_METHODS, field="callback_method")
        if token not in cleaned:
            cleaned.append(token)
    if not cleaned:
        raise TicketlessConsultationError(
            "ticketless consultation callback_methods must name at least one return "
            f"primitive; expected a non-empty subset of {list(CALLBACK_METHODS)}"
        )
    return tuple(cleaned)


@dataclass(frozen=True)
class TicketlessConsultation:
    """Structured forward ticketless no-anchor consultation payload (#12740).

    Names the class of consultation forwarded to the project gateway, which role
    the gateway returns the result to, the product primitives it may return via,
    and which role contract governs its action. All fields are fixed tokens with no
    operator free text, so a payload is durable-record safe in full and is recorded
    distinctly from the transport ``DeliveryOutcome``.

    Construction fails closed on an unknown token or an empty / unknown
    callback-method set. The :data:`WORKER_DISPATCH_REQUIRES_ANCHOR` invariant is
    carried so the receiver reads — verbatim — that the no-anchor forward rail does
    not relax the Redmine-anchor gate for worker dispatch.
    """

    consultation_kind: str
    callback_to_role: str
    callback_methods: tuple[str, ...]
    read_contract: str
    #: Opaque forward generation correlation id (Redmine #13583 R1-F1). ``""`` for a non-forward
    #: (tmux) consultation; a herdr coordinator forward sets it so the returning callback can echo
    #: it and complete the exact forward generation. Never a role / approval / anchor authority.
    forward_action_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "consultation_kind",
            _clean_choice(
                self.consultation_kind, CONSULTATION_KINDS, field="consultation_kind"
            ),
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
        object.__setattr__(
            self,
            "forward_action_id",
            str(self.forward_action_id).strip() if self.forward_action_id is not None else "",
        )

    @property
    def worker_dispatch_requires_anchor(self) -> bool:
        """Fixed-true invariant: worker dispatch still needs a Redmine anchor."""
        return WORKER_DISPATCH_REQUIRES_ANCHOR

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free fields for the handoff consultation payload."""
        payload: dict[str, object] = {
            "consultation_kind": self.consultation_kind,
            "callback_to_role": self.callback_to_role,
            "callback_methods": list(self.callback_methods),
            "read_contract": self.read_contract,
            "worker_dispatch_requires_anchor": WORKER_DISPATCH_REQUIRES_ANCHOR,
        }
        if self.forward_action_id:
            payload["forward_action_id"] = self.forward_action_id
        return payload

    def marker_fields(self) -> list[tuple[str, str]]:
        """Marker key/value pairs for the ticketless forward landing marker.

        Only the consultation kind + callback-target role ride the greppable
        landing marker (the full structured payload is in the durable delivery
        record); both are fixed tokens, so the marker stays deterministic and
        durable-record safe, and distinct from the callback rail's
        ``classification`` / ``dispatch`` marker.
        """
        return [
            ("consultation", self.consultation_kind),
            ("callback_to", self.callback_to_role),
        ]

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full structured payload stays in the durable delivery record. Names
        the consultation kind, the callback-return contract (role + primitives),
        the read contract, and the preserved worker-dispatch anchor rule.
        """
        methods = " or ".join(self.callback_methods)
        return (
            f"ticketless consultation: kind {self.consultation_kind}; return the "
            f"result to {self.callback_to_role} via {methods}; act under the "
            f"{self.read_contract} contract; worker dispatch / implementation / "
            "domain probe still requires a Redmine anchor (mint one and use "
            "`handoff send --source redmine`); the structured consultation fields "
            "are the durable delivery record (no Redmine anchor was fabricated)"
        )

    def record_lines(self) -> list[str]:
        """Full durable-record block: every structured forward consultation field.

        Fixed tokens only, so it is rendered in place and the receiver reads the
        forward consultation it must act on — and how to return the result —
        without re-reading the pane.
        """
        methods = ", ".join(f"`{m}`" for m in self.callback_methods)
        return [
            f"- Ticketless consultation: kind `{self.consultation_kind}`",
            f"  - Return result to role: `{self.callback_to_role}`",
            f"  - Return via: {methods}",
            f"  - Read contract: `{self.read_contract}`",
            "  - Worker dispatch requires Redmine anchor: "
            f"`{str(WORKER_DISPATCH_REQUIRES_ANCHOR).lower()}` "
            "(this no-anchor forward rail does not relax the worker-dispatch gate)",
        ]


def ticketless_consultation_from_payload(
    payload: Mapping[str, object],
) -> TicketlessConsultation:
    """Rebuild a consultation from a structured payload (round-trips to_structured_dict).

    Fails closed (:class:`TicketlessConsultationError`) on a missing / malformed
    field, so a receiver that parses the consultation payload cannot silently
    accept a partial forward request. The ``worker_dispatch_requires_anchor``
    invariant is re-asserted by construction (it is a fixed constant, not a carried
    variable), so a tampered payload cannot smuggle a relaxed anchor obligation.
    """
    try:
        consultation_kind = payload["consultation_kind"]
        callback_to_role = payload["callback_to_role"]
        callback_methods = payload["callback_methods"]
        read_contract = payload["read_contract"]
    except KeyError as exc:
        raise TicketlessConsultationError(
            f"ticketless consultation payload missing required field: {exc.args[0]!r}"
        ) from exc
    return TicketlessConsultation(
        consultation_kind=consultation_kind,  # type: ignore[arg-type]
        callback_to_role=callback_to_role,  # type: ignore[arg-type]
        callback_methods=callback_methods,  # type: ignore[arg-type]
        read_contract=read_contract,  # type: ignore[arg-type]
        forward_action_id=payload.get("forward_action_id", ""),  # type: ignore[arg-type]
    )


__all__: Iterable[str] = (
    "TicketlessConsultationError",
    "WORKER_DISPATCH_REQUIRES_ANCHOR",
    "CONSULTATION_PROJECT_DOMAIN",
    "CONSULTATION_ROUTING",
    "CONSULTATION_KINDS",
    "CALLBACK_VIA_TICKETLESS_CALLBACK",
    "CALLBACK_VIA_Q_ENTER_CONSULTATION_CALLBACK",
    "CALLBACK_METHODS",
    "CALLBACK_TO_ROLES",
    "READ_CONTRACT_TOKENS",
    "TicketlessConsultation",
    "ticketless_consultation_from_payload",
)
