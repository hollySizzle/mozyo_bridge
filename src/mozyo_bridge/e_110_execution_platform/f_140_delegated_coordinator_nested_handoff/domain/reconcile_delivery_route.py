"""Reconcile callback delivery routing — semantic receiver -> provider (Redmine #13758 F3/F2).

Pure resolution of the ``--to <provider>`` a callback row must be delivered with, from the
row's semantic receiver role. The event-driven reconciler routes a self-heal to the
``expected_next_owner`` (worker / gateway / auditor), but the shared callback send port
previously delivered EVERY row via ``--to codex`` (review F2): a worker self-heal was
mis-addressed to a Codex pane instead of the same-lane Claude worker.

Provider binding (the default role binding; the config source of truth is
``provider_binding`` #13157, resolved upstream at composition — this is the fail-closed
default the delivery port applies when a row carries no richer binding):

- an implementation **worker** receiver -> ``claude`` (a sanctioned same-lane ``--to claude``
  dispatch; the same-lane Claude dispatch doctrine requires it to submit-complete);
- every other receiver — gateway, auditor, coordinator, the legacy discovery ``coordinator``
  / ``review_return:<lane>`` routes — -> ``codex``.

Fail-closed default is ``codex`` (the pre-#13758 behavior), so a non-worker / unknown
receiver is never mis-promoted to a Claude same-lane dispatch; only a recognized worker
receiver switches the provider.
"""

from __future__ import annotations

#: Provider tokens (literal; the ``--to`` argument of the handoff CLI).
PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"

#: Receiver-role tokens that resolve to a same-lane Claude worker dispatch.
WORKER_RECEIVER_ROLES = frozenset({"implementation_worker", "worker"})


def receiver_role_of(row: object) -> str:
    """The row's semantic receiver role: ``target_receiver`` if set, else ``callback_route``.

    The reconcile self-heal row carries the expected owner in both ``callback_route`` (the
    UNIQUE-key route) and ``target_receiver`` (the delivery target); a coordinator / discovery
    row carries a blank ``target_receiver`` and a ``coordinator`` / ``review_return:<lane>``
    route. Preferring ``target_receiver`` keeps the provider bound to the semantic receiver.
    """
    receiver = str(getattr(row, "target_receiver", "") or "").strip()
    if receiver:
        return receiver
    return str(getattr(row, "callback_route", "") or "").strip()


def callback_receiver_provider(row: object) -> str:
    """Resolve the ``--to`` provider for a callback row (pure, fail-closed to ``codex``).

    A recognized implementation-worker receiver -> :data:`PROVIDER_CLAUDE` (same-lane); every
    other / unknown receiver -> :data:`PROVIDER_CODEX` (the pre-#13758 default, so no existing
    discovery / coordinator row changes provider).
    """
    return (
        PROVIDER_CLAUDE
        if receiver_role_of(row) in WORKER_RECEIVER_ROLES
        else PROVIDER_CODEX
    )


def is_same_lane_worker(row: object) -> bool:
    """True when the row is delivered to a same-lane Claude worker (``--to claude``)."""
    return callback_receiver_provider(row) == PROVIDER_CLAUDE


__all__ = (
    "PROVIDER_CLAUDE",
    "PROVIDER_CODEX",
    "WORKER_RECEIVER_ROLES",
    "receiver_role_of",
    "callback_receiver_provider",
    "is_same_lane_worker",
)
