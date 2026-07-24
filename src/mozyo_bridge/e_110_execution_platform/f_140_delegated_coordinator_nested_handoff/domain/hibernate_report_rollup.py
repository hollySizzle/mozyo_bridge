"""Auto-hibernate observability roll-up (Redmine #14219 T3, Answer j#87108 item 4; review j#87154 R1-F3/F4).

Pure classification of the folded hibernate leg's per-workspace outcomes into the secret-free
report metrics the T3 acceptance asks for — ``ran`` / candidate / claimed / applied / blocked /
uncertain / deferred / released capacity / release-incomplete / closed reason. Split out of
``workspace_supervisor`` to keep that module under the module-health line ceiling.

Two redaction-safe inputs per workspace, both carried on a :class:`WorkspaceSupervisionOutcome`
(this leaf imports no outcome type — it duck-types):

* ``hibernate_attempts`` — the leg's typed per-candidate dicts (issue / lane / kind / reason /
  revision), classified by their ``kind`` token.
* ``hibernate_disposition`` — WHY the folded leg did or did not actuate when it produced NO typed
  attempts (review j#87154 R1-F3): a RAISED leg (``hibernate_leg_error``) is an UNCERTAIN mutation
  status that must surface as ``uncertain > 0`` / never an empty pass even though it yields zero
  attempts; the budget / delivery-uncertain / wake-unbound / unwired defers surface WHY hibernate
  stood down this pass. Without this a fail-closed leg looked empty and healthy.

The attempt-``kind`` tokens are the application ``hibernate_actuation_leg`` vocabulary and the
disposition tokens are the domain ``SKIP_HIBERNATE_*`` constants; both are kept here as literals so
the domain imports no application leg and this leaf is not circular with ``workspace_supervisor``
(a drift-guard test pins them against their definitions).
"""

from __future__ import annotations

from typing import Iterable, Sequence

# -- attempt-kind classification (``hibernate_actuation_leg`` ATTEMPT_* token literals) -----------
#: A candidate actually hibernated/redriven this pass (the lane state MUTATED). Split into the
#: capacity that was actually freed vs. the mutations that freed NO slot (review j#87154 R1-F4).
_FULLY_RELEASED_KINDS = frozenset({"actuated", "redriven"})
_RELEASE_INCOMPLETE_KINDS = frozenset({"actuated_release_incomplete", "redriven_success_withheld"})
_HIBERNATE_APPLIED_KINDS = _FULLY_RELEASED_KINDS | _RELEASE_INCOMPLETE_KINDS
_HIBERNATE_BLOCKED_KINDS = frozenset(
    {"blocked", "redrive_blocked", "no_basis_journal", "release_state_unknown"}
)
_HIBERNATE_UNCERTAIN_KINDS = frozenset({"lease_lost"})
_HIBERNATE_DEFERRED_KINDS = frozenset({"deferred", "stale_basis"})

# -- folded-leg disposition classification (domain ``SKIP_HIBERNATE_*`` token literals) -----------
#: A RAISED leg — an UNCERTAIN mutation status (it may have mutated before throwing).
_DISPOSITION_LEG_ERROR = "hibernate_leg_error"
#: The leg stood down WITHOUT actuating (a typed defer): the pass's one mutation was already spent,
#: the delivery leg was uncertain, this local_wake pass had no wake binding, or no leg is wired.
_DISPOSITION_DEFER_TOKENS = frozenset(
    {
        "hibernate_budget_deferred",
        "hibernate_delivery_uncertain",
        "hibernate_wake_unbound",
        "hibernate_leg_unwired",
    }
)


def _attempts(workspaces: Sequence) -> "list[dict]":
    return [a for w in workspaces for a in w.hibernate_attempts]


def _count_kinds(workspaces: Sequence, kinds: Iterable[str]) -> int:
    allowed = frozenset(kinds)
    return sum(1 for a in _attempts(workspaces) if str(a.get("kind") or "") in allowed)


def _dispositions(workspaces: Sequence) -> "list[str]":
    return [str(getattr(w, "hibernate_disposition", "") or "") for w in workspaces]


def _leg_error_workspaces(workspaces: Sequence) -> int:
    return sum(1 for d in _dispositions(workspaces) if d == _DISPOSITION_LEG_ERROR)


def ran(workspaces: Sequence) -> bool:
    """True iff the folded leg engaged this pass — a typed run OR a RAISED (uncertain) leg.

    A pure defer (budget / delivery-uncertain / wake-unbound / unwired) did NOT run.
    """
    return any(w.hibernate_ran for w in workspaces) or _leg_error_workspaces(workspaces) > 0


def candidates(workspaces: Sequence) -> int:
    """Every candidate the folded hibernate leg evaluated to a typed attempt this pass."""
    return sum(len(w.hibernate_attempts) for w in workspaces)


def claimed(workspaces: Sequence) -> int:
    """Candidates the leg actually ACTED ON — applied, blocked at actuation, or uncertain.

    Distinct from every enumerated candidate (a deferred/stale candidate was never claimed) and
    from the applied-lane count. A RAISED leg claimed a candidate whose outcome is unknown, so each
    ``hibernate_leg_error`` workspace counts as one claimed candidate.
    """
    claimable = _HIBERNATE_APPLIED_KINDS | _HIBERNATE_BLOCKED_KINDS | _HIBERNATE_UNCERTAIN_KINDS
    return _count_kinds(workspaces, claimable) + _leg_error_workspaces(workspaces)


def applied(workspaces: Sequence) -> int:
    """Lanes actually hibernated/redriven this pass — the authoritative applied count (0 or 1)."""
    return sum(w.hibernate_mutations for w in workspaces)


def released_capacity(workspaces: Sequence) -> int:
    """Process slots ACTUALLY freed this pass (review j#87176 R2-F2): the sum of each attempt's
    real ``released`` count (``len(ReleaseOutcome.closed)``), NOT the number of actuated attempts.

    A lane whose CAS applied but whose release was ``not_requested`` (no live slot / dead process)
    mutated the lane yet freed ZERO slots, so it contributes 0 here even though it counts in
    :func:`applied`. The report therefore never reports freed capacity that no process release
    produced, and this metric is on a different axis from the applied-lane count.
    """
    return sum(int(a.get("released") or 0) for a in _attempts(workspaces))


def release_incomplete(workspaces: Sequence) -> int:
    """Applied lanes that mutated but freed FEWER-than-actuated slots — a real close count of 0.

    An APPLIED attempt (``actuated`` / ``redriven`` / partial / withheld) whose real ``released``
    count is 0 mutated the lifecycle row but released no process slot (``not_requested`` / withheld /
    a wholly-failed partial). Surfaced so a lane mutation with no freed capacity is never invisible.
    """
    return sum(
        1
        for a in _attempts(workspaces)
        if str(a.get("kind") or "") in _HIBERNATE_APPLIED_KINDS and int(a.get("released") or 0) == 0
    )


def blocked(workspaces: Sequence) -> int:
    return _count_kinds(workspaces, _HIBERNATE_BLOCKED_KINDS)


def uncertain(workspaces: Sequence) -> int:
    """Actuations of UNKNOWN effect — a lease lost mid-actuation OR a RAISED leg (review R1-F3)."""
    return _count_kinds(workspaces, _HIBERNATE_UNCERTAIN_KINDS) + _leg_error_workspaces(workspaces)


def deferred(workspaces: Sequence) -> int:
    """Candidates/passes that stood down without actuating — typed defers, observable by reason."""
    attempt_defers = _count_kinds(workspaces, _HIBERNATE_DEFERRED_KINDS)
    disposition_defers = sum(1 for d in _dispositions(workspaces) if d in _DISPOSITION_DEFER_TOKENS)
    return attempt_defers + disposition_defers


def closed_reasons(workspaces: Sequence) -> "tuple[str, ...]":
    """The distinct, redaction-safe BLOCKED block-reason tokens (no paths, no secrets)."""
    reasons = {
        str(a.get("reason") or "")
        for a in _attempts(workspaces)
        if str(a.get("kind") or "") in _HIBERNATE_BLOCKED_KINDS and a.get("reason")
    }
    return tuple(sorted(reasons))


def deferred_reasons(workspaces: Sequence) -> "tuple[str, ...]":
    """The distinct, redaction-safe reasons the leg deferred (attempt tokens + disposition tokens)."""
    reasons = {
        str(a.get("reason") or "")
        for a in _attempts(workspaces)
        if str(a.get("kind") or "") in _HIBERNATE_DEFERRED_KINDS and a.get("reason")
    }
    reasons |= {d for d in _dispositions(workspaces) if d in _DISPOSITION_DEFER_TOKENS}
    return tuple(sorted(reasons))


def payload(workspaces: Sequence, duration_ms: int) -> dict:
    """The secret-free auto-hibernate observability roll-up for this pass.

    ``pass_duration_ms`` is the whole run-once sweep's wall-clock (review j#87154 R1-F4): it is
    NAMED for what it measures and is NOT mislabelled a candidate drain-ready → terminal-disposition
    latency, which needs a drain-ready-timestamp authority this pass roll-up does not carry.
    """
    return {
        "ran": ran(workspaces),
        "candidates": candidates(workspaces),
        "claimed": claimed(workspaces),
        "applied": applied(workspaces),
        "blocked": blocked(workspaces),
        "uncertain": uncertain(workspaces),
        "deferred": deferred(workspaces),
        "released_capacity": released_capacity(workspaces),
        "release_incomplete": release_incomplete(workspaces),
        "closed_reasons": list(closed_reasons(workspaces)),
        "deferred_reasons": list(deferred_reasons(workspaces)),
        "pass_duration_ms": duration_ms,
    }


__all__ = (
    "ran",
    "candidates",
    "claimed",
    "applied",
    "released_capacity",
    "release_incomplete",
    "blocked",
    "uncertain",
    "deferred",
    "closed_reasons",
    "deferred_reasons",
    "payload",
)
