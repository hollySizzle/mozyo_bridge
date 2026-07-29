"""The close-boundary fence for the guarded live-worker refresh (Redmine #14661 j#92487 F2).

Split out of :mod:`.sublane_worker_refresh_live` at the responsibility seam — that module
OBSERVES the live world, while this one is an actuation **port**: it sits on the destructive
edge and decides whether the close may proceed. Keeping them together pushed the observer over
the module-health line, and an allowlist entry would have recorded the growth instead of
resolving it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WorkerRefreshRequest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
)


@dataclass
class SettledCloseBoundaryPort:
    """The shared #13806 actuation port, fenced on a POSITIVELY SETTLED worker (j#92487 F2).

    The shared close boundary reduces the runtime to one boolean —
    ``running_process = (state == busy)`` — and
    :func:`...replacement_preservation.assess_worker_recovery_preservation` decides ``may_close``
    from it. Measured over the whole herdr status vocabulary, that admits a close on every
    non-``working`` state:

    ============= ================ ==============
    herdr status  runtime state    may_close
    ============= ================ ==============
    ``working``   busy             False (correct)
    ``done``      turn_ended       True  (correct)
    ``idle``      awaiting_input   True  (correct)
    ``blocked``   blocked          **True — a live agent at a permission prompt**
    (absent)      unknown          **True — an unreadable observation**
    (novel token) unknown          **True — an unrecognised state**
    ============= ================ ==============

    The preflight requires ``settled_idle``; this restores that requirement at the boundary
    that actually closes, and adds the composer re-read the preflight also performs. It is a
    THIN wrapper: identity, lane lifecycle and row-revision re-verification stay entirely with
    the shared implementation (no second implementation to drift), and this only ever turns a
    ``may_close`` into a refusal — never the reverse.

    Scoped to this surface deliberately. The same fail-open exists for ``recover-stale`` and
    ``recover-gateway``, but those modules are outside this task's changed-path boundary and
    are not silently retuned here (the coordinator's j#92454 disposition); the gap is reported
    instead.
    """

    inner: object
    ops: "LiveWorkerRefreshOps"
    request: WorkerRefreshRequest
    #: ``() -> bool`` — True only when a FRESH durable read still shows this worker's turn as
    #: failed (no progress landed). Supplied for a first close, ``None`` for a post-close
    #: replay, where the close already committed and re-litigating it would refuse every
    #: legitimate replay. Review j#92601 F3: the use case read progress before the lane
    #: authority, the target re-observation, and the actuator's own claim/lease/preservation
    #: reads — so a gate could still land in that window and the worker was closed anyway.
    #: The last durable observation has to sit on the destructive edge, which is here.
    progress_still_failed: object = None

    #: Forwarded so :func:`...replacement_launch_failure.port_launch_failure_reason` reads the
    #: INNER port's typed diagnostic rather than seeing an attribute-less wrapper.
    @property
    def launch_failure_reason(self) -> str:
        return getattr(self.inner, "launch_failure_reason", "")

    def observe_old_slot(self, pin):
        return self.inner.observe_old_slot(pin)

    def observe_preservation(self, pin):
        observation = self.inner.observe_preservation(pin)
        if not observation.identity_matches:
            return observation  # the shared fence already refuses; do not mask its detail
        state = self.ops.pinned_runtime_state(self.request)
        if state not in (RUNTIME_TURN_ENDED, RUNTIME_AWAITING_INPUT):
            # ``running_process`` is the closed reason meaning "closing would destroy live
            # work". A ``blocked`` slot is a live agent awaiting a permission answer, and an
            # ``unknown`` / absent / novel state cannot prove it is not one — fail-closed. The
            # concrete axis travels in ``detail`` so the refusal is diagnosable without adding
            # a token to a shared closed vocabulary.
            return replace(
                observation, running_process=True, detail=f"worker_not_settled:{state}"
            )
        if not self.ops._composer_clear(self.request):
            return replace(
                observation, running_process=True, detail="pending_composer_input"
            )
        guard = self.progress_still_failed
        if guard is not None:
            try:
                still_failed = bool(guard())
            except Exception:  # noqa: BLE001 - an unreadable durable authority never permits
                still_failed = False
            if not still_failed:
                # Landed, ambiguous or unreadable — all three refuse. The worker may have
                # written its gate moments ago; closing now destroys exactly the work this
                # surface exists to preserve.
                return replace(
                    observation, running_process=True, detail="durable_progress_moved"
                )
        return observation

    def close_exact_generation(self, pin):
        return self.inner.close_exact_generation(pin)

    def launch_action_bound(self, action_id: str, pin):
        return self.inner.launch_action_bound(action_id, pin)

    def verify_attestation(self, action_id: str, pin):
        return self.inner.verify_attestation(action_id, pin)


__all__ = ("SettledCloseBoundaryPort",)
