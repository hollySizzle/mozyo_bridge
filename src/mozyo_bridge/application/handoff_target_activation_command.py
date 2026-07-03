"""OOP-first boundary for the handoff target-activation tail (Redmine #13124).

The ``orchestrate_handoff`` strict-rail body in ``application/commands.py``
historically carried the standard_target_admission *activation/restore* helper
tail (Redmine #12597) inline:

- ``_window_active_pane_id`` — best-effort id of the currently-active pane in
  the target's window, read from a live ``pane_lines()`` snapshot, so the
  durable record can show which pane was active before the rail activated the
  target.
- ``_activate_target_pane`` — activate an admitted inactive split via
  ``tmux select-pane`` (pane SELECTION only — never raw ``send-keys`` /
  ``paste-buffer`` / low-level ``type`` / ``keys`` as a delivery recovery
  path), capturing the previously-active pane first.
- the post-delivery restore plumbing on the sent terminal path — if the
  admission policy asks to restore focus, re-select the previously-active pane
  best-effort (a vanished pane must not break the already-completed send) and
  record the restore fact.

This module carves that tail into an OOP-first boundary under #12638, aligning
with the existing ``handoff_command.py`` entry boundary (#12936) and the
``handoff_delivery_command.py`` delivery-rendering boundary (#12981),
**without touching** the admission gate itself
(``evaluate_standard_target_admission`` /
``resolve_standard_target_admission_policy``), the deferred-activation decision
in ``orchestrate_handoff``, the queue-enter rail semantics, or the wire enums
(all out of #13124 scope):

- :class:`TargetActivationOps` is the port for the two environment
  dependencies the tail needs (the ``tmux`` runner and the live pane
  snapshot), so :class:`TargetActivationUseCase` is exercisable with a
  synthetic fake — no live tmux.
- :class:`TargetActivationUseCase` holds the three bodies
  (``window_active_pane_id`` / ``activate_target_pane`` /
  ``maybe_restore_previous_active``).
- :class:`LiveTargetActivationOps` routes ``run_tmux`` through the
  :mod:`commands` module *at call time* and ``pane_lines`` through the
  :mod:`pane_resolver` module at call time, so the existing ``handoff`` CLI
  characterization tests (which patch the low-level
  ``mozyo_bridge.application.commands.run_tmux`` and
  ``pane_resolver.pane_lines`` seams and drive ``orchestrate_handoff`` for
  real) keep intercepting the side effects unchanged and no import cycle is
  introduced.

This is a behavior-preserving restructuring: the ``select-pane`` wording, the
activation/restore ordering around the typed send, the best-effort failure
degrade (an observation or restore failure must never break delivery), and the
:class:`TargetActivationOutcome` facts recorded in the durable record are
byte-for-byte identical to the original bodies.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    TargetActivationOutcome,
)


# --------------------------------------------------------------------------- #
# Port
# --------------------------------------------------------------------------- #


@runtime_checkable
class TargetActivationOps(Protocol):
    """The environment dependencies the target-activation tail needs.

    Only the two non-pure dependencies are injected: the ``tmux`` runner (kept
    routed through ``commands.run_tmux`` so pane selection is interceptable
    exactly as before) and the live pane snapshot (kept routed through
    ``pane_resolver.pane_lines`` so the observation seam is unchanged).
    """

    def run_tmux(self, *args: str) -> Any:
        """Run a tmux command, matching ``commands.run_tmux``."""
        ...

    def pane_lines(self) -> Iterable[dict]:
        """A live pane snapshot, matching ``pane_resolver.pane_lines``."""
        ...


# --------------------------------------------------------------------------- #
# Use case
# --------------------------------------------------------------------------- #


class TargetActivationUseCase:
    """The standard_target_admission activation/restore tail behind the port.

    Each method encodes one of the original ``commands`` bodies; behavior is
    byte-for-byte identical to the procedural originals (Redmine #12597).
    """

    def __init__(self, ops: TargetActivationOps) -> None:
        self._ops = ops

    def window_active_pane_id(self, target_info: dict) -> str | None:
        """Best-effort id of the currently-active pane in the target's window.

        Reads a live `pane_lines()` snapshot (Redmine #12597) and returns the id
        of the *other* pane that is the active split of the target pane's
        window, so the durable record can show which pane was active before
        standard_target_admission activated the target. Returns `None` when it
        cannot be observed (no window location, snapshot failure, or no other
        active pane); a failure here must never break delivery.
        """
        location = target_info.get("location") or ""
        window_prefix = location.rsplit(".", 1)[0] if "." in location else location
        if not window_prefix:
            return None
        try:
            for pane in self._ops.pane_lines():
                if pane.get("id") == target_info.get("id"):
                    continue
                pane_loc = pane.get("location") or ""
                pane_window = (
                    pane_loc.rsplit(".", 1)[0] if "." in pane_loc else pane_loc
                )
                if pane_window == window_prefix and pane.get("pane_active") == "1":
                    return pane.get("id")
        except (Exception, SystemExit):
            return None
        return None

    def activate_target_pane(self, target_info: dict) -> TargetActivationOutcome:
        """Activate an admitted inactive split via `tmux select-pane` (Redmine #12597).

        Pane SELECTION only — never raw `send-keys` / `paste-buffer` / low-level
        `type` / `keys` as a delivery recovery path. Captures the
        previously-active pane first so the durable record can show the active化
        / restore facts; the optional restore runs after delivery on the sent
        terminal path (:meth:`maybe_restore_previous_active`).
        """
        target = target_info["id"]
        previous = self.window_active_pane_id(target_info)
        self._ops.run_tmux("select-pane", "-t", target)
        return TargetActivationOutcome(
            activated=True,
            target_pane=target,
            previous_active_pane=previous,
            restored=False,
        )

    def maybe_restore_previous_active(
        self,
        target_activation: TargetActivationOutcome | None,
        *,
        restore_previous_active: bool,
    ) -> TargetActivationOutcome | None:
        """Post-delivery focus restore on the sent terminal path (Redmine #12597).

        If standard_target_admission activated an inactive split and the policy
        asks to restore focus, re-select the previously-active pane after
        delivery. Pane selection only, best-effort (a vanished pane must not
        break the already-completed send), and the restore fact is recorded.
        Returns the activation outcome unchanged when the restore does not
        engage (no activation, policy off, no observed previous pane) or when
        the re-select fails.
        """
        if (
            target_activation is None
            or not restore_previous_active
            or not target_activation.previous_active_pane
        ):
            return target_activation
        try:
            self._ops.run_tmux(
                "select-pane", "-t", target_activation.previous_active_pane
            )
            return TargetActivationOutcome(
                activated=True,
                target_pane=target_activation.target_pane,
                previous_active_pane=target_activation.previous_active_pane,
                restored=True,
            )
        except (Exception, SystemExit):
            return target_activation


# --------------------------------------------------------------------------- #
# Live adapter
# --------------------------------------------------------------------------- #


class LiveTargetActivationOps:
    """Live :class:`TargetActivationOps`.

    ``run_tmux`` resolves *through the* :mod:`commands` *module at call time* so
    a monkeypatched ``commands.run_tmux`` still intercepts the pane selection;
    ``pane_lines`` resolves through the :mod:`pane_resolver` module at call time
    via the same lazy import the original helper used, so the CLI
    characterization tests that patch ``pane_resolver.pane_lines`` keep feeding
    the snapshot and no import cycle is introduced.
    """

    def run_tmux(self, *args: str) -> Any:
        from mozyo_bridge.application import commands as _commands

        return _commands.run_tmux(*args)

    def pane_lines(self) -> Iterable[dict]:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain import (
            pane_resolver as _pr,
        )

        return _pr.pane_lines()
