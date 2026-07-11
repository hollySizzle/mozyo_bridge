"""Pure preflight assembly + drift-bound plan + human gate receipt tests."""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.intent import (
    OnboardingIntent,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.path_safety import (
    ADOPTION_ABSENT,
    ADOPTION_CONFIG,
    PATH_RISK_AMBIGUOUS,
    PATH_RISK_HOME,
    PATH_RISK_NORMAL,
    PATH_RISK_SYNC_OR_CLOUD,
    ROOT_KIND_GIT,
    ROOT_KIND_NON_GIT,
    PathSafety,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.plan import (
    OnboardingFacts,
    PlanError,
    build_plan,
    compute_root_fingerprint,
    issue_human_gate_receipt,
    verify_human_gate_receipt,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.preflight import (
    HERDR_RESOLVED,
    HERDR_SOURCE_ENV,
    RECEIPT_STATE_BROKEN,
    RECEIPT_STATE_NONE,
    STATE_ADOPTED,
    STATE_ADOPTION_IN_PROGRESS,
    STATE_BLOCKED,
    STATE_CAUTION_REQUIRES_ACK,
    STATE_UNADOPTED,
    HerdrBinary,
    assemble_preflight,
)
from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.domain.receipt import (
    RECEIPT_STATE_COMPLETE,
    RECEIPT_STATE_IN_PROGRESS,
)

_HERDR = HerdrBinary(state=HERDR_RESOLVED, source=HERDR_SOURCE_ENV, path="/usr/local/bin/herdr")
_SECRET = "test-gate-secret"


def _safety(**overrides) -> PathSafety:
    base = dict(
        root="/private/tmp/proj",
        root_kind=ROOT_KIND_NON_GIT,
        path_risk=PATH_RISK_NORMAL,
        adoption_marker=ADOPTION_ABSENT,
        notes=(),
    )
    base.update(overrides)
    from pathlib import Path

    return PathSafety(
        root=Path(base["root"]),
        root_kind=base["root_kind"],
        path_risk=base["path_risk"],
        adoption_marker=base["adoption_marker"],
        notes=base["notes"],
    )


class PreflightStateMatrixTests(unittest.TestCase):
    def test_home_is_blocked(self) -> None:
        pf = assemble_preflight(
            _safety(path_risk=PATH_RISK_HOME, notes=("home",)), _HERDR
        )
        self.assertEqual(pf.state, STATE_BLOCKED)
        self.assertTrue(pf.hard_block_reasons)

    def test_ambiguous_is_blocked(self) -> None:
        pf = assemble_preflight(
            _safety(path_risk=PATH_RISK_AMBIGUOUS, notes=("ambiguous",)), _HERDR
        )
        self.assertEqual(pf.state, STATE_BLOCKED)

    def test_unreadable_config_is_blocked(self) -> None:
        pf = assemble_preflight(_safety(), _HERDR, config_readable=False)
        self.assertEqual(pf.state, STATE_BLOCKED)

    def test_broken_receipt_is_blocked(self) -> None:
        pf = assemble_preflight(_safety(), _HERDR, receipt_state=RECEIPT_STATE_BROKEN)
        self.assertEqual(pf.state, STATE_BLOCKED)

    def test_in_progress_receipt_is_adoption_in_progress(self) -> None:
        pf = assemble_preflight(
            _safety(adoption_marker=ADOPTION_CONFIG),
            _HERDR,
            receipt_state=RECEIPT_STATE_IN_PROGRESS,
        )
        self.assertEqual(pf.state, STATE_ADOPTION_IN_PROGRESS)

    def test_complete_receipt_is_adopted(self) -> None:
        pf = assemble_preflight(
            _safety(adoption_marker=ADOPTION_CONFIG),
            _HERDR,
            receipt_state=RECEIPT_STATE_COMPLETE,
        )
        self.assertEqual(pf.state, STATE_ADOPTED)

    def test_preexisting_marker_without_receipt_is_adopted(self) -> None:
        pf = assemble_preflight(_safety(adoption_marker=ADOPTION_CONFIG), _HERDR)
        self.assertEqual(pf.state, STATE_ADOPTED)

    def test_sync_unadopted_is_caution(self) -> None:
        pf = assemble_preflight(
            _safety(path_risk=PATH_RISK_SYNC_OR_CLOUD, notes=("sync",)), _HERDR
        )
        self.assertEqual(pf.state, STATE_CAUTION_REQUIRES_ACK)
        self.assertTrue(pf.caution)

    def test_normal_unadopted_is_unadopted(self) -> None:
        pf = assemble_preflight(_safety(), _HERDR)
        self.assertEqual(pf.state, STATE_UNADOPTED)


def _facts(**overrides) -> OnboardingFacts:
    base = dict(
        canonical_root="/private/tmp/proj",
        state=STATE_UNADOPTED,
        root_kind=ROOT_KIND_NON_GIT,
        path_risk=PATH_RISK_NORMAL,
        adoption_marker=ADOPTION_ABSENT,
        herdr_binary_realpath="/usr/local/bin/herdr",
        existing_file_hashes={},
    )
    base.update(overrides)
    return OnboardingFacts(**base)


def _intent(**overrides) -> OnboardingIntent:
    base = dict(
        action="propose",
        preset="none",
        backend="herdr",
        git_mode="none",
        rules_store="central",
        free_text_summary="",
        schema_version=1,
    )
    base.update(overrides)
    return OnboardingIntent(**base)


class HumanGateReceiptTests(unittest.TestCase):
    def test_issue_and_verify_roundtrip(self) -> None:
        token = issue_human_gate_receipt("fp", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        self.assertTrue(
            verify_human_gate_receipt(token, "fp", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        )

    def test_spoofed_token_rejected(self) -> None:
        self.assertFalse(
            verify_human_gate_receipt("hgr.v1.deadbeef", "fp", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        )

    def test_receipt_bound_to_other_root_rejected(self) -> None:
        token = issue_human_gate_receipt("fp-A", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        self.assertFalse(
            verify_human_gate_receipt(token, "fp-B", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        )

    def test_receipt_from_other_secret_rejected(self) -> None:
        token = issue_human_gate_receipt("fp", PATH_RISK_SYNC_OR_CLOUD, secret="other")
        self.assertFalse(
            verify_human_gate_receipt(token, "fp", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        )

    def test_none_token_rejected(self) -> None:
        self.assertFalse(
            verify_human_gate_receipt(None, "fp", PATH_RISK_SYNC_OR_CLOUD, secret=_SECRET)
        )


class BuildPlanTests(unittest.TestCase):
    def test_normal_unadopted_plan_builds(self) -> None:
        plan = build_plan(_facts(), _intent(), gate_secret=_SECRET)
        self.assertEqual(plan.scaffold_preset, "none")
        self.assertEqual(len(plan.ordered_steps), 7)
        self.assertTrue(plan.requires_confirmation)

    def test_preset_underscore_maps_to_hyphen(self) -> None:
        plan = build_plan(_facts(), _intent(preset="redmine_governed"), gate_secret=_SECRET)
        self.assertEqual(plan.scaffold_preset, "redmine-governed")

    def test_undecided_preset_refused(self) -> None:
        with self.assertRaises(PlanError) as ctx:
            build_plan(_facts(), _intent(preset="undecided"), gate_secret=_SECRET)
        self.assertEqual(ctx.exception.code, "preset_undecided")

    def test_blocked_state_refused(self) -> None:
        with self.assertRaises(PlanError) as ctx:
            build_plan(_facts(state=STATE_BLOCKED), _intent(), gate_secret=_SECRET)
        self.assertEqual(ctx.exception.code, "blocked")

    def test_adopted_state_refused(self) -> None:
        with self.assertRaises(PlanError) as ctx:
            build_plan(_facts(state=STATE_ADOPTED), _intent(), gate_secret=_SECRET)
        self.assertEqual(ctx.exception.code, "not_plannable")

    def test_sync_caution_requires_valid_receipt(self) -> None:
        facts = _facts(state=STATE_CAUTION_REQUIRES_ACK, path_risk=PATH_RISK_SYNC_OR_CLOUD)
        with self.assertRaises(PlanError) as ctx:
            build_plan(facts, _intent(), gate_secret=_SECRET)
        self.assertEqual(ctx.exception.code, "human_gate_required")

        fp = compute_root_fingerprint(facts)
        token = issue_human_gate_receipt(fp, facts.path_risk, secret=_SECRET)
        plan = build_plan(facts, _intent(), human_gate_receipt=token, gate_secret=_SECRET)
        self.assertTrue(any("sync/cloud" in w for w in plan.warnings))

    def test_git_init_forbidden_on_sync(self) -> None:
        facts = _facts(state=STATE_CAUTION_REQUIRES_ACK, path_risk=PATH_RISK_SYNC_OR_CLOUD)
        fp = compute_root_fingerprint(facts)
        token = issue_human_gate_receipt(fp, facts.path_risk, secret=_SECRET)
        with self.assertRaises(PlanError) as ctx:
            build_plan(
                facts, _intent(git_mode="initialize"), human_gate_receipt=token, gate_secret=_SECRET
            )
        self.assertEqual(ctx.exception.code, "git_init_forbidden_on_sync")

    def test_git_init_on_normal_requires_receipt(self) -> None:
        with self.assertRaises(PlanError) as ctx:
            build_plan(_facts(), _intent(git_mode="initialize"), gate_secret=_SECRET)
        self.assertEqual(ctx.exception.code, "git_init_requires_confirmation")

    def test_plan_id_and_fingerprint_are_deterministic(self) -> None:
        p1 = build_plan(_facts(), _intent(), gate_secret=_SECRET)
        p2 = build_plan(_facts(), _intent(), gate_secret=_SECRET)
        self.assertEqual(p1.plan_id, p2.plan_id)
        self.assertEqual(p1.root_fingerprint, p2.root_fingerprint)

    def test_fingerprint_changes_when_existing_file_hash_changes(self) -> None:
        fp1 = compute_root_fingerprint(_facts(existing_file_hashes={"a": "1"}))
        fp2 = compute_root_fingerprint(_facts(existing_file_hashes={"a": "2"}))
        self.assertNotEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
