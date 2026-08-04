"""`sublane resume` use case tests (Redmine #13682).

Drives :class:`SublaneResumeUseCase` over a fake IO port (fake live herdr inventory +
fake attestation reads — resume closes nothing and launches nothing) and a real
:class:`LaneLifecycleStore` over a temp home. Covers the fail-closed preflight (lane
hibernated, release settled, issue not re-owned, fresh pair both-slots-live +
generation-matched attested), the disposition CAS (hibernated -> active), epoch precedence
(minted exact epoch replaces timestamp/released-locator generation evidence), the in-flight
release / owner-conflict guards, and idempotent already-active.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_release_observation import (  # noqa: E402
    build_release_observation,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (
    IdentityAttestationRecord,
    VERDICT_MISSING,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    OWNER_RESOLVED,
    RELEASE_RELEASED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ReleasePin,
)
from mozyo_bridge.core.state.lane_pin_role import read_declared_pin_pair
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
    format_resume_text,
    BLOCK_ISSUE_REOWNED,
    BLOCK_NOT_HIBERNATED,
    BLOCK_PAIR_ATTESTATION,
    BLOCK_PAIR_PINS,
    BLOCK_PAIR_SLOTS,
    BLOCK_RELEASE_IN_FLIGHT,
    ResumeRequest,
    SublaneResumeUseCase,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

#: The epoch a lane that has hibernated once has minted (#14756). These fixtures drive
#: exactly one hibernate transition before resuming.
CURRENT_EPOCH = "1"

WS = "wProj"
ISSUE = "13441"
LANE = "issue_13441_provider"
OTHER = "issue_13441_recovery"
JOURNAL = "77485"
RESUME_JOURNAL = "77490"

# Deterministic timestamps for the freshness gate: a fresh relaunch self-attests AFTER
# the lane hibernated; a survivor / pre-hibernate record predates it.
DECLARE_AT = "2026-07-13T13:00:00+00:00"
HIBERNATE_AT = "2026-07-13T13:34:10+00:00"
FRESH_AT = "2026-07-13T13:40:00+00:00"
STALE_AT = "2026-07-13T13:20:00+00:00"


def _row(role: str, lane: str, locator: str) -> dict:
    return {"name": encode_assigned_name(WS, role, lane), "pane_id": locator}


def _attest(
    role: str,
    lane: str,
    locator: str,
    verdict: str = VERDICT_PRESENT,
    observed_at: str = FRESH_AT,
    lane_epoch: str = CURRENT_EPOCH,
):
    """A startup self-attestation for this fixture's pair.

    ``lane_epoch`` defaults to the epoch a lane hibernated ONCE has minted (Redmine #14756),
    because that is what a genuine relaunch of these fixtures' pairs would have received. It
    is a parameter so a test can attest a PRE-hibernate epoch — what a survivor carries.
    A minted exact epoch is the generation authority, so timestamp and released-locator
    evidence are deliberately irrelevant to resume. Tests for a pre-hibernate survivor pass
    an absent/old epoch explicitly; otherwise they would describe an impossible process that
    survived while somehow receiving the post-hibernate environment.
    """
    return IdentityAttestationRecord(
        assigned_name=encode_assigned_name(WS, role, lane),
        workspace_id=WS,
        role=role,
        lane_id=lane,
        locator=locator,
        verdict=verdict,
        observed_at=observed_at,
        lane_epoch=lane_epoch,
    )


class _FakeOps:
    """Fake resume IO port: canned workspace / rows / attestations (read-only)."""

    def __init__(self, *, rows, attestations):
        self._rows = list(rows)
        self._attest = dict(attestations)

    def workspace_id(self) -> str:
        return WS

    def live_rows(self):
        return list(self._rows)

    def read_attestation(self, assigned_name):
        return self._attest.get(assigned_name)

    def provider_pair(self):
        return ("codex", "claude")


def _decision(journal: str = JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=journal)


def _request(**kw) -> ResumeRequest:
    return ResumeRequest(
        issue=kw.get("issue", ISSUE),
        lane=kw.get("lane", LANE),
        journal=kw.get("journal", RESUME_JOURNAL),
    )


def _fresh_pair_ops(*, gw="p20", wk="p21", lane=LANE):
    """A freshly relaunched pair: both slots live at NEW locators with fresh attestation."""
    rows = [
        _row("codex", lane, f"{WS}:{gw}"),
        _row("claude", lane, f"{WS}:{wk}"),
    ]
    attest = {
        encode_assigned_name(WS, "codex", lane): _attest("codex", lane, f"{WS}:{gw}"),
        encode_assigned_name(WS, "claude", lane): _attest("claude", lane, f"{WS}:{wk}"),
    }
    return _FakeOps(rows=rows, attestations=attest)


def _declared_pair() -> tuple[ProcessGenerationPin, ...]:
    return (
        ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(WS, "codex", LANE),
            locator=f"{WS}:p10",
            attested_at=DECLARE_AT,
        ),
        ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=encode_assigned_name(WS, "claude", LANE),
            locator=f"{WS}:p11",
            attested_at=DECLARE_AT,
        ),
    )


class SublaneResumeTest(unittest.TestCase):
    def _store(self, tmp) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=Path(tmp))

    def _hibernated(self, store, *, released=False, declared_slots=()) -> None:
        """Declare active, then hibernate; optionally drive the release to `released`.

        All hibernate-side writes are stamped ``HIBERNATE_AT``, so the row's immutable
        ``hibernated_at`` boundary (v8, Redmine #14477) — and its pre-v8 ``updated_at``
        fallback — are both that deterministic value (a fresh relaunch attests at ``FRESH_AT``).
        """
        key = LaneLifecycleKey(WS, LANE)
        if declared_slots:
            LaneDeclarationStore(path=store.path).declare_lane(
                key,
                decision=_decision(JOURNAL),
                issue_id=ISSUE,
                declared_slots=declared_slots,
                now=DECLARE_AT,
            )
        else:
            store.declare_active(
                key,
                decision=_decision(JOURNAL),
                issue_id=ISSUE,
                now=DECLARE_AT,
            )
        store.transition_disposition(
            key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=1,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(JOURNAL),
            now=HIBERNATE_AT,
        )
        if released:
            rec = store.get(key)
            store.request_release(
                key,
                expected_revision=rec.revision,
                action_id=f"hibernate:{LANE}",
                observation=build_release_observation([
                    # Redmine #14477 (disposition j#94544 A): the released-locator fence needs
                    # the release generation to cover EVERY slot it closed. One pin for a
                    # two-slot pair is incomplete evidence and now fails closed, so the seed
                    # records both — which is also what a real hibernate release records.
                    ReleasePin(
                        role="codex",
                        assigned_name=encode_assigned_name(WS, "codex", LANE),
                        locator=f"{WS}:pOLD_GW",
                    ),
                    ReleasePin(
                        role="claude",
                        assigned_name=encode_assigned_name(WS, "claude", LANE),
                        locator=f"{WS}:pOLD_WK",
                    ),
                ]),
                now=HIBERNATE_AT,
            )
            rec = store.get(key)
            store.record_release_outcome(
                key,
                action_id=f"hibernate:{LANE}",
                expected_revision=rec.revision,
                target=RELEASE_RELEASED,
                now=HIBERNATE_AT,
            )

    def test_happy_path_resumes_to_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(
                store, released=True, declared_slots=_declared_pair()
            )
            ops = _fresh_pair_ops()
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.transition.applied)
            rec = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(rec.lane_disposition, DISPOSITION_ACTIVE)
            self.assertEqual(rec.issue_id, ISSUE)
            # Rehydrate cleared the finished release generation.
            self.assertEqual(rec.process_release, "not_requested")
            self.assertEqual(rec.release_action_id, "")
            owner = store.resolve_owner(WS, ISSUE)
            self.assertEqual(owner.status, OWNER_RESOLVED)
            self.assertEqual(owner.lane_id, LANE)
            # The resume decision anchor replaced the hibernate one (never inherited).
            self.assertEqual(rec.decision_journal, RESUME_JOURNAL)
            pair = read_declared_pin_pair(rec)
            self.assertTrue(pair.ok)
            self.assertEqual(pair.gateway.locator, f"{WS}:p20")
            self.assertEqual(pair.worker.locator, f"{WS}:p21")

    def test_a_moved_commit_authority_leaves_the_active_flip_zero(self) -> None:
        """Redmine #14475 (review j#88538 F1): the seam sits AT the CAS, not before ``run()``.

        Everything the resume does before the commit is observation — the live pair read, the
        attestation join, the pin resolution — so a caller checking its authority before
        calling ``run()`` has not checked it before the active flip. This drives the exact
        happy path and moves only the commit authority.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E501
            BLOCK_COMMIT_AUTHORITY_MOVED,
            DISPOSITION_HIBERNATED as _HIB,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            # released=True: the #14477 released-locator fence needs completed release
            # evidence, and this test is about the commit-edge seam, not the fence.
            self._hibernated(store, released=True)
            before = store.get(LaneLifecycleKey(WS, LANE))
            outcome = SublaneResumeUseCase(
                ops=_fresh_pair_ops(), store=store, commit_authority=lambda: False,
            ).run(_request(), execute=True)
            self.assertEqual(outcome.detail, BLOCK_COMMIT_AUTHORITY_MOVED)
            # Review j#88547 F1: the outcome must READ as blocked, not merely apply nothing.
            # A caller gating on ``is_blocked`` would otherwise proceed to its own next effect.
            self.assertTrue(outcome.is_blocked, "a commit-edge authority loss is a block")
            self.assertIn(
                BLOCK_COMMIT_AUTHORITY_MOVED, format_resume_text(outcome),
                "and the operator-facing line must name the reason, not print an empty one",
            )
            after = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(after.lane_disposition, _HIB, "zero active flip")
            self.assertEqual(after.revision, before.revision, "and the row untouched")

    def test_a_current_commit_authority_still_resumes(self) -> None:
        # The positive control: the seam must not block a healthy resume.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            # released=True so the #14477 released-locator fence has complete evidence; a
            # healthy resume must satisfy every fence, this one included.
            self._hibernated(store, released=True)
            outcome = SublaneResumeUseCase(
                ops=_fresh_pair_ops(), store=store, commit_authority=lambda: True,
            ).run(_request(), execute=True)
            self.assertFalse(outcome.is_blocked)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition, DISPOSITION_ACTIVE
            )

    def test_minted_epoch_replaces_missing_release_evidence(self) -> None:
        """#14955: exact epoch answers the question missing release evidence cannot."""
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=False)
            outcome = SublaneResumeUseCase(ops=_fresh_pair_ops(), store=store).run(
                _request(), execute=True
            )
            self.assertFalse(outcome.is_blocked)
            self.assertTrue(outcome.transition.applied)
            self.assertNotIn(
                "release_evidence_absent", outcome.preflight.pair_attestation_detail
            )
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_ACTIVE,
            )

    def test_blocks_when_pair_not_both_slots_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            # Only the gateway relaunched.
            rows = [_row("codex", LANE, f"{WS}:p20")]
            attest = {
                encode_assigned_name(WS, "codex", LANE): _attest("codex", LANE, f"{WS}:p20")
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PAIR_SLOTS, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_blocks_when_pair_not_attested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            # Both slots live, but the gateway self-attestation is missing.
            rows = [
                _row("codex", LANE, f"{WS}:p20"),
                _row("claude", LANE, f"{WS}:p21"),
            ]
            attest = {
                encode_assigned_name(WS, "codex", LANE): _attest(
                    "codex", LANE, f"{WS}:p20", verdict=VERDICT_MISSING
                ),
                encode_assigned_name(WS, "claude", LANE): _attest(
                    "claude", LANE, f"{WS}:p21"
                ),
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)

    def test_blocks_before_transition_when_fresh_pair_pins_do_not_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            ops = _fresh_pair_ops()
            ops.provider_pair = lambda: ("unknown-gateway", "unknown-worker")
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(
                BLOCK_PAIR_PINS,
                outcome.preflight.blocked_reasons,
            )
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_blocks_provider_binding_drift_without_mutating_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(
                store, released=True, declared_slots=_declared_pair()
            )
            before = store.get(LaneLifecycleKey(WS, LANE))
            ops = _fresh_pair_ops()
            ops.provider_pair = lambda: ("claude", "codex")
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PAIR_PINS, outcome.preflight.blocked_reasons)
            self.assertIn(
                "provider_binding_drift",
                outcome.preflight.pair_attestation_detail,
            )
            self.assertIsNone(outcome.transition)
            after = store.get(LaneLifecycleKey(WS, LANE))
            self.assertEqual(after.lane_disposition, before.lane_disposition)
            self.assertEqual(after.revision, before.revision)
            self.assertEqual(after.declared_slots, before.declared_slots)

    def test_stale_pre_hibernate_pane_is_not_fresh(self) -> None:
        # Freshness guard: a pane lingering from before hibernate is live at its OLD
        # locator, but its attestation record points at that old locator while the
        # inventory row we simulate as the "resumed" slot is at a NEW locator — so the
        # attestation join is stale and the pair does not attest. (Here the gateway's
        # live row is at p20 but its attestation was written for pOLD.)
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            rows = [
                _row("codex", LANE, f"{WS}:p20"),
                _row("claude", LANE, f"{WS}:p21"),
            ]
            attest = {
                # Gateway attestation is for a DIFFERENT (stale) locator than the live row.
                encode_assigned_name(WS, "codex", LANE): _attest(
                    "codex", LANE, f"{WS}:pOLD"
                ),
                encode_assigned_name(WS, "claude", LANE): _attest(
                    "claude", LANE, f"{WS}:p21"
                ),
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_survived_pane_with_matching_locator_has_no_minted_epoch(self) -> None:
        # The adversarial defect (Finding 4): a pane that SURVIVED hibernate's release keeps
        # its tmux pane-id locator and still matches its own PRE-hibernate attestation — the
        # locator alone cannot tell a survivor from a relaunch. Here both slots are live at
        # their original locators AND their attestations are locator-matched (would pass the
        # #13637 join), but they carry no epoch minted by the hibernate transition. That
        # immutable process-env fact rejects the survivor without using its old timestamp.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            rows = [
                _row("codex", LANE, f"{WS}:p12"),
                _row("claude", LANE, f"{WS}:p13"),
            ]
            attest = {
                # Locators MATCH the live rows (the panes survived), but the records were
                # written BEFORE hibernation (STALE_AT < HIBERNATE_AT).
                encode_assigned_name(WS, "codex", LANE): _attest(
                    "codex", LANE, f"{WS}:p12", observed_at=STALE_AT, lane_epoch=""
                ),
                encode_assigned_name(WS, "claude", LANE): _attest(
                    "claude", LANE, f"{WS}:p13", observed_at=STALE_AT, lane_epoch=""
                ),
            }
            ops = _FakeOps(rows=rows, attestations=attest)
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_PAIR_ATTESTATION, outcome.preflight.blocked_reasons)
            self.assertIn(
                "lane_epoch_attestation_absent",
                outcome.preflight.pair_attestation_detail,
            )
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_blocks_when_release_generation_in_flight(self) -> None:
        # A hibernate whose release is still `requested`/`partial` (panes may still be
        # closing) must never resume — even if a live pair appears attested. The lane's
        # release generation is not settled -> fail closed (the substrate CAS is the
        # backstop; here the preflight names the reason).
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(
                key, decision=_decision(JOURNAL), issue_id=ISSUE, now=DECLARE_AT
            )
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target=DISPOSITION_HIBERNATED,
                decision=_decision(JOURNAL),
                now=HIBERNATE_AT,
            )
            rec = store.get(key)
            store.request_release(  # -> RELEASE_REQUESTED (in flight, never recorded)
                key,
                expected_revision=rec.revision,
                action_id=f"hibernate:{LANE}",
                observation=build_release_observation([
                    ReleasePin(
                        role="codex",
                        assigned_name=encode_assigned_name(WS, "codex", LANE),
                        locator=f"{WS}:p20",
                    )
                ]),
                now=HIBERNATE_AT,
            )
            # A genuinely fresh pair is present — proving the ONLY blocker is the
            # in-flight release, not the pair.
            ops = _fresh_pair_ops()
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            # The exact epoch proves the pair generation, but release actuation is still in
            # flight. That independent state-machine fence remains blocking.
            self.assertEqual(
                outcome.preflight.blocked_reasons,
                (BLOCK_RELEASE_IN_FLIGHT,),
            )
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(key).lane_disposition, DISPOSITION_HIBERNATED
            )

    def test_blocks_when_issue_reowned_by_another_lane(self) -> None:
        # While the lane slept, another lane took the issue (a fresh declare_active).
        # Resuming would create a second active owner — fail closed.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            # A different lane becomes the active owner of the same issue.
            store.declare_active(
                LaneLifecycleKey(WS, OTHER), decision=_decision(JOURNAL), issue_id=ISSUE
            )
            ops = _fresh_pair_ops()
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_ISSUE_REOWNED, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )
            # The other lane still owns it (never displaced).
            self.assertEqual(store.resolve_owner(WS, ISSUE).lane_id, OTHER)

    def test_blocks_when_lane_not_hibernated(self) -> None:
        # An active (never hibernated) lane that is not the resolved owner path: a lane in
        # some other non-hibernated state is not resumable.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            key = LaneLifecycleKey(WS, LANE)
            store.declare_active(key, decision=_decision(JOURNAL), issue_id=ISSUE)
            # Supersede it so it is `superseded`, not hibernated.
            store.transition_disposition(
                key,
                expected_disposition=DISPOSITION_ACTIVE,
                expected_revision=1,
                target="superseded",
                decision=_decision(JOURNAL),
            )
            ops = _fresh_pair_ops()
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIn(BLOCK_NOT_HIBERNATED, outcome.preflight.blocked_reasons)
            self.assertIsNone(outcome.transition)

    def test_preflight_only_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            ops = _fresh_pair_ops()
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=False
            )
            self.assertTrue(outcome.preflight.may_resume)
            self.assertFalse(outcome.executed)
            self.assertFalse(outcome.is_blocked)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )

    def test_already_active_is_idempotent_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            ops = _fresh_pair_ops()
            first = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(), execute=True
            )
            self.assertFalse(first.is_blocked)
            rev_after = store.get(LaneLifecycleKey(WS, LANE)).revision
            # Re-run (restart / re-login): already active -> no-op, no further mutation.
            second = SublaneResumeUseCase(ops=_fresh_pair_ops(), store=store).run(
                _request(journal="77491"), execute=True
            )
            self.assertTrue(second.already_active)
            self.assertFalse(second.is_blocked)
            self.assertIsNone(second.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).revision, rev_after
            )

    def test_incomplete_identity_fails_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._hibernated(store, released=True)
            ops = _fresh_pair_ops()
            outcome = SublaneResumeUseCase(ops=ops, store=store).run(
                _request(journal="not-a-number"), execute=True
            )
            self.assertTrue(outcome.is_blocked)
            self.assertIsNone(outcome.transition)
            self.assertEqual(
                store.get(LaneLifecycleKey(WS, LANE)).lane_disposition,
                DISPOSITION_HIBERNATED,
            )


if __name__ == "__main__":
    unittest.main()
