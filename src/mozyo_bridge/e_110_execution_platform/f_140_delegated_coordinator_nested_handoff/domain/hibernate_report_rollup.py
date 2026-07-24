"""Auto-hibernate observability roll-up (Redmine #14219 T3, Design Consultation Answer j#87108).

Pure classification of the folded hibernate leg's per-workspace outcomes into the secret-free
report metrics the T3 acceptance asks for — ``ran`` / candidate / applied / blocked / uncertain /
deferred / released capacity / closed reason / time-to-drain. Split out of ``workspace_supervisor``
to keep that module under the module-health line ceiling; it reads only the redaction-safe
``hibernate_attempts`` dicts (issue / lane / kind / reason / revision) + ``hibernate_mutations`` /
``hibernate_ran`` a :class:`WorkspaceSupervisionOutcome` carries, so it stays domain-pure and
imports no outcome type.

The attempt-``kind`` tokens are the application ``hibernate_actuation_leg`` vocabulary, kept as
literals so the domain imports no application leg (a drift-guard test pins them against the leg).
"""

from __future__ import annotations

from typing import Iterable, Sequence

_HIBERNATE_APPLIED_KINDS = frozenset(
    {"actuated", "actuated_release_incomplete", "redriven", "redriven_success_withheld"}
)
_HIBERNATE_BLOCKED_KINDS = frozenset(
    {"blocked", "redrive_blocked", "no_basis_journal", "release_state_unknown"}
)
_HIBERNATE_UNCERTAIN_KINDS = frozenset({"lease_lost"})
_HIBERNATE_DEFERRED_KINDS = frozenset({"deferred", "stale_basis"})


def _attempts(workspaces: Sequence) -> "list[dict]":
    return [a for w in workspaces for a in w.hibernate_attempts]


def _count_kinds(workspaces: Sequence, kinds: Iterable[str]) -> int:
    allowed = frozenset(kinds)
    return sum(1 for a in _attempts(workspaces) if str(a.get("kind") or "") in allowed)


def ran(workspaces: Sequence) -> bool:
    return any(w.hibernate_ran for w in workspaces)


def candidates(workspaces: Sequence) -> int:
    """Every candidate the folded hibernate leg evaluated this pass (one typed attempt each)."""
    return sum(len(w.hibernate_attempts) for w in workspaces)


def applied(workspaces: Sequence) -> int:
    """Lanes actually hibernated/redriven this pass — the authoritative applied count (0 or 1)."""
    return sum(w.hibernate_mutations for w in workspaces)


def blocked(workspaces: Sequence) -> int:
    return _count_kinds(workspaces, _HIBERNATE_BLOCKED_KINDS)


def uncertain(workspaces: Sequence) -> int:
    return _count_kinds(workspaces, _HIBERNATE_UNCERTAIN_KINDS)


def deferred(workspaces: Sequence) -> int:
    return _count_kinds(workspaces, _HIBERNATE_DEFERRED_KINDS)


def closed_reasons(workspaces: Sequence) -> "tuple[str, ...]":
    """The distinct, redaction-safe closed block-reason tokens (no paths, no secrets)."""
    reasons = {
        str(a.get("reason") or "")
        for a in _attempts(workspaces)
        if str(a.get("kind") or "") in _HIBERNATE_BLOCKED_KINDS and a.get("reason")
    }
    return tuple(sorted(reasons))


def payload(workspaces: Sequence, duration_ms: int) -> dict:
    """The secret-free auto-hibernate observability roll-up for this pass."""
    return {
        "ran": ran(workspaces),
        "candidates": candidates(workspaces),
        "applied": applied(workspaces),
        "blocked": blocked(workspaces),
        "uncertain": uncertain(workspaces),
        "deferred": deferred(workspaces),
        "released_capacity": applied(workspaces),
        "closed_reasons": list(closed_reasons(workspaces)),
        "time_to_drain_ms": duration_ms,
    }


__all__ = (
    "ran",
    "candidates",
    "applied",
    "blocked",
    "uncertain",
    "deferred",
    "closed_reasons",
    "payload",
)
