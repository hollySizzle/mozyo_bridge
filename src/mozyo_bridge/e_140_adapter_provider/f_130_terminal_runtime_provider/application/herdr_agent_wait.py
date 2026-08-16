"""Shared argv builder for the Herdr 0.8 agent wait surface (Redmine #15198).

Herdr 0.8 exposes the event wait as
``herdr agent wait <target> --until <status> --timeout <ms>``.  Keeping that
grammar in one pure builder prevents the turn-start, callback-wake, and
reconcile-pump callers from drifting onto different CLI generations.
"""

from __future__ import annotations


def build_herdr_agent_wait_argv(
    binary: object,
    target: object,
    *,
    until: object,
    timeout_ms: object,
) -> list[str]:
    """Return the fixed-shell-free Herdr 0.8 agent-wait argv."""

    return [
        str(binary),
        "agent",
        "wait",
        str(target),
        "--until",
        str(until),
        "--timeout",
        str(timeout_ms),
    ]


__all__ = ("build_herdr_agent_wait_argv",)
