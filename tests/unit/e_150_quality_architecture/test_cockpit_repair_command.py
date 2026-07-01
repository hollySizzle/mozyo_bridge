"""Fake-port specifications for the cockpit repair executor boundary (#12972).

These exercise the ``cockpit_repair_command`` use case directly with a synthetic
:class:`CockpitRepairOps` (an in-memory ``run`` recorder + a ``die`` that raises)
— no real tmux server. They pin:

- the pure ``result_detail`` extraction (``stderr`` preferred, else ``stdout``,
  else ``""``),
- the adopt transaction: both joins + best-effort stamp on the happy path, the
  codex-pane rollback when a later join fails, and the "rollback failed" wording
  when the rollback itself fails,
- the peer-adopt transaction: identity binds recorded, and the reverse-order
  ``set-option -u`` unbind when a later bind fails,
- the three fail-fast destructive paths (reset / rebalance / reconcile): every
  step run in order on success, and a fail-closed ``die`` with the exact
  per-action wording (plus reconcile's "no pane was killed" recovery hint) on the
  first non-zero exit.

The end-to-end behavior over the live ``commands`` seams stays pinned by the
cockpit adopt / peer-adopt / reset / rebalance / reconcile characterization
tests; this file pins the boundary in isolation.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application.cockpit_repair_command import (
    CockpitRepairUseCase,
    LiveCockpitRepairOps,
    result_detail,
)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> argparse.Namespace:
    return argparse.Namespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _Cmd:
    """A minimal stand-in for a plan step: an ``argv`` tuple and a ``purpose``."""

    def __init__(self, argv, purpose: str) -> None:
        self.argv = tuple(argv)
        self.purpose = purpose


class _Die(Exception):
    """Raised by the fake port's ``die`` so aborts terminate like the real thing."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class FakeRepairOps:
    """In-memory :class:`CockpitRepairOps`: records ``run`` argv, ``die`` raises.

    ``results`` maps the argv-0 verb to a queue of results (or a single result);
    an unmapped verb defaults to a zero-exit result. ``die`` raises :class:`_Die`
    so a fail-closed path terminates rather than falling through.
    """

    def __init__(self, results=None) -> None:
        self.calls: list[tuple] = []
        self._results = results or {}

    def run(self, *args, **kwargs):
        self.calls.append(args)
        spec = self._results.get(args[0])
        if isinstance(spec, list):
            return spec.pop(0) if spec else _result()
        if spec is not None:
            return spec
        return _result()

    def die(self, message: str):
        raise _Die(message)


class ResultDetailTest(unittest.TestCase):
    def test_prefers_stripped_stderr(self) -> None:
        self.assertEqual("boom", result_detail(_result(stderr="  boom  ", stdout="out")))

    def test_falls_back_to_stdout_then_empty(self) -> None:
        self.assertEqual("out", result_detail(_result(stdout=" out ")))
        self.assertEqual("", result_detail(_result()))
        # A result missing both attributes reads as empty, never raises.
        self.assertEqual("", result_detail(object()))


class AdoptUseCaseTest(unittest.TestCase):
    def _plan(self):
        return argparse.Namespace(
            join_commands=[
                _Cmd(("join-pane", "-h", "-s", "%2", "-t", "%9"), "join codex"),
                _Cmd(("join-pane", "-v", "-s", "%3", "-t", "%2"), "join claude"),
            ],
            stamp_commands=[
                _Cmd(("set-option", "-p", "-t", "%2", "@mozyo_agent_role", "codex"), "stamp codex"),
            ],
            source_codex_pane="%2",
            source_claude_pane="%3",
            source_session="mozyo-ws",
        )

    def test_happy_path_runs_joins_then_stamps_no_warnings(self) -> None:
        ops = FakeRepairOps()
        result = CockpitRepairUseCase(ops).execute_adopt(self._plan())
        self.assertEqual({"stamp_warnings": []}, result)
        verbs = [c[0] for c in ops.calls]
        self.assertEqual(["join-pane", "join-pane", "set-option"], verbs)

    def test_stamp_failure_is_warning_not_rollback(self) -> None:
        ops = FakeRepairOps(results={"set-option": _result(returncode=1, stderr="nope")})
        result = CockpitRepairUseCase(ops).execute_adopt(self._plan())
        self.assertEqual(["stamp codex: nope"], result["stamp_warnings"])
        # No rollback join issued: the pair is adopted, only the stamp warned.
        self.assertEqual(2, sum(1 for c in ops.calls if c[0] == "join-pane"))

    def test_second_join_failure_rolls_codex_back_and_dies(self) -> None:
        ops = FakeRepairOps(
            results={"join-pane": [_result(), _result(returncode=1, stderr="boom")]}
        )
        with self.assertRaises(_Die) as caught:
            CockpitRepairUseCase(ops).execute_adopt(self._plan())
        # The codex pane is moved back beside the source claude pane (rollback).
        self.assertIn(("join-pane", "-h", "-s", "%2", "-t", "%3"), ops.calls)
        self.assertIn("rolled back: codex pane %2 moved", caught.exception.message)

    def test_rollback_failure_reports_manual_recovery(self) -> None:
        # First join ok, second join fails, and the rollback join also fails.
        ops = FakeRepairOps(
            results={
                "join-pane": [
                    _result(),
                    _result(returncode=1, stderr="boom"),
                    _result(returncode=1, stderr="stuck"),
                ]
            }
        )
        with self.assertRaises(_Die) as caught:
            CockpitRepairUseCase(ops).execute_adopt(self._plan())
        self.assertIn("rollback failed", caught.exception.message)
        self.assertIn("it was NOT killed", caught.exception.message)


class PeerAdoptUseCaseTest(unittest.TestCase):
    def _plan(self):
        return argparse.Namespace(
            pane_id="%7",
            stamp_commands=[
                _Cmd(("set-option", "-p", "-t", "%7", "@mozyo_workspace_id", "wsA"), "bind ws"),
                _Cmd(("set-option", "-p", "-t", "%7", "@mozyo_agent_role", "claude"), "bind role"),
                _Cmd(("set-option", "-p", "-t", "%7", "@mozyo_lane_id", "lane-x"), "bind lane"),
            ],
        )

    def test_happy_path_binds_all_options(self) -> None:
        ops = FakeRepairOps()
        self.assertIsNone(CockpitRepairUseCase(ops).execute_peer_adopt(self._plan()))
        self.assertEqual(3, len(ops.calls))
        self.assertNotIn("-u", [tok for c in ops.calls for tok in c])

    def test_mid_bind_failure_unsets_earlier_options_reverse_order(self) -> None:
        # The lane bind (3rd) fails after ws + role bound; route by inspecting the
        # argv so only the lane bind (and never an unbind) returns non-zero.
        ops = FakeRepairOps()

        def run(*args, **kwargs):
            ops.calls.append(args)
            if args[0] == "set-option" and "-u" not in args and args[-2] == "@mozyo_lane_id":
                return _result(returncode=1, stderr="boom")
            return _result()

        ops.run = run
        with self.assertRaises(_Die) as caught:
            CockpitRepairUseCase(ops).execute_peer_adopt(self._plan())
        unsets = [c[-1] for c in ops.calls if c[0] == "set-option" and "-u" in c]
        self.assertEqual(["@mozyo_agent_role", "@mozyo_workspace_id"], unsets)
        self.assertIn("rolled back 2 identity option(s)", caught.exception.message)


class FailFastUseCaseTest(unittest.TestCase):
    def _plan(self, verb: str, target: str):
        return argparse.Namespace(
            commands=[
                _Cmd((verb, "-t", target, "-x", "40"), f"step A {target}"),
                _Cmd((verb, "-t", target, "-x", "50"), f"step B {target}"),
            ]
        )

    def test_reset_runs_all_steps_on_success(self) -> None:
        ops = FakeRepairOps()
        CockpitRepairUseCase(ops).execute_reset(self._plan("kill-session", "s"))
        self.assertEqual(2, len(ops.calls))

    def test_reset_dies_on_first_failure(self) -> None:
        ops = FakeRepairOps(results={"kill-session": _result(returncode=1, stderr="boom")})
        with self.assertRaises(_Die) as caught:
            CockpitRepairUseCase(ops).execute_reset(self._plan("kill-session", "s"))
        self.assertIn("cockpit reset step failed (step A s)", caught.exception.message)
        self.assertIn("-> boom", caught.exception.message)
        # Fail-fast: the second step never ran.
        self.assertEqual(1, len(ops.calls))

    def test_rebalance_dies_with_rebalance_wording(self) -> None:
        ops = FakeRepairOps(results={"resize-pane": _result(returncode=1)})
        with self.assertRaises(_Die) as caught:
            CockpitRepairUseCase(ops).execute_rebalance(self._plan("resize-pane", "%1"))
        self.assertIn("cockpit rebalance step failed", caught.exception.message)
        # Empty detail falls back to the "nonzero exit" sentinel.
        self.assertIn("-> nonzero exit", caught.exception.message)

    def test_reconcile_dies_with_no_pane_killed_hint(self) -> None:
        ops = FakeRepairOps(results={"swap-pane": _result(returncode=1, stderr="boom")})
        with self.assertRaises(_Die) as caught:
            CockpitRepairUseCase(ops).execute_reconcile(self._plan("swap-pane", "%1"))
        message = caught.exception.message
        self.assertIn("cockpit reconcile step failed", message)
        self.assertIn(
            "No pane was killed; re-run `mozyo cockpit reconcile` to continue "
            "from the current live layout.",
            message,
        )


class LiveAdapterRoutingTest(unittest.TestCase):
    def test_run_uses_injected_callable(self) -> None:
        seen = []

        def fake_run(*args, **kwargs):
            seen.append((args, kwargs))
            return _result(stdout="ok")

        ops = LiveCockpitRepairOps(fake_run)
        result = ops.run("list-panes", "-t", "s", check=False)
        self.assertEqual("ok", result.stdout)
        self.assertEqual([(("list-panes", "-t", "s"), {"check": False})], seen)

    def test_die_routes_through_commands_module_at_call_time(self) -> None:
        from unittest.mock import patch

        from mozyo_bridge.application import commands

        ops = LiveCockpitRepairOps(lambda *a, **k: _result())
        with patch.object(commands, "die", side_effect=RuntimeError("routed")) as spy:
            with self.assertRaises(RuntimeError):
                ops.die("boom")
        spy.assert_called_once_with("boom")


if __name__ == "__main__":
    unittest.main()
