"""Ticketless no-anchor rail anchor value objects (Redmine #12750 split).

These three value objects and the ``SOURCE_TICKETLESS`` token previously lived
inline in ``domain/handoff.py``. They are factored into their own module so the
**Redmine-anchored vs ticketless-no-anchor boundary is physically explicit**:
the anchored anchor models (:class:`AsanaAnchor` / :class:`RedmineAnchor`) and
the anchored ``SOURCES`` set stay in ``handoff.py``, while the no-anchor rail
models live here. ``handoff.py`` imports and re-exports these symbols, so every
existing import site (``commands.py`` and the ticketless tests import them from
``handoff``) keeps working unchanged.

The fundamental invariant is preserved by construction: ``SOURCE_TICKETLESS`` is
deliberately NOT a member of the anchored ``SOURCES`` (asana / redmine) set, and
``normalize_anchor`` only ever builds an :class:`AsanaAnchor` /
:class:`RedmineAnchor`. A regular ``handoff send`` / ``reply`` therefore can
never select the ticketless source to bypass the anchor requirement — only the
dedicated ticketless rails (``handoff ticketless-callback`` /
``project-gateway consult`` / ``project-gateway child-intake``) construct the
value objects below, and none of them fabricate a Redmine issue/journal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Redmine #12703 / #12740 / #12748: the ticketless no-anchor rails carry no
# Redmine / Asana anchor. ``SOURCE_TICKETLESS`` is the marker/outcome source
# token for those rails. It is deliberately NOT in the anchored ``SOURCES`` set
# defined in ``handoff.py``: the anchored ``handoff send`` / ``reply`` rails (and
# ``normalize_anchor``) only accept asana / redmine, so a regular send can never
# select the ticketless source to bypass the anchor requirement. Only the
# dedicated ticketless rails build the value objects below.
SOURCE_TICKETLESS = "ticketless"


@dataclass(frozen=True)
class TicketlessAnchor:
    """Anchor for the ticketless no-anchor callback rail (Redmine #12703).

    There is no Redmine issue/journal or Asana task here — the ticketless
    consultation phase is kept anchor-free on purpose, and fabricating an anchor
    to satisfy the reply rail is the issue's explicit prohibition. This value
    object exists only so the standard delivery rail (marker / notification body /
    structured outcome) works without a real ticket anchor: it carries the fixed
    ``classification`` / ``dispatch_decision`` tokens that ride the greppable
    landing marker, and its ``human_pointer`` states plainly that the structured
    callback fields are the durable record. The full structured callback result
    is carried separately on ``DeliveryOutcome.ticketless_callback`` so the
    transport outcome and the workflow result stay distinct.
    """

    classification: str
    dispatch_decision: str

    @property
    def source(self) -> str:
        return SOURCE_TICKETLESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "classification": self.classification,
            "dispatch_decision": self.dispatch_decision,
        }

    def marker_fields(self) -> list[tuple[str, str]]:
        return [
            ("classification", self.classification),
            ("dispatch", self.dispatch_decision),
        ]

    def human_pointer(self) -> str:
        return (
            f"ticketless callback (classification={self.classification}, "
            f"dispatch={self.dispatch_decision}); no Redmine anchor — the "
            "structured callback fields are the durable record"
        )


@dataclass(frozen=True)
class TicketlessConsultationAnchor:
    """Anchor for the ticketless no-anchor FORWARD consultation rail (Redmine #12740).

    The symmetric counterpart of :class:`TicketlessAnchor` (which rides the
    *return* callback leg): there is no Redmine issue/journal or Asana task here —
    the forward consultation phase is kept anchor-free on purpose, and fabricating
    an anchor to satisfy the anchored send rail is the issue's explicit
    prohibition. This value object exists only so the standard delivery rail
    (marker / notification body / structured outcome) works without a real ticket
    anchor: it carries the fixed ``consultation_kind`` / ``callback_to_role`` tokens
    that ride the greppable landing marker (distinct from the callback rail's
    ``classification`` / ``dispatch`` marker), and its ``human_pointer`` states
    plainly that the structured consultation fields are the durable record. The full
    structured forward payload is carried separately on
    ``DeliveryOutcome.ticketless_consultation`` so the transport outcome and the
    workflow request stay distinct.
    """

    consultation_kind: str
    callback_to_role: str

    @property
    def source(self) -> str:
        return SOURCE_TICKETLESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "consultation_kind": self.consultation_kind,
            "callback_to_role": self.callback_to_role,
        }

    def marker_fields(self) -> list[tuple[str, str]]:
        return [
            ("consultation", self.consultation_kind),
            ("callback_to", self.callback_to_role),
        ]

    def human_pointer(self) -> str:
        return (
            f"ticketless consultation (kind={self.consultation_kind}, "
            f"callback_to={self.callback_to_role}); no Redmine anchor — the "
            "structured consultation fields are the durable record"
        )


@dataclass(frozen=True)
class TicketlessWorkIntakeAnchor:
    """Anchor for the ticketless no-anchor parent -> child work-intake rail (#12748).

    The one-step-down sibling of :class:`TicketlessConsultationAnchor` (which rides
    the grandparent -> parent forward leg): there is no Redmine issue/journal or
    Asana task here — the parent -> child work-intake phase is kept anchor-free on
    purpose (the child owns the anchor create/select/blocked decision), and
    fabricating an anchor to satisfy the anchored send rail is the issue's explicit
    prohibition. This value object exists only so the standard delivery rail
    (marker / notification body / structured outcome) works without a real ticket
    anchor: it carries the fixed ``work_shape`` / ``callback_to_role`` tokens that
    ride the greppable landing marker (distinct from the consultation rail's
    ``consultation`` marker and the callback rail's ``classification`` /
    ``dispatch`` marker), and its ``human_pointer`` states plainly that the
    structured work-intake fields are the durable record. The full structured
    forward payload is carried separately on
    ``DeliveryOutcome.ticketless_work_intake`` so the transport outcome and the
    workflow request stay distinct.
    """

    work_shape: str
    callback_to_role: str

    @property
    def source(self) -> str:
        return SOURCE_TICKETLESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "work_shape": self.work_shape,
            "callback_to_role": self.callback_to_role,
        }

    def marker_fields(self) -> list[tuple[str, str]]:
        return [
            ("work_intake", self.work_shape),
            ("callback_to", self.callback_to_role),
        ]

    def human_pointer(self) -> str:
        return (
            f"ticketless work-intake (shape={self.work_shape}, "
            f"callback_to={self.callback_to_role}); no Redmine anchor — the child "
            "owns the anchor decision and the structured work-intake fields are the "
            "durable record"
        )


__all__ = [
    "SOURCE_TICKETLESS",
    "TicketlessAnchor",
    "TicketlessConsultationAnchor",
    "TicketlessWorkIntakeAnchor",
]
