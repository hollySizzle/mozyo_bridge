"""Fake-port specs for the handoff target-activation boundary (#13124).

These exercise the ``handoff_target_activation_command`` boundary directly —
the :class:`TargetActivationUseCase` bodies through a synthetic
:class:`TargetActivationOps` fake — with no live tmux, no
``orchestrate_handoff``. They pin, in isolation, the standard_target_admission
activation/restore tail carved out of ``commands.py`` (Redmine #12597):

- ``window_active_pane_id`` — the other-active-pane-in-window observation, the
  window-prefix matching, and the never-break-delivery degrade (no location /
  snapshot failure / no other active pane -> ``None``);
- ``activate_target_pane`` — observe-then-``select-pane`` ordering and the
  recorded :class:`TargetActivationOutcome` facts;
- ``maybe_restore_previous_active`` — the engage conditions (activation
  present + policy on + previous pane observed), the ``restored=True``
  re-record on success, and the best-effort keep-original degrade on a
  re-select failure.

The end-to-end behavior over the real ``commands.*`` seams +
``orchestrate_handoff`` stays pinned by the ``handoff`` CLI characterization
tests under ``tests/integration/.../f_130_handoff_routing/``
(``RelaxedQueueEnterRailTest`` asserts the live activation / restore /
``--no-target-activation`` rails); this file pins the extracted bodies in
isolation, which is the OOP-first carve's payoff.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.application.handoff_target_activation_command import (
    TargetActivationUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
    TargetActivationOutcome,
)


class _FakeActivationOps:
    """Recording fake :class:`TargetActivationOps`.

    Records an ordered call log and lets a test script the pane snapshot
    (``panes`` / ``pane_lines_raises``) and the tmux runner
    (``run_tmux_raises``).
    """

    def __init__(
        self,
        *,
        panes: list[dict] | None = None,
        pane_lines_raises: BaseException | None = None,
        run_tmux_raises: BaseException | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self._panes = panes or []
        self._pane_lines_raises = pane_lines_raises
        self._run_tmux_raises = run_tmux_raises

    def run_tmux(self, *args: str):
        self.calls.append(("run_tmux", *args))
        if self._run_tmux_raises is not None:
            raise self._run_tmux_raises
        return None

    def pane_lines(self) -> list[dict]:
        self.calls.append(("pane_lines",))
        if self._pane_lines_raises is not None:
            raise self._pane_lines_raises
        return self._panes


def _target(pane_id: str = "%2", location: str = "agents:0.1") -> dict:
    return {"id": pane_id, "location": location, "pane_active": "0"}


class WindowActivePaneIdTest(unittest.TestCase):
    def test_returns_other_active_pane_in_same_window(self) -> None:
        ops = _FakeActivationOps(
            panes=[
                {"id": "%2", "location": "agents:0.1", "pane_active": "0"},
                {"id": "%5", "location": "agents:1.0", "pane_active": "1"},
                {"id": "%3", "location": "agents:0.0", "pane_active": "1"},
            ]
        )
        result = TargetActivationUseCase(ops).window_active_pane_id(_target())
        # %5 is active but in another window; %3 is the same-window active split.
        self.assertEqual("%3", result)

    def test_skips_the_target_pane_itself(self) -> None:
        # The target row can carry a stale pane_active=1; the observation wants
        # the *other* pane, so the target's own row is skipped.
        ops = _FakeActivationOps(
            panes=[{"id": "%2", "location": "agents:0.1", "pane_active": "1"}]
        )
        self.assertIsNone(TargetActivationUseCase(ops).window_active_pane_id(_target()))

    def test_no_window_location_returns_none_without_snapshot(self) -> None:
        ops = _FakeActivationOps(panes=[{"id": "%3", "pane_active": "1"}])
        result = TargetActivationUseCase(ops).window_active_pane_id(
            {"id": "%2", "location": ""}
        )
        self.assertIsNone(result)
        # Short-circuits before the snapshot read.
        self.assertEqual([], ops.calls)

    def test_snapshot_failure_degrades_to_none(self) -> None:
        ops = _FakeActivationOps(pane_lines_raises=RuntimeError("tmux gone"))
        self.assertIsNone(TargetActivationUseCase(ops).window_active_pane_id(_target()))

    def test_snapshot_systemexit_degrades_to_none(self) -> None:
        # `pane_lines` can die() (SystemExit); observation must never break the
        # delivery, so SystemExit degrades to None exactly like an Exception.
        ops = _FakeActivationOps(pane_lines_raises=SystemExit(2))
        self.assertIsNone(TargetActivationUseCase(ops).window_active_pane_id(_target()))

    def test_no_other_active_pane_returns_none(self) -> None:
        ops = _FakeActivationOps(
            panes=[{"id": "%3", "location": "agents:0.0", "pane_active": "0"}]
        )
        self.assertIsNone(TargetActivationUseCase(ops).window_active_pane_id(_target()))


class ActivateTargetPaneTest(unittest.TestCase):
    def test_observes_previous_then_selects_target(self) -> None:
        ops = _FakeActivationOps(
            panes=[{"id": "%3", "location": "agents:0.0", "pane_active": "1"}]
        )
        outcome = TargetActivationUseCase(ops).activate_target_pane(_target())
        self.assertEqual(
            [("pane_lines",), ("run_tmux", "select-pane", "-t", "%2")],
            ops.calls,
        )
        self.assertEqual(
            TargetActivationOutcome(
                activated=True,
                target_pane="%2",
                previous_active_pane="%3",
                restored=False,
            ),
            outcome,
        )

    def test_unobserved_previous_pane_is_recorded_as_none(self) -> None:
        ops = _FakeActivationOps(pane_lines_raises=RuntimeError("no snapshot"))
        outcome = TargetActivationUseCase(ops).activate_target_pane(_target())
        # The observation degrade never blocks the select-pane activation.
        self.assertIn(("run_tmux", "select-pane", "-t", "%2"), ops.calls)
        self.assertIsNone(outcome.previous_active_pane)
        self.assertTrue(outcome.activated)
        self.assertFalse(outcome.restored)


class MaybeRestorePreviousActiveTest(unittest.TestCase):
    def _activation(self, previous: str | None = "%3") -> TargetActivationOutcome:
        return TargetActivationOutcome(
            activated=True,
            target_pane="%2",
            previous_active_pane=previous,
            restored=False,
        )

    def test_restores_and_records_restored_fact(self) -> None:
        ops = _FakeActivationOps()
        result = TargetActivationUseCase(ops).maybe_restore_previous_active(
            self._activation(), restore_previous_active=True
        )
        self.assertEqual([("run_tmux", "select-pane", "-t", "%3")], ops.calls)
        self.assertEqual(
            TargetActivationOutcome(
                activated=True,
                target_pane="%2",
                previous_active_pane="%3",
                restored=True,
            ),
            result,
        )

    def test_no_activation_is_a_no_op(self) -> None:
        ops = _FakeActivationOps()
        result = TargetActivationUseCase(ops).maybe_restore_previous_active(
            None, restore_previous_active=True
        )
        self.assertIsNone(result)
        self.assertEqual([], ops.calls)

    def test_policy_off_keeps_activation_unrestored(self) -> None:
        ops = _FakeActivationOps()
        activation = self._activation()
        result = TargetActivationUseCase(ops).maybe_restore_previous_active(
            activation, restore_previous_active=False
        )
        self.assertIs(activation, result)
        self.assertEqual([], ops.calls)

    def test_no_previous_pane_keeps_activation_unrestored(self) -> None:
        ops = _FakeActivationOps()
        activation = self._activation(previous=None)
        result = TargetActivationUseCase(ops).maybe_restore_previous_active(
            activation, restore_previous_active=True
        )
        self.assertIs(activation, result)
        self.assertEqual([], ops.calls)

    def test_reselect_failure_keeps_original_outcome(self) -> None:
        # Best-effort: a vanished previous pane must not break the
        # already-completed send; the unrestored activation fact is kept.
        ops = _FakeActivationOps(run_tmux_raises=RuntimeError("pane vanished"))
        activation = self._activation()
        result = TargetActivationUseCase(ops).maybe_restore_previous_active(
            activation, restore_previous_active=True
        )
        self.assertIs(activation, result)
        self.assertFalse(result.restored)

    def test_reselect_systemexit_keeps_original_outcome(self) -> None:
        ops = _FakeActivationOps(run_tmux_raises=SystemExit(2))
        activation = self._activation()
        result = TargetActivationUseCase(ops).maybe_restore_previous_active(
            activation, restore_previous_active=True
        )
        self.assertIs(activation, result)


class TargetActivationBoundaryHygieneTest(unittest.TestCase):
    def test_module_does_not_import_commands_at_load(self) -> None:
        # The live adapter imports ``commands`` lazily at call time to preserve
        # the monkeypatch seams and avoid an import cycle; the module must not
        # import it at load.
        import ast

        import mozyo_bridge.application.handoff_target_activation_command as mod

        with open(mod.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        top_level_imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                top_level_imports.append(node.module)
            elif isinstance(node, ast.Import):
                top_level_imports.extend(alias.name for alias in node.names)
        self.assertFalse([m for m in top_level_imports if "application.commands" in m])


if __name__ == "__main__":
    unittest.main()
