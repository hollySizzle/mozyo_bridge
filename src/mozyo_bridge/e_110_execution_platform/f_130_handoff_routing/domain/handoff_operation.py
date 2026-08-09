"""Core-owned high-level handoff operation vocabulary + entry policy (Redmine #15149).

The four high-level handoff operations (``send`` / ``reply`` /
``ticketless-callback`` / ``cross-workspace-consult``) each carry a small amount
of *entry policy* on top of the shared ``orchestrate_handoff`` primitive: which
``--kind`` default applies, whether the receiver is pinned, whether the relaxed
receiver-binding gate is forced in every mode, whether the run is the anchorless
ticketless rail, and whether semantic target selection runs first.

Until #15149 that policy existed only as four hand-written CLI entry bodies in
``application/handoff_command.py``, keyed off an ``argparse.Namespace``. A second
caller of the same operations — the shared application API a local MCP server
calls (#15148 / #15149) — would have had to restate it, and a restated policy is
a policy that drifts: a receiver pin or a forced binding gate that exists on one
entry and not the other is a silently weakened boundary.

This module is the single core-owned statement of that policy:

- :data:`HANDOFF_OPERATIONS` is the closed operation vocabulary. A caller cannot
  invent an operation; :func:`entry_policy_for` fails closed on an unknown name.
- :class:`HandoffEntryPolicy` is the frozen description of one operation's entry
  policy.
- :data:`ENTRY_POLICIES` binds each operation to its policy.

It is pure: no argparse, no I/O, no CLI text, no provider import. The CLI entry
bodies and the typed application API both read it, so the two entries cannot
answer "what does `cross-workspace-consult` pin?" differently.

The policy describes the *entry*; it never grants authority. Every actual gate —
identity, authority, gateway route, send safety — stays where it already is, in
``orchestrate_handoff`` and the f_130 admission / target / rail slices, and runs
identically for both callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

#: `handoff send` — the anchored cross-agent send. Applies semantic target
#: selection (Redmine #12663) before the unchanged identity gates.
OP_SEND = "send"

#: `handoff reply` — the anchored reply rail (``--kind`` defaults to ``reply``).
OP_REPLY = "reply"

#: `handoff ticketless-callback` — the anchorless callback rail (Redmine #12703).
OP_TICKETLESS_CALLBACK = "ticketless_callback"

#: `handoff cross-workspace-consult` — the cross-workspace design consult that
#: always lands on the target workspace's Codex gateway (Redmine #11779).
OP_CROSS_WORKSPACE_CONSULT = "cross_workspace_consult"

CONSULT_DEFAULT_KIND = "design_consultation"
"""Default ``--kind`` for `handoff cross-workspace-consult` (Redmine #11779).

The cross-workspace primitive exists to carry design-consultation requests
through the target workspace's Codex gateway, so it defaults to
``design_consultation`` while still accepting any other ``KIND_LABELS`` value
(e.g. a cross-workspace ``review_request``) via an explicit ``--kind``.
"""


class UnknownHandoffOperation(ValueError):
    """Fail-closed: an operation outside the core-owned vocabulary."""


@dataclass(frozen=True)
class HandoffEntryPolicy:
    """One high-level operation's entry policy over ``orchestrate_handoff``.

    ``default_kind`` is handed to the orchestration as its default-kind
    parameter (``kind = kind or default_kind``); ``pinned_kind`` is instead
    written onto the *input's* kind when the caller left it unset, which is how
    the consult entry has always expressed its default. Both resolve to the same
    effective kind, and they are kept distinct so each entry stays byte-for-byte
    what it was before the policy was extracted.
    """

    operation: str
    default_kind: str | None = None
    pinned_kind: str | None = None
    pinned_receiver: str | None = None
    require_receiver_binding: bool = False
    ticketless: bool = False
    semantic_selection: bool = False


ENTRY_POLICIES: Mapping[str, HandoffEntryPolicy] = {
    OP_SEND: HandoffEntryPolicy(operation=OP_SEND, semantic_selection=True),
    OP_REPLY: HandoffEntryPolicy(operation=OP_REPLY, default_kind="reply"),
    OP_TICKETLESS_CALLBACK: HandoffEntryPolicy(
        operation=OP_TICKETLESS_CALLBACK, default_kind="reply", ticketless=True
    ),
    OP_CROSS_WORKSPACE_CONSULT: HandoffEntryPolicy(
        operation=OP_CROSS_WORKSPACE_CONSULT,
        pinned_kind=CONSULT_DEFAULT_KIND,
        pinned_receiver="codex",
        require_receiver_binding=True,
    ),
}

#: The closed high-level operation vocabulary.
HANDOFF_OPERATIONS: tuple[str, ...] = tuple(ENTRY_POLICIES)


def entry_policy_for(operation: str) -> HandoffEntryPolicy:
    """The entry policy for ``operation``, or fail closed on an unknown name."""
    try:
        return ENTRY_POLICIES[operation]
    except KeyError:
        raise UnknownHandoffOperation(
            f"unknown handoff operation {operation!r}; "
            f"expected one of {sorted(HANDOFF_OPERATIONS)}"
        ) from None


__all__ = (
    "CONSULT_DEFAULT_KIND",
    "ENTRY_POLICIES",
    "HANDOFF_OPERATIONS",
    "HandoffEntryPolicy",
    "OP_CROSS_WORKSPACE_CONSULT",
    "OP_REPLY",
    "OP_SEND",
    "OP_TICKETLESS_CALLBACK",
    "UnknownHandoffOperation",
    "entry_policy_for",
)
