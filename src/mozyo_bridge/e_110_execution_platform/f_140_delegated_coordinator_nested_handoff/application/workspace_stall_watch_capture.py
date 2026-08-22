"""Fold the stall-watch leg's outcome into the sweep's telemetry (Redmine #15855).

The composition root (:mod:`...application.workspace_callback_supervisor`) invokes an
INJECTED stall-watch leg, so it must not import the observation feature to interpret what
came back. This leaf is the interpretation: it takes whatever the injected callable
returned (or raised) and turns it into one redaction-safe dict for
:attr:`SupervisorReport.stall_watch`.

Split out for the same two reasons the retire and hibernate legs were: it keeps the
composition root inside the module-health line budget
(``vibes/docs/logics/module-health-gate.md``), and it is a genuinely separable concern —
nothing here decides anything about the sweep, it only projects a result.

Why a failed leg is reported rather than swallowed
--------------------------------------------------
The leg is wrapped so a broken watcher can never break a supervisor sweep. But an earlier
version discarded the result entirely, which made "the watcher ran and found nothing" and
"the watcher blew up on every tick" produce byte-identical operator output — the shape
review j#110132 finding_3 named. A raise now yields a :data:`STALL_WATCH_LEG_ERROR` entry
carrying the exception *type* (never its message, which could quote a path or a screen), so
the failure is visible without becoming a leak.
"""

from __future__ import annotations

from typing import Callable, Optional

#: The reason token a raised leg reports. Distinct from every reason the leg itself emits.
STALL_WATCH_LEG_ERROR = "leg_error"


def capture_stall_watch(
    leg_fn: Optional[Callable[..., object]],
    workspace,
    *,
    pass_budget: Optional[dict] = None,
) -> Optional[dict]:
    """Run the injected leg and project its outcome, or ``None`` when no leg is wired.

    Never raises: a stall watcher is an observer, and an observer that can abort the sweep
    it observes would be worse than no observer at all.
    """
    if leg_fn is None:
        return None
    workspace_id = str(getattr(workspace, "workspace_id", "") or "")
    try:
        outcome = leg_fn(workspace, pass_budget=pass_budget)
    except Exception as exc:  # noqa: BLE001 - the watcher never breaks a sweep
        return {
            "workspace_id": workspace_id,
            "stall_watch_reason": STALL_WATCH_LEG_ERROR,
            # The TYPE only. An exception message can quote a path, a config value or a
            # screen, none of which may reach an operator surface from this rail.
            "error": type(exc).__name__,
        }
    telemetry = getattr(outcome, "telemetry", None)
    if not callable(telemetry):
        return None
    try:
        return dict(telemetry())
    except Exception:  # noqa: BLE001 - an unprojectable outcome is reported as absent
        return {
            "workspace_id": workspace_id,
            "stall_watch_reason": STALL_WATCH_LEG_ERROR,
            "error": "telemetry_unavailable",
        }


__all__ = ("STALL_WATCH_LEG_ERROR", "capture_stall_watch")
