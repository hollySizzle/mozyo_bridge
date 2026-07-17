"""Redmine #13892 — public guarded retirement of a session-start scratch pair.

Acceptance 5 asks for deterministic coverage of success / identity ambiguity / foreign slot
/ partial close / retry / missing-and-unreadable inventory. These pin BOTH directions:

- **fail-open** probes — nothing may close on an unproven fact (the ordinary guard tests);
- **over-block** probes — an over-block is equally a defect here, because a scratch pair
  with no other retirement rail that this surface refuses is stuck *forever*, which is the
  exact defect the ticket removes (#13845's lesson, `managed-state-model.md`). So the
  partial-close resume, the stale-residue close, and the unattested-pair close are pinned
  as REQUIRED behaviour, not tolerated behaviour.
"""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    REASON_CLOSE_FAILED,
    REASON_LANE_IS_DEFAULT,
    REASON_LANE_REQUIRED,
    run_session_retire,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.scratch_pair_retire import (  # noqa: E501
    REASON_AGENT_NOT_IDLE,
    REASON_AMBIGUOUS_LOCATOR,
    REASON_DUPLICATE_INVENTORY,
    REASON_EXPECTED_IDENTITY_UNRESOLVED,
    REASON_FOREIGN_INVENTORY_PRESENT,
    REASON_INVENTORY_UNREADABLE,
    REASON_LANE_RECORD_PRESENT,
    REASON_PENDING_COMPOSER,
    STATE_ABSENT,
    STATE_BLOCKED,
    STATE_GREEN,
)

LANE = "dogfood13892"
GATEWAY = "codex"
WORKER = "claude"


class _CloseResult:
    def __init__(self, closed=(), failed=()):
        self.closed = tuple(closed)
        self.failed = tuple(failed)


class FakeOps:
    """A scriptable stand-in for the live herdr / store seam.

    Records every close so a test can assert **zero-close**, which is the property most of
    these gates actually protect.
    """

    def __init__(
        self,
        rows,
        *,
        record_absent=True,
        runtime="awaiting_input",
        composer=(True, False),
        rows_raise=False,
        close_failed=(),
    ):
        self._rows = rows
        self._record_absent = record_absent
        self._runtime = runtime
        self._composer = composer
        self._rows_raise = rows_raise
        self._close_failed = tuple(close_failed)
        self.close_calls = []
        self.recorded = []

    def agent_rows(self):
        if self._rows_raise:
            raise RuntimeError("herdr inventory unreadable")
        return self._rows

    def runtime_state(self, locator):
        if isinstance(self._runtime, dict):
            return self._runtime.get(locator, "awaiting_input")
        return self._runtime

    def observe_composer(self, locator):
        if isinstance(self._composer, dict):
            return self._composer.get(locator, (True, False))
        return self._composer

    def lifecycle_record_absent(self, workspace_id, lane_id):
        return self._record_absent

    def close(self, workspace_id, lane_id, targets):
        self.close_calls.append(tuple(targets))
        if self._close_failed:
            return _CloseResult(closed=(), failed=self._close_failed)
        return _CloseResult(closed=tuple(targets))

    def record_retirement(self, *, workspace_id, lane_id, intent):
        self.recorded.append(intent)
        return "recorded"


class ScratchPairRetireTest(unittest.TestCase):
    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        # The workspace segment the surface derives from this repo root; the test builds
        # its fixture names with the SAME encoder the product uses, so a name-shape change
        # cannot make these tests silently stop matching.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            herdr_workspace_segment,
        )

        self.ws = herdr_workspace_segment(self.repo_root)
        self.assertTrue(self.ws, "the fixture needs a resolvable workspace segment")
        self.gw_name = encode_assigned_name(self.ws, GATEWAY, LANE)
        self.wk_name = encode_assigned_name(self.ws, WORKER, LANE)

    def _args(self, *, execute=False, lane=LANE):
        return argparse.Namespace(lane=lane, execute=execute, json=False, repo=None)

    def _row(self, name, *, locator="%1", agent=None):
        row = {"name": name, "pane": locator}
        if agent is not None:
            row["agent"] = agent
        return row

    def _pair_rows(self, *, gw_locator="%1", wk_locator="%2"):
        return [
            self._row(self.gw_name, locator=gw_locator, agent=GATEWAY),
            self._row(self.wk_name, locator=wk_locator, agent=WORKER),
        ]

    def _run(self, ops, **kw):
        return run_session_retire(self._args(**kw), self.repo_root, ops=ops)

    # -- control: the harness itself resolves the pair -------------------------

    def test_control_fixture_resolves_the_pair(self):
        """The fixture names must actually match, or every guard test is vacuous."""
        ops = FakeOps(self._pair_rows())
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertEqual(
            sorted(result.expected_names), sorted([self.gw_name, self.wk_name])
        )

    # -- acceptance 5: success -------------------------------------------------

    def test_success_closes_exactly_the_pair_and_records_durable_outcome(self):
        ops = FakeOps(self._pair_rows())
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN)
        self.assertTrue(result.ok)
        self.assertEqual(len(ops.close_calls), 1)
        self.assertEqual(
            sorted(ops.close_calls[0]), sorted([(GATEWAY, "%1"), (WORKER, "%2")])
        )
        self.assertEqual(result.durable_retirement, "recorded")
        self.assertEqual(len(ops.recorded), 1)

    def test_read_only_by_default_closes_nothing(self):
        ops = FakeOps(self._pair_rows())
        result = self._run(ops, execute=False)
        self.assertEqual(result.state, STATE_GREEN)
        self.assertEqual(ops.close_calls, [], "a preflight must never close")
        self.assertEqual(ops.recorded, [], "a preflight must never write")

    def test_unattested_pair_is_retirable(self):
        """Over-block probe: the #13882 preserved pair is live-and-UNATTESTED.

        Requiring attestation (as #13842 does, for a surface that writes generation pins)
        would make this rail unable to retire the only shape it exists for — a permanent
        stuck that reproduces the ticket's own defect. No attestation is consulted.
        """
        ops = FakeOps(self._pair_rows())
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN)
        self.assertEqual(len(ops.close_calls), 1)

    # -- acceptance 5: identity ambiguity --------------------------------------

    def test_duplicate_assigned_name_is_ambiguous_and_closes_nothing(self):
        rows = self._pair_rows() + [
            self._row(self.wk_name, locator="%3", agent=WORKER)
        ]
        ops = FakeOps(rows)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_DUPLICATE_INVENTORY)
        self.assertEqual(ops.close_calls, [])

    def test_two_slots_at_one_locator_is_ambiguous(self):
        ops = FakeOps(self._pair_rows(gw_locator="%1", wk_locator="%1"))
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_AMBIGUOUS_LOCATOR)
        self.assertEqual(ops.close_calls, [])

    def test_present_slot_without_locator_is_unresolved_not_gone(self):
        rows = [
            self._row(self.gw_name, locator="%1", agent=GATEWAY),
            {"name": self.wk_name, "agent": WORKER},  # live agent, no locator
        ]
        ops = FakeOps(rows)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_EXPECTED_IDENTITY_UNRESOLVED)
        self.assertEqual(ops.close_calls, [])

    # -- acceptance 5: foreign slot --------------------------------------------

    def test_foreign_occupant_in_the_unit_closes_nothing(self):
        """live-zero is not unoccupied: a foreign occupant must fail closed on its own."""
        foreign = encode_assigned_name(self.ws, "gemini", LANE)
        rows = self._pair_rows() + [self._row(foreign, locator="%9", agent="gemini")]
        ops = FakeOps(rows)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_FOREIGN_INVENTORY_PRESENT)
        self.assertEqual(ops.close_calls, [])
        self.assertTrue(result.foreign_names)

    def test_other_lane_and_default_lane_pairs_are_never_touched(self):
        """Scope: the coordinator's default-lane pair and other lanes stay untouched."""
        other = encode_assigned_name(self.ws, WORKER, "issue_99999_other")
        coordinator = encode_assigned_name(self.ws, GATEWAY, "")  # default lane
        rows = self._pair_rows() + [
            self._row(other, locator="%7", agent=WORKER),
            self._row(coordinator, locator="%8", agent=GATEWAY),
        ]
        ops = FakeOps(rows)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN)
        closed = ops.close_calls[0]
        self.assertEqual(sorted(closed), sorted([(GATEWAY, "%1"), (WORKER, "%2")]))
        for _role, locator in closed:
            self.assertNotIn(locator, ("%7", "%8"))

    def test_default_lane_is_refused(self):
        ops = FakeOps([])
        result = self._run(ops, execute=True, lane="default")
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_LANE_IS_DEFAULT)
        self.assertEqual(ops.close_calls, [])

    def test_lane_is_required(self):
        ops = FakeOps([])
        result = self._run(ops, execute=True, lane="")
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_LANE_REQUIRED)
        self.assertEqual(ops.close_calls, [])

    # -- acceptance 3/5: partial close + retry ---------------------------------

    def test_partial_close_resumes_the_remaining_slot(self):
        """Over-block probe: a slot closed by a prior run must NOT strand the retire.

        Blocking a half pair (as #13842's `pair_incomplete` does, for a surface that
        re-binds a whole unit) would make every interrupted retire permanently stuck.
        """
        rows = [self._row(self.wk_name, locator="%2", agent=WORKER)]  # gateway gone
        ops = FakeOps(rows)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertEqual(ops.close_calls, [((WORKER, "%2"),)])

    def test_second_run_is_idempotent_and_closes_nothing(self):
        ops = FakeOps([])  # both slots already gone
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_ABSENT)
        self.assertTrue(result.ok, "a proven absence is a successful idempotent replay")
        self.assertEqual(ops.close_calls, [])

    def test_failed_close_reports_what_committed_and_is_not_ok(self):
        ops = FakeOps(
            self._pair_rows(), close_failed=((GATEWAY, "%1", "herdr refused"),)
        )
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_CLOSE_FAILED)
        self.assertFalse(result.ok)
        self.assertTrue(result.failed)

    # -- acceptance 5: missing / unreadable inventory --------------------------

    def test_unreadable_inventory_closes_nothing(self):
        ops = FakeOps([], rows_raise=True)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_INVENTORY_UNREADABLE)
        self.assertEqual(ops.close_calls, [])

    # -- the surface's own signature -------------------------------------------

    def test_lane_with_a_lifecycle_record_is_refused(self):
        ops = FakeOps(self._pair_rows(), record_absent=False)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_LANE_RECORD_PRESENT)
        self.assertEqual(ops.close_calls, [])

    def test_unreadable_lifecycle_store_is_not_read_as_absent(self):
        """`None` (unreadable) must never be folded into "record-less"."""
        ops = FakeOps(self._pair_rows(), record_absent=None)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_LANE_RECORD_PRESENT)
        self.assertEqual(ops.close_calls, [])

    def test_never_writes_a_lifecycle_record(self):
        """Acceptance 4: the durable outcome is an audit record, never a minted row."""
        ops = FakeOps(self._pair_rows())
        self.assertFalse(hasattr(ops, "declare_lane"))
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN)
        # The only durable write the surface performs is the audit append.
        self.assertEqual(len(ops.recorded), 1)
        self.assertNotIn("lane_disposition", ops.recorded[0])

    # -- obligation gates ------------------------------------------------------

    def test_busy_agent_closes_nothing(self):
        ops = FakeOps(self._pair_rows(), runtime="busy")
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_AGENT_NOT_IDLE)
        self.assertEqual(ops.close_calls, [])

    def test_unknown_runtime_closes_nothing(self):
        ops = FakeOps(self._pair_rows(), runtime="unknown")
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_AGENT_NOT_IDLE)
        self.assertEqual(ops.close_calls, [])

    def test_pending_composer_closes_nothing(self):
        ops = FakeOps(self._pair_rows(), composer=(True, True))
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_PENDING_COMPOSER)
        self.assertEqual(ops.close_calls, [])

    def test_unreadable_composer_is_not_read_as_settled(self):
        ops = FakeOps(self._pair_rows(), composer=(False, None))
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_PENDING_COMPOSER)
        self.assertEqual(ops.close_calls, [])

    def test_only_one_busy_slot_still_blocks_the_whole_pair(self):
        ops = FakeOps(self._pair_rows(), runtime={"%1": "awaiting_input", "%2": "busy"})
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_AGENT_NOT_IDLE)
        self.assertEqual(ops.close_calls, [])

    def test_stale_shell_residue_is_closed_not_stranded(self):
        """Over-block probe: positively-dead residue has no turn / composer to protect.

        Its runtime reads `unknown`, so gating on the runtime alone would strand it
        forever — progress requires a positive proof of deadness, which `classify_named_slot`
        supplies here (`agent` present-but-blank = shell residue).
        """
        rows = [
            self._row(self.gw_name, locator="%1", agent=""),  # residue
            self._row(self.wk_name, locator="%2", agent=""),  # residue
        ]
        ops = FakeOps(rows, runtime="unknown", composer=(False, None))
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertEqual(
            sorted(ops.close_calls[0]), sorted([(GATEWAY, "%1"), (WORKER, "%2")])
        )


if __name__ == "__main__":
    unittest.main()
