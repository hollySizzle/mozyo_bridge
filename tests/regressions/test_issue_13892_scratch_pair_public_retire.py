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
import shutil
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.dispatch_outbox_fence import TargetObligation
from mozyo_bridge.core.state.scratch_retirement_fence import (  # noqa: E501
    RETIRE_COMPLETED,
    RETIRE_PENDING,
    RetirementUnit,
    ScratchRetirementBusy,
    ScratchRetirementFence,
    ScratchRetirementFenceError,
    slot_digest,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_retire import (  # noqa: E501
    REASON_CLOSE_FAILED,
    REASON_COMPLETION_UNPROVEN,
    REASON_RETIREMENT_AUTHORITY_UNAVAILABLE,
    REASON_RETIREMENT_BUSY,
    REASON_PIN_DRIFT,
    REASON_SIGNATURE_LOST,
    STATE_ALREADY_RETIRED,
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
        self.fence = None  # set by the test fixture to a real fence over a temp home

    def agent_rows(self):
        if self._rows_raise:
            raise RuntimeError("herdr inventory unreadable")
        if self._closed_any and self._rows_raise_after_close:
            raise RuntimeError("herdr inventory unreadable after close")
        return list(self._rows)

    def open_obligations(self, workspace_id, assigned_names):
        return self._obligations

    def retirement_transaction(self, unit, *, live_pair_present):
        return self.fence.transaction(unit, live_pair_present=live_pair_present)

    def peek_retirement(self, unit):
        return self.fence.peek(unit)

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

    def _fence(self):
        """A REAL fence over a throwaway home: the lock / replay / identity semantics under
        test must be production's, not a mock's (the test-double fidelity rule)."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return ScratchRetirementFence(home=Path(d))

    def _run(self, ops, **kw):
        if getattr(ops, "fence", None) is None:
            ops.fence = self._fence()
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
        self.assertEqual(result.durable_retirement, "fence_completed",
                         "the fence row is the load-bearing durable outcome")
        self.assertEqual(len(ops.recorded), 1, "managed_events is appended AFTER the fence")

    def test_read_only_by_default_closes_nothing(self):
        ops = FakeOps(self._pair_rows())
        result = self._run(ops, execute=False)
        self.assertEqual(result.state, STATE_GREEN)
        self.assertEqual(ops.close_calls, [], "a preflight must never close")
        self.assertEqual(ops.recorded, [], "a preflight must never write")

    def test_read_only_preflight_creates_no_authority_artifact(self):
        """j#80523 R3-F4: the old test watched close/audit only, so a preflight that
        BOOTSTRAPPED the authority (db + seal) sailed through it."""
        ops = FakeOps(self._pair_rows())
        ops.fence = self._fence()
        f = ops.fence
        before = {
            "db": f.path.exists(),
            "seal": f.seal_path.exists(),
            "lock": f.lock_path.exists(),
            "temp": f.temp_path.exists(),
        }
        result = self._run(ops, execute=False)
        after = {
            "db": f.path.exists(),
            "seal": f.seal_path.exists(),
            "lock": f.lock_path.exists(),
            "temp": f.temp_path.exists(),
        }
        self.assertEqual(result.state, STATE_GREEN)
        self.assertFalse(result.executed)
        self.assertEqual(
            before, after,
            "a --execute-less preflight must leave every authority artifact untouched "
            "(including the lock file: open(O_CREAT) is a write)",
        )
        self.assertEqual(after, {"db": False, "seal": False, "lock": False, "temp": False})

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
            self._pair_rows(),
            obligations=(
                TargetObligation(self.wk_name, "reserved", issue="13999", journal="1"),
            ),
        )
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
        self.assertEqual(ops.close_calls, [], "never close over owed work")

    def test_uncertain_obligation_blocks_the_close(self):
        ops = FakeOps(
            self._pair_rows(),
            obligations=(TargetObligation(self.gw_name, "uncertain", issue="13999"),),
        )
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
        ops = FakeOps(
            self._pair_rows(),
            obligations=(TargetObligation(self.wk_name, "reserved", issue="13999"),),
        )
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


class ScratchRetirementFenceTest(unittest.TestCase):
    """The retirement authority itself (Redmine #13892, design j#80526).

    Drives the REAL store: the lock, the artifact inventory and the identity checks are the
    properties under test, so a mock would test nothing.
    """

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        self.fence = ScratchRetirementFence(home=self.home)
        self.unit = RetirementUnit("ws1", "lane1", slot_digest(["mzb1_a", "mzb1_b"]))
        self.pins = (("codex", "%1"), ("claude", "%2"))

    def _open(self, *, live=True):
        return self.fence.transaction(self.unit, live_pair_present=live)

    # -- identity ----------------------------------------------------------

    def test_slot_digest_is_order_independent_and_deduped(self):
        self.assertEqual(slot_digest(["b", "a"]), slot_digest(["a", "b"]))
        self.assertEqual(slot_digest(["a", "b", "a"]), slot_digest(["a", "b"]))
        self.assertNotEqual(slot_digest(["a", "b"]), slot_digest(["a", "c"]))
        with self.assertRaises(ValueError):
            slot_digest([])

    def test_a_different_slot_set_is_a_different_unit(self):
        with self._open() as txn:
            txn.reserve(pinned=self.pins)
            txn.mark_completed(attempt_id=txn.current().attempt_id, closed=self.pins)
        other = RetirementUnit("ws1", "lane1", slot_digest(["mzb1_a", "mzb1_c"]))
        with self.fence.transaction(other, live_pair_present=False) as txn:
            self.assertIsNone(txn.current(), "a different pair must not inherit the proof")

    # -- bootstrap ---------------------------------------------------------

    def test_zero_slot_never_bootstraps_and_reports_no_attempt(self):
        with self._open(live=False) as txn:
            self.assertIsNone(txn.current())
            self.assertFalse(txn.bootstrapped)
        self.assertFalse(self.fence.path.exists(), "a zero-slot run must not create the store")

    def test_first_bootstrap_requires_a_live_pair(self):
        with self._open(live=True) as txn:
            self.assertTrue(txn.bootstrapped)
        self.assertTrue(self.fence.path.exists())
        self.assertTrue(self.fence.seal_path.exists())

    # -- store damage: every shape fails closed ----------------------------

    def _bootstrap(self):
        with self._open(live=True):
            pass

    def test_seal_without_db_fails_closed(self):
        self._bootstrap()
        self.fence.path.unlink()
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True):
                pass

    def test_db_without_seal_fails_closed(self):
        self._bootstrap()
        self.fence.seal_path.unlink()
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True):
                pass

    def test_orphan_wal_is_not_read_as_absent(self):
        """A stray SQLite sidecar is evidence something was here — never a fresh world."""
        (self.home / (ScratchRetirementFence(home=self.home).path.name + "-wal")).write_text("x")
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True):
                pass

    def test_broken_symlink_artifact_counts_as_present(self):
        """`lexists` semantics: a dangling link is still evidence (j#80526)."""
        self.fence.seal_path.symlink_to(self.home / "nowhere")
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True):
                pass

    def test_seal_mismatch_fails_closed(self):
        self._bootstrap()
        self.fence.seal_path.write_text("deadbeef")
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True) as txn:
                txn.current()

    def test_unknown_schema_fails_closed(self):
        self._bootstrap()
        import sqlite3

        conn = sqlite3.connect(self.fence.path)
        conn.execute("PRAGMA user_version = 9999")
        conn.commit()
        conn.close()
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True) as txn:
                txn.current()

    def test_corrupt_db_fails_closed(self):
        self._bootstrap()
        self.fence.path.write_bytes(b"not sqlite")
        with self.assertRaises(ScratchRetirementFenceError):
            with self._open(live=True) as txn:
                txn.current()

    # -- concurrency -------------------------------------------------------

    def test_second_caller_is_busy_and_never_waits(self):
        with self._open(live=True):
            with self.assertRaises(ScratchRetirementBusy):
                with self.fence.transaction(self.unit, live_pair_present=True):
                    pass


    def test_lock_is_exclusive_across_processes(self):
        """The real risk is two CLI invocations, not two threads.

        An in-process assertion could pass on a re-entrant lock while two separate
        `herdr session-retire` processes both entered the same unit and closed it twice.
        """
        import os
        import subprocess
        import sys
        import textwrap

        child = textwrap.dedent(
            f"""
            from pathlib import Path
            from mozyo_bridge.core.state.scratch_retirement_fence import (
                ScratchRetirementFence, RetirementUnit, ScratchRetirementBusy, slot_digest)
            f = ScratchRetirementFence(home=Path({str(self.home)!r}))
            u = RetirementUnit("ws1", "lane1", slot_digest(["mzb1_a", "mzb1_b"]))
            try:
                with f.transaction(u, live_pair_present=True):
                    print("ACQUIRED")
            except ScratchRetirementBusy:
                print("BUSY")
            """
        )
        env = {**os.environ, "PYTHONPATH": "src"}
        repo = Path(__file__).resolve().parents[2]
        with self._open(live=True):
            out = subprocess.run(
                [sys.executable, "-c", child], env=env, cwd=repo,
                capture_output=True, text=True,
            )
            self.assertEqual(out.stdout.strip(), "BUSY", out.stderr[-300:])
        out = subprocess.run(
            [sys.executable, "-c", child], env=env, cwd=repo,
            capture_output=True, text=True,
        )
        self.assertEqual(
            out.stdout.strip(), "ACQUIRED",
            "the lock must be released when the transaction exits",
        )

    def test_lock_is_released_after_the_transaction(self):
        with self._open(live=True):
            pass
        with self._open(live=True) as txn:  # must not raise
            self.assertIsNone(txn.current())

    # -- replay ------------------------------------------------------------

    def test_reserve_then_complete(self):
        with self._open(live=True) as txn:
            a = txn.reserve(pinned=self.pins)
            self.assertTrue(a.pending)
            txn.mark_completed(attempt_id=a.attempt_id, closed=self.pins)
            self.assertTrue(txn.current().completed)

    def test_crash_at_reserve_leaves_a_resumable_pending(self):
        with self._open(live=True) as txn:
            txn.reserve(pinned=self.pins)  # "crash" — no completion
        with self._open(live=True) as txn:
            cur = txn.current()
            self.assertIsNotNone(cur)
            self.assertTrue(cur.pending, "a crashed attempt must be resumable, not stuck")

    def test_partial_close_progress_survives_a_crash(self):
        with self._open(live=True) as txn:
            a = txn.reserve(pinned=self.pins)
            txn.record_progress(attempt_id=a.attempt_id, closed=(("codex", "%1"),))
        with self._open(live=True) as txn:
            self.assertEqual(txn.current().closed, (("codex", "%1"),))

    def test_completion_cas_rejects_a_stale_attempt(self):
        with self._open(live=True) as txn:
            stale = txn.reserve(pinned=self.pins).attempt_id
            txn.reserve(pinned=self.pins)  # a newer attempt supersedes it
            with self.assertRaises(ScratchRetirementFenceError):
                txn.mark_completed(attempt_id=stale, closed=self.pins)

    def test_relaunch_after_completed_opens_a_new_attempt(self):
        """The same deterministic names relaunched must NOT inherit the old completion."""
        with self._open(live=True) as txn:
            a = txn.reserve(pinned=self.pins)
            txn.mark_completed(attempt_id=a.attempt_id, closed=self.pins)
            first_rev = txn.current().revision
        with self._open(live=True) as txn:
            b = txn.reserve(pinned=self.pins)  # a live pair reappeared at the same names
            self.assertTrue(b.pending)
            self.assertGreater(b.revision, first_rev)

    # -- status ------------------------------------------------------------

    def test_status_reports_absent_readable_and_damaged(self):
        self.assertEqual(self.fence.status()["store_state"], "absent")
        self._bootstrap()
        self.assertEqual(self.fence.status()["store_state"], "present")
        self.assertTrue(self.fence.status()["readable"])
        self.fence.seal_path.unlink()
        st = self.fence.status()
        self.assertEqual(st["store_state"], "damaged")
        self.assertIn("db", st["present_artifacts"])


class ScratchPairRetireReplayTest(ScratchPairRetireTest):
    """End-to-end replay of the retirement transaction through the real entry point."""

    def test_second_run_after_success_is_idempotent_exit_zero(self):
        """j#80526: an exact prior completion + a live-zero now is a SUCCESS, not exit 1."""
        fence = self._fence()
        ops = FakeOps(self._pair_rows())
        ops.fence = fence
        first = self._run(ops, execute=True)
        self.assertEqual(first.state, STATE_GREEN)

        second_ops = FakeOps([])  # the panes are gone now
        second_ops.fence = fence  # ...but the SAME authority proves we retired them
        second = self._run(second_ops, execute=True)
        self.assertEqual(second.state, STATE_ALREADY_RETIRED)
        self.assertTrue(second.ok, "a proven replay must exit 0")
        self.assertEqual(second_ops.close_calls, [], "and close nothing")

    def test_completion_failure_is_not_success_then_repairs(self):
        """j#80506 F2: the load-bearing write fails -> non-success; the next run repairs."""
        fence = self._fence()
        ops = FakeOps(self._pair_rows())
        ops.fence = fence
        real_txn = fence.transaction

        class _BreakComplete:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._t = self._inner.__enter__()
                return self

            def __exit__(self, *a):
                return self._inner.__exit__(*a)

            def __getattr__(self, name):
                return getattr(self._t, name)

            def mark_completed(self, **kw):
                raise ScratchRetirementFenceError("simulated completion write failure")

        fence.transaction = lambda u, **kw: _BreakComplete(real_txn(u, **kw))
        first = self._run(ops, execute=True)
        self.assertEqual(first.state, STATE_BLOCKED)
        self.assertEqual(first.reason, REASON_COMPLETION_UNPROVEN)
        self.assertFalse(first.ok)
        self.assertEqual(len(first.closed), 2, "the closes that committed are reported")
        self.assertEqual(ops.recorded, [], "no audit over an unproven completion")

        # The pending attempt survives: a re-run re-measures and repairs to completed.
        fence.transaction = real_txn
        repair_ops = FakeOps([])  # panes already gone
        repair_ops.fence = fence
        second = self._run(repair_ops, execute=True)
        self.assertEqual(second.state, STATE_GREEN, second.detail)
        self.assertEqual(second.durable_retirement, "fence_completed")
        self.assertEqual(repair_ops.close_calls, [], "repair closes nothing further")

    def test_audit_append_failure_does_not_invalidate_a_proven_retirement(self):
        """j#80526: managed_events is lossy narrative; the fence is the authority."""
        fence = self._fence()
        ops = FakeOps(self._pair_rows())
        ops.fence = fence
        ops.record_retirement = lambda **kw: "not_recorded:append_failed"
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_GREEN, result.detail)
        self.assertTrue(result.ok, "a lossy audit failure must not invalidate the fence proof")
        self.assertEqual(result.durable_retirement, "fence_completed")
        self.assertEqual(result.audit_record, "not_recorded:append_failed")

    def test_busy_unit_closes_nothing(self):
        fence = self._fence()
        ops = FakeOps(self._pair_rows())
        ops.fence = fence
        unit = RetirementUnit(
            workspace_id=self.ws,
            lane_id=LANE,
            slot_digest=slot_digest([self.gw_name, self.wk_name]),
        )
        with fence.transaction(unit, live_pair_present=True):
            result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_RETIREMENT_BUSY)
        self.assertEqual(ops.close_calls, [], "a busy unit must never be closed into")

    def test_signature_lost_mid_flight_withholds_completion(self):
        """j#80523 R2-F3: the unit gained a lifecycle record during the retire."""
        fence = self._fence()
        ops = FakeOps(self._pair_rows())
        ops.fence = fence
        state = {"closed": False}
        real_close = ops.close

        def close_then_declare(ws, lane, targets):
            out = real_close(ws, lane, targets)
            state["closed"] = True
            return out

        ops.close = close_then_declare
        ops.lifecycle_record_absent = lambda ws, lane: not state["closed"]
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_SIGNATURE_LOST)
        self.assertTrue(result.closed, "the committed closes are still reported")
        self.assertEqual(ops.recorded, [])

    def test_late_obligation_after_close_withholds_completion(self):
        """j#80523 R2-F2: an obligation reserved DURING the close is re-read before completing."""
        fence = self._fence()
        ops = FakeOps(self._pair_rows())
        ops.fence = fence
        state = {"closed": False}
        real_close = ops.close

        def close_then_reserve(ws, lane, targets):
            out = real_close(ws, lane, targets)
            state["closed"] = True
            return out

        ops.close = close_then_reserve
        ops.open_obligations = lambda ws, names: (
            (TargetObligation(self.wk_name, "reserved", issue="13999"),)
            if state["closed"]
            else ()
        )
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
        self.assertEqual(ops.recorded, [], "no completion over newly owed work")

    def test_delivered_obligation_without_correlation_blocks(self):
        """j#80526: a delivery ACK is not task completion; uncorrelatable -> fail closed."""
        ops = FakeOps(
            self._pair_rows(),
            obligations=(TargetObligation(self.wk_name, "delivered", issue="13999",
                                          journal="42"),),
        )
        result = self._run(ops, execute=True)
        self.assertEqual(result.state, STATE_BLOCKED)
        self.assertEqual(result.reason, REASON_WORK_OBLIGATION_PRESENT)
        self.assertEqual(ops.close_calls, [])


class ScratchRetirementBootstrapTest(unittest.TestCase):
    """j#80523 R3-F5 — a REAL interrupted bootstrap, not an after-the-fact artifact delete.

    The prior "interrupted bootstrap" test deleted the seal after a SUCCESSFUL bootstrap. That
    is a different shape and pins nothing about the write path: it never drives a crash inside
    the bootstrap itself, so a bootstrap that left a half-built authority at the real path
    would still pass.
    """

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.home = Path(d)
        self.fence = ScratchRetirementFence(home=self.home)
        self.unit = RetirementUnit("ws1", "lane1", slot_digest(["mzb1_a", "mzb1_b"]))

    def _crash_at(self, stage):
        def hook(s, got):
            if got == stage:
                raise RuntimeError(f"simulated crash at {stage}")

        self.fence._bootstrap_hook = hook.__get__(self.fence)

    def test_crash_before_rename_leaves_a_temp_and_the_next_run_fails_closed(self):
        self._crash_at("built_temp")
        with self.assertRaises(RuntimeError):
            with self.fence.transaction(self.unit, live_pair_present=True):
                pass
        self.assertTrue(self.fence.temp_path.exists(), "the temp is the crash evidence")
        self.assertFalse(self.fence.path.exists(), "no half-built authority at the real path")
        shape = ScratchRetirementFence(home=self.home).store_shape()
        self.assertEqual(shape.state, "damaged")
        self.assertIn("temp", shape.present_artifacts)
        # A later run must NOT bootstrap over the wreckage.
        with self.assertRaises(ScratchRetirementFenceError):
            with ScratchRetirementFence(home=self.home).transaction(
                self.unit, live_pair_present=True
            ):
                pass

    def test_crash_after_rename_before_seal_fails_closed(self):
        self._crash_at("renamed")
        with self.assertRaises(RuntimeError):
            with self.fence.transaction(self.unit, live_pair_present=True):
                pass
        self.assertTrue(self.fence.path.exists())
        self.assertFalse(self.fence.seal_path.exists(), "the seal is written last")
        shape = ScratchRetirementFence(home=self.home).store_shape()
        self.assertEqual(shape.state, "damaged", "a db with no identity seal is damaged")
        with self.assertRaises(ScratchRetirementFenceError):
            with ScratchRetirementFence(home=self.home).transaction(
                self.unit, live_pair_present=True
            ):
                pass

    def test_orphan_temp_alone_is_not_an_absent_store(self):
        self.fence.temp_path.write_text("x")
        self.assertEqual(ScratchRetirementFence(home=self.home).store_shape().state, "damaged")

    def test_temp_residue_beside_a_healthy_store_is_damaged(self):
        """j#80594 R4-F4: R3-F5 added `temp` to the inventory but not to the VERDICT.

        A bootstrap builds in temp, renames, then seals — a healthy store never carries a
        temp beside it, so its presence is interrupted / foreign residue. It showed up in
        `present_artifacts` while `store_shape` still returned `present` and admitted the
        transaction: inventoried but not load-bearing.
        """
        with self.fence.transaction(self.unit, live_pair_present=True):
            pass
        self.assertEqual(self.fence.store_shape().state, "present")
        self.fence.temp_path.write_text("residue")
        shape = ScratchRetirementFence(home=self.home).store_shape()
        self.assertEqual(shape.state, "damaged")
        self.assertIn("temp", shape.present_artifacts)
        with self.assertRaises(ScratchRetirementFenceError):
            with ScratchRetirementFence(home=self.home).transaction(
                self.unit, live_pair_present=True
            ):
                pass
