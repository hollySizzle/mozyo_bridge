"""Resolve the body marker a watched unit was last dispatched (Redmine #15855).

Without this, the ``unsent_composer`` classification is **unreachable in production**
(review j#110132 finding_2). The sensor only asserts it when
:func:`current_composer_retains_body` finds the dispatched body in the live composer, and
that predicate returns ``False`` for an empty marker — so a periodic pass that supplies no
marker can never report the one class #15842 exists to describe. The stall still escalates
(it falls through to ``unresponsive_indeterminate``, also an escalating class), but the
durable record then names the wrong remedy: patient waiting instead of ADR-0002's
Enter-only retry.

What is matched, and why it is redaction-safe
---------------------------------------------
The marker is the handoff ``notification_marker`` — a fixed structured token of the form
``[mozyo:handoff:source=…:issue=…:journal=…:kind=…:to=…]`` — recorded append-only in the
herdr delivery ledger. It carries **no pane content and no message body**: it is composed
entirely of identifiers this rail already writes into durable records elsewhere. That is
what lets a watcher match against it without breaking the hygiene rule that no screen text
leaves the classifier (``stall-watcher-screen-diff.md`` `## 出力の hygiene`).

The join, and the residual it leaves
------------------------------------
The ledger has no ``generation`` column, so "the marker for *this* generation" cannot be
selected directly. The join is instead the conjunction of what the ledger does carry:

- the **issue anchor** the discovery join already resolved for this slot;
- the **receiver** matching the slot's role;
- the send-time **target** matching the locator being observed right now;
- the **most recent** such entry.

Any part missing → no marker → the classification is exactly what it was before this module
existed. That is the fail-closed direction, and it is the important one: a *wrong* marker
would assert ``unsent_composer`` and recommend pressing Enter.

**Named residual.** A locator can be recycled onto a different process, so target-match is
not proof of same-generation. The bound on that is
:func:`current_composer_retains_body` itself: it asserts only when the marker is in the
**current composer** — not anywhere in the scrollback — so a stale marker mis-fires only if
that exact body is sitting unsent on the live screen right now, which is the very condition
the class describes. Closing the residual properly needs a generation column on the ledger,
which is a change to a shared store and out of this issue's scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

#: Ledger entry kinds are not filtered: a chained outcome / disposition row carries the same
#: ``notification_marker`` as its send, and taking the most recent row that has one is what
#: makes the resolution robust to which rail recorded last.
_MARKER_PREFIX = "[mozyo:handoff:"


def _norm(value: object) -> str:
    return str(value or "").strip()


def resolve_body_marker(
    ledger: object, *, issue: str, role: str, locator: str
) -> str:
    """The marker last dispatched to this slot, or ``""`` when it cannot be established.

    Fail-soft on every axis: an absent ledger, an unreadable one, a missing column, or a
    marker that does not look like a handoff marker all yield ``""``. A watcher must never
    fail a pass because a delivery history could not be read.
    """
    issue_id = _norm(issue)
    receiver = _norm(role)
    target = _norm(locator)
    if ledger is None or not issue_id or not receiver or not target:
        return ""

    try:
        records = ledger.records_for_issue(issue_id)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unreadable history is "no marker", never a crash
        return ""

    marker = ""
    for record in records or ():
        candidate = _norm(getattr(record, "notification_marker", ""))
        if not candidate.startswith(_MARKER_PREFIX):
            # Not a handoff marker (or absent). Matching arbitrary text would reintroduce
            # the whole-screen substring guess `ack-completion-receiver-state.md` forbids.
            continue
        if _norm(getattr(record, "receiver", "")) != receiver:
            continue
        if _norm(getattr(record, "target", "")) != target:
            continue
        # Rows arrive in ledger id order, so the last match is the most recent dispatch.
        marker = candidate
    return marker


def default_body_marker_resolver(
    home: Optional[Path] = None,
    *,
    ledger: object = None,
) -> Callable[[str, str, str], str]:
    """Bind :func:`resolve_body_marker` to the home-scoped herdr delivery ledger.

    Returns a ``(issue, role, locator) -> marker`` callable. The ledger is resolved lazily
    and a resolution failure yields a callable that always returns ``""`` — a host with no
    delivery history simply classifies without the ``unsent_composer`` evidence.
    """
    resolved = ledger
    if resolved is None:
        try:
            from mozyo_bridge.core.state.herdr_delivery_ledger import (
                HerdrDeliveryLedger,
                herdr_delivery_ledger_path,
            )

            resolved = HerdrDeliveryLedger(path=herdr_delivery_ledger_path(home))
        except Exception:  # noqa: BLE001 - no ledger -> no marker, never a crash
            resolved = None

    def _resolve(issue: str, role: str, locator: str) -> str:
        return resolve_body_marker(resolved, issue=issue, role=role, locator=locator)

    return _resolve


__all__ = (
    "default_body_marker_resolver",
    "resolve_body_marker",
)
