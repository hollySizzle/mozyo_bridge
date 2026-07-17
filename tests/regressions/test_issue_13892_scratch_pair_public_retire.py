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
    REASON_OBLIGATION_UNREADABLE,
    REASON_POST_CLOSE_RESIDUE,
    REASON_POST_CLOSE_UNREADABLE,
    REASON_RETIRE_EVIDENCE_ABSENT,
    REASON_WORK_OBLIGATION_PRESENT,
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

    ``fail_roles`` mirrors the production close executor's **per-target, non-fatal** contract
    (``execute_herdr_retire_close``): each target closes or fails independently, so
    "one closed + one failed" is a reachable production state and must be reachable here too
    (review j#80506 F5 — the first double returned ``closed=()`` whenever anything failed,
    which made a real partial commit inexpressible and the "reports what committed" assertion
    vacuous).

    A close also REMOVES the closed rows from the inventory, so the post-close re-measure and
    a re-run observe the world the closes actually produced rather than a frozen fixture.
    """

    def __init__(
        self,
        rows,
        *,
        record_absent=True,
        runtime="awaiting_input",
        composer=(True, False),
        rows_raise=False,
        fail_roles=(),
        obligations=(),
        rows_raise_after_close=False,
        residue_after_close=False,
    ):
        self._rows = list(rows)
        self._record_absent = record_absent
        self._runtime = runtime
        self._composer = composer
        self._rows_raise = rows_raise
        self._fail_roles = tuple(fail_roles)
        self._obligations = obligations
        self._rows_raise_after_close = rows_raise_after_close
        self._residue_after_close = residue_after_close
        self._closed_any = False
        self.close_calls = []
        self.recorded = []

    def agent_rows(self):
        if self._rows_raise:
            raise RuntimeError("herdr inventory unreadable")
        if self._closed_any and self._rows_raise_after_close:
            raise RuntimeError("herdr inventory unreadable after close")
        return list(self._rows)

    def open_obligations(self, workspace_id, assigned_names):
        return self._obligations

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
        closed, failed = [], []
        for role, locator in targets:
            if role in self._fail_roles:
                failed.append((role, locator, "herdr refused"))
            else:
                closed.append((role, locator))
        self._closed_any = bool(closed)
        if not self._residue_after_close:
            # A committed close removes that row: the next read sees the real post-close world.
            closed_locators = {loc for _r, loc in closed}
            self._rows = [
                row for row in self._rows if row.get("pane") not in closed_locators
            ]
        return _CloseResult(closed=tuple(closed), failed=tuple(failed))

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

    def test_zero_slot_without_evidence_is_not_a_retirement(self):
        """j#80506 F1: absence proves the pair is not here, never that WE retired it."""
        ops = FakeOps([])  # nothing live — a completed retire, or a lane that never existed
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_RETIRE_EVIDENCE_ABSENT)
        self.assertFalse(result.ok, "exit 0 must mean a proven retire")
        self.assertEqual(ops.close_calls, [])
        self.assertEqual(ops.recorded, [], "zero-close must also be zero-write")

    def test_mistyped_lane_does_not_report_success(self):
        """j#80506 F1: the operator-facing consequence — a typo must not read as retired."""
        ops = FakeOps(self._pair_rows())  # the REAL pair is live under the intended label
        result = self._run(ops, execute=True, lane="dogfood13892_typo")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_RETIRE_EVIDENCE_ABSENT)
        self.assertEqual(ops.close_calls, [], "the real pair must be untouched")

    def test_partial_commit_then_rerun_closes_the_remainder(self):
        """j#80506 F5: one closed + one failed in ONE execute, then a real re-run.

        The production close executor is per-target non-fatal, so this state is reachable;
        the first double could not express it.
        """
        ops = FakeOps(self._pair_rows(), fail_roles=(WORKER,))
        first = self._run(ops, execute=True)
        # Run 1: the gateway close committed, the worker close failed.
        self.assertEqual(first.state, STATE_BLOCKED)
        self.assertEqual(first.reason, REASON_CLOSE_FAILED)
        self.assertFalse(first.ok)
        self.assertEqual(first.closed, ((GATEWAY, "%1"),), "must report what committed")
        self.assertEqual(len(first.failed), 1)
        self.assertEqual(ops.recorded, [], "a partial close is not a retirement")

        # Run 2: the gateway slot is now positively absent; the worker slot must still close.
        ops._fail_roles = ()
        second = self._run(ops, execute=True)
        self.assertEqual(second.state, STATE_GREEN, second.detail)
        self.assertEqual(ops.close_calls[-1], ((WORKER, "%2"),))
        self.assertEqual(len(ops.recorded), 1, "the completed retire is recorded once")

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

    # -- j#80506 F3: the close command's return code is not proof of emptiness ---

    def test_ack_success_but_still_live_is_not_a_retirement(self):
        """F3: the close 'succeeded' yet the unit is still occupied -> no success, no record."""
        ops = FakeOps(self._pair_rows(), residue_after_close=True)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_POST_CLOSE_RESIDUE)
        self.assertFalse(result.ok)
        self.assertEqual(result.closed, ((GATEWAY, "%1"), (WORKER, "%2")),
                         "the committed closes are still reported")
        self.assertEqual(ops.recorded, [], "no durable retirement over a non-empty unit")

    def test_post_close_foreign_arrival_withholds_success(self):
        """F3: a foreign occupant appearing at the unit withholds the retirement."""
        ops = FakeOps(self._pair_rows())
        foreign = encode_assigned_name(self.ws, "gemini", LANE)
        original_close = ops.close

        def close_then_foreign(ws, lane, targets):
            out = original_close(ws, lane, targets)
            ops._rows.append(self._row(foreign, locator="%9", agent="gemini"))
            return out

        ops.close = close_then_foreign
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_POST_CLOSE_RESIDUE)
        self.assertEqual(ops.recorded, [])

    def test_post_close_unreadable_inventory_withholds_success(self):
        """F3: emptiness unproven -> not a retirement, but the closes are reported."""
        ops = FakeOps(self._pair_rows(), rows_raise_after_close=True)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_POST_CLOSE_UNREADABLE)
        self.assertTrue(result.closed)
        self.assertEqual(ops.recorded, [])

    # -- j#80506 F4: durable obligations are not a runtime reading ------------

    def test_durable_work_obligation_blocks_the_close(self):
        """F4: a reserved dispatch is owed to a slot -> zero-close, even when idle."""
        ops = FakeOps(
            self._pair_rows(), obligations=((f"{self.wk_name}", "reserved"),)
        )
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
        self.assertEqual(ops.close_calls, [], "never close over owed work")

    def test_uncertain_obligation_blocks_the_close(self):
        ops = FakeOps(self._pair_rows(), obligations=((f"{self.gw_name}", "uncertain"),))
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
        self.assertEqual(ops.close_calls, [])

    def test_unreadable_obligation_store_blocks_the_close(self):
        """F4: an obligation we cannot observe is not an obligation that is absent."""
        ops = FakeOps(self._pair_rows(), obligations=None)
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_OBLIGATION_UNREADABLE)
        self.assertEqual(ops.close_calls, [])

    def test_obligation_gate_also_refuses_in_read_only_preflight(self):
        ops = FakeOps(self._pair_rows(), obligations=((f"{self.wk_name}", "reserved"),))
        result = self._run(ops, execute=False)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
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
