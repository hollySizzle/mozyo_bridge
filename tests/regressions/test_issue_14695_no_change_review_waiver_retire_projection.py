"""Redmine #14695 — a direct-owner no-change Review waiver must reach glance, close and retire.

#14613 was a characterization: zero repository change, zero commits, and an owner who said in as
many words that no separate reviewer was owed. The coordinator recorded that waiver durably and
closed the issue — and the standard ``sublane retire`` still blocked with
``stale_review_generation``, because the only durable authority that fence reads is a REVIEW
GENERATION and a lane that changed nothing has none (reproduction: #14613 j#93256 / j#93262).

The two escapes correctly refused there are what this suite exists to keep refused: asserting
``--latest-generation-admissible`` about a review that never happened is a FALSE assert, and a
fabricated Review Gate is the "exemption を Review Gate approval または自己 review と表現しない"
the central preset forbids.

So this pins BOTH directions, which is the whole point of a safety route:

* the positive — a valid waiver on a genuinely no-change lane projects the same conclusion
  through the glance and the terminal retire, with no false assert anywhere;
* the negatives — and there are far more of them. A record that declares ANY repository change,
  a hard carve-out surface, a newer review round, a foreign issue / lane / generation, a moved
  head, a dirty worktree, an owed callback, a marker the canonical producer could not render, or
  a heading with no marker at all must each keep the ordinary fence fully armed.

The design ruling is #14695 j#93412 (superseding j#93406 on the same consultation j#93404).
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.glance_journal_grammar import (  # noqa: E501
    fold_issue_gate_facts,
    lane_signal_from_gate_facts,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_evidence_authority import (  # noqa: E501
    GATE_NO_CHANGE_REVIEW_WAIVER,
    ISSUER_COORDINATOR,
    contract_ruling_pointer,
    contract_writer_role,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_carve_out import (  # noqa: E501
    CARVE_OUT_DECLARED,
    CARVE_OUT_UNRESOLVED,
    HARD_CARVE_OUT_GATE_TOKENS,
    fold_hard_carve_out,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.no_change_review_waiver import (  # noqa: E501
    NO_CHANGE_REVIEW_WAIVER_GATE,
    REASON_CALLBACK_OWED,
    REASON_CHANGE_DECLARED,
    REASON_CLOSE_NOT_RECORDED,
    REASON_HARD_CARVE_OUT,
    REASON_HARD_CARVE_OUT_UNRESOLVED,
    REASON_LANE_COMMITS_PRESENT,
    REASON_LANE_HEAD_UNMEASURED,
    REASON_NO_WAIVER_RECORDED,
    REASON_POST_WAIVER_MUTATION,
    REASON_WAIVER_INVALID,
    REASON_WAIVER_ISSUE_MISMATCH,
    REASON_WAIVER_LANE_MISMATCH,
    REASON_WAIVER_SUPERSEDED,
    REASON_WRITER_AUTHORITY_UNRESOLVED,
    REASON_WORKTREE_NOT_CLEAN,
    WAIVER_INVALID,
    WRITER_AUTHORITY_RESOLVABLE,
    WAIVER_NONE,
    WAIVER_WAIVED,
    ZERO_CHANGE_COMMIT_DECLARED,
    ZERO_CHANGE_INTEGRATION_DECLARED,
    ZERO_CHANGE_SCOPE_DECLARED,
    evaluate_no_change_waiver_admissible,
    fold_no_change_review_waiver,
    fold_zero_change_record,
    render_no_change_review_waiver_marker,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
    LANE_STATE_BLOCKED,
    LANE_STATE_OWNER_WAITING,
    LANE_STATE_RETIRE_READY,
    LANE_STATE_REVIEW_WAITING,
    classify_lane_state,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
ISSUE = "14613"
WORKSPACE = "ws1"
LANE = "issue_14613_herdr_075"
GENERATION = 3


def waiver_marker(
    *, issue=ISSUE, workspace=WORKSPACE, lane=LANE, generation=GENERATION, head=HEAD
) -> str:
    return render_no_change_review_waiver_marker(
        issue=issue, workspace=workspace, lane=lane, lane_generation=generation, head=head
    )


#: The CANONICAL carve-out determination: the ``## Gate: owner_close_approval`` template's own
#: ``carve_out_check`` field. It is what RESOLVES the hard carve-out half, so every fixture that
#: expects to admit must carry it — and one that omits it is a negative, not a broken fixture.
#: R2 used the issue's ``work_unit`` as a stand-in and review j#93638 finding 1 measured the cost:
#: a record stating ``carve_out_check: production_verification`` still admitted, because the
#: reader never looked at the field the record used to say so.
CARVE_OUT_CLEARED = (
    "## Gate: owner_close_approval\n- approval_source: direct_owner\n- carve_out_check: none"
)


def no_change_journals(marker: str | None = None) -> list:
    """A genuinely no-change record: a start, a progress note, and a Close carrying the waiver."""
    return [
        ("100", "## Gate: start\n- issue: #14613\n- 目的: characterization"),
        ("200", f"## Progress Log\n- read-only 実測のみ\n\n{CARVE_OUT_CLEARED}"),
        ("300", f"## Gate: close\n- 受け入れ確認: 済\n{marker if marker else waiver_marker()}"),
    ]


def admit(journals, **overrides):
    """Evaluate the retire admission over ``journals`` with all live facts satisfied."""
    facts = fold_issue_gate_facts(journals)
    kwargs = dict(
        # The supersession half only — the retire's own wiring. Passing the folded
        # ``review_waived`` here would make every change-bearing record refuse as "superseded",
        # which is what this suite caught during development.
        currently_in_force=facts.review_waiver_unsuperseded if facts else False,
        zero_change=fold_zero_change_record(journals),
        carve_out=fold_hard_carve_out(journals),
        close_recorded=(facts.latest_gate == "close") if facts else False,
        target_issue=ISSUE,
        expected_workspace=WORKSPACE,
        expected_lane=LANE,
        expected_lane_generation=GENERATION,
        live_head=HEAD,
        live_commits_ahead=0,
        worktree_clean=True,
        callbacks_drained=True,
    )
    kwargs.update(overrides)
    return evaluate_no_change_waiver_admissible(fold_no_change_review_waiver(journals), **kwargs)


class WriterContractRulingTest(unittest.TestCase):
    """The gate's writer role and the record that DECIDED it (#14661 j#92715's requirement)."""

    def test_canonical_writer_is_the_coordinator(self):
        self.assertEqual(
            contract_writer_role(GATE_NO_CHANGE_REVIEW_WAIVER), ISSUER_COORDINATOR
        )

    def test_ruling_pointer_names_this_gates_own_answer(self):
        # Not #14219 j#85530 Q3 and not #14661 j#92641: neither mentions this gate, and an anchor
        # whose target is silent about the gate passes ``is_anchored`` while being untraceable.
        self.assertEqual(
            contract_ruling_pointer(GATE_NO_CHANGE_REVIEW_WAIVER), "redmine:#14695:j#93412"
        )

    def test_the_authority_module_and_the_waiver_module_name_one_token(self):
        # The token is re-declared as a literal in the authority module to avoid an import cycle;
        # a test is what keeps the two spellings from drifting into two gates.
        self.assertEqual(GATE_NO_CHANGE_REVIEW_WAIVER, NO_CHANGE_REVIEW_WAIVER_GATE)


class WriterAuthorityTypedRefusalTest(unittest.TestCase):
    """Review j#93776 finding 1: the route admits NOTHING while its writer cannot be established.

    The issue's Acceptance sanctions this outcome in as many words — express the waiver as a
    durable authority "or typed-refuse before Close". This record system cannot establish who
    wrote a journal: every role posts under one account (ruling #14219 j#86718) and the issuer
    resolution is a policy binding that takes no author input. A lane worker therefore knows every
    value the envelope asks for, so the envelope is something it fills in, not something it must
    forge — and R4's two-self-declaration conjunction was still one self-declaration by one
    unauthenticated actor.
    """

    def test_a_wellformed_record_with_no_author_or_receipt_is_refused(self):
        # The exact reproduction from the review: nothing in this record identifies its writer.
        result = admit(no_change_journals())
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, REASON_WRITER_AUTHORITY_UNRESOLVED)

    def test_the_glance_does_not_call_a_waiver_bearing_closed_lane_retire_ready(self):
        """Review j#93807 finding 1, and the defect in this suite's own earlier test.

        The previous version built the signal and asserted NOTHING about the lane state, so it
        claimed the two consumers agreed while the glance said ``retire_ready`` and the retire
        refused. A test named for an agreement must measure the agreement.
        """
        journals = no_change_journals()
        facts = fold_issue_gate_facts(journals)
        self.assertFalse(facts.review_waived)
        self.assertTrue(facts.review_waiver_unsupported)
        state = classify_lane_state(lane_signal_from_gate_facts(ISSUE, facts, issue_open=False))
        self.assertNotEqual(state, LANE_STATE_RETIRE_READY)
        self.assertFalse(admit(journals).admissible)
        self.assertNotEqual(facts.review_conclusion, "approved")

    def test_a_lane_with_no_waiver_is_left_exactly_as_it_was(self):
        """The scope control, measured before the fix and preserved by it.

        A closed no-commit lane projects ``retire_ready`` whether or not a waiver is involved —
        that is pre-existing general glance behaviour, not something this route introduced. The
        suppression is therefore scoped to records that CARRY a waiver; widening it would change
        unrelated lanes (and #14539's route) on this issue's authority.
        """
        journals = [
            ("100", "## Gate: start"),
            ("200", CARVE_OUT_CLEARED),
            ("300", "## Gate: close\n- 受け入れ確認: 済"),
        ]
        facts = fold_issue_gate_facts(journals)
        self.assertFalse(facts.review_waiver_unsupported)
        state = classify_lane_state(lane_signal_from_gate_facts(ISSUE, facts, issue_open=False))
        self.assertEqual(state, LANE_STATE_RETIRE_READY)

    def test_a_waiver_before_close_does_not_reach_owner_waiting(self):
        journals = [
            ("100", "## Gate: start"),
            ("300", f"## Gate: implementation done\n{waiver_marker()}"),
        ]
        facts = fold_issue_gate_facts(journals)
        signal = lane_signal_from_gate_facts(ISSUE, facts, issue_open=True)
        self.assertEqual(classify_lane_state(signal), LANE_STATE_REVIEW_WAITING)

    def test_the_same_record_without_a_waiver_is_identical(self):
        # The control that makes the case above meaningful: with the route inert, a waiver changes
        # nothing at all, which is exactly what "admits nothing" must mean.
        journals = [("100", "## Gate: start"), ("300", "## Gate: implementation done")]
        facts = fold_issue_gate_facts(journals)
        signal = lane_signal_from_gate_facts(ISSUE, facts, issue_open=True)
        self.assertEqual(classify_lane_state(signal), LANE_STATE_REVIEW_WAITING)

    def test_the_gate_is_a_single_flag_the_folds_do_not_depend_on(self):
        """Everything below the authority gate stays live, so a future ruling flips one thing.

        The folds are still exercised by the rest of this suite; this pins that the refusal is the
        LAST conjunct, so a malformed / change-bearing record is still diagnosed by its own true
        cause rather than being swallowed by the authority refusal.
        """
        self.assertFalse(WRITER_AUTHORITY_RESOLVABLE)
        change_bearing = [
            ("100", "## Gate: start"),
            ("250", "## Gate: implementation done\n- commit_hash: `deadbeef1234567`"),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(admit(change_bearing).reason, REASON_CHANGE_DECLARED)


class SourceChangeCarveOutTest(unittest.TestCase):
    """Acceptance: a record that declares repository change must FAIL CLOSED, every way."""

    def test_a_declared_commit_refuses(self):
        # Inserted BEFORE the Close so the latest gate stays ``close`` and the earlier conjuncts
        # pass: this test must fail on the declared change, not on a rearranged lifecycle.
        journals = [
            ("100", "## Gate: start"),
            ("250", "## Gate: implementation done\n- commit_hash: `deadbeef1234567`"),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(
            fold_zero_change_record(journals).reason, ZERO_CHANGE_COMMIT_DECLARED
        )
        self.assertEqual(admit(journals).reason, REASON_CHANGE_DECLARED)

    def test_declared_changed_paths_refuse(self):
        # A plain note, deliberately NOT a ``review_request`` gate: that would also open a newer
        # review round, and the record would then refuse for supersession rather than for the
        # declared change. Each negative must isolate the conjunct it claims to test.
        journals = [
            ("100", "## Gate: start"),
            ("250", "## Progress Log\n- changed_paths:\n  - `src/a.py`"),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(
            fold_zero_change_record(journals).reason, ZERO_CHANGE_SCOPE_DECLARED
        )
        self.assertEqual(admit(journals).reason, REASON_CHANGE_DECLARED)

    def test_a_recorded_integration_disposition_refuses(self):
        # Not "did integration succeed" — an integration disposition of ANY kind presupposes work
        # that exists, and a no-change lane has none to integrate.
        journals = no_change_journals() + [
            ("400", "## Integration disposition\n- disposition: merge")
        ]
        self.assertEqual(
            fold_zero_change_record(journals).reason, ZERO_CHANGE_INTEGRATION_DECLARED
        )
        self.assertEqual(admit(journals).reason, REASON_CHANGE_DECLARED)

    def test_a_deferred_integration_disposition_also_refuses(self):
        journals = no_change_journals() + [
            ("400", "## Integration disposition\n- disposition: explicit_deferral")
        ]
        self.assertEqual(admit(journals).reason, REASON_CHANGE_DECLARED)

    def test_the_glance_agrees_with_the_retire_about_a_change_bearing_record(self):
        # The disagreement #14539 j#90137 F3 measured: one authority, two consumers. A record the
        # retire refuses must not read as "no review owed" in the glance.
        journals = [
            ("100", "## Gate: start"),
            ("300", f"## Gate: implementation done\n- commit_hash: `deadbeef1234567`\n{waiver_marker()}"),
        ]
        facts = fold_issue_gate_facts(journals)
        self.assertFalse(facts.review_waived)
        signal = lane_signal_from_gate_facts(ISSUE, facts, issue_open=True)
        self.assertEqual(classify_lane_state(signal), LANE_STATE_REVIEW_WAITING)


class HardCarveOutTest(unittest.TestCase):
    """#14695 j#93412 §3: the marker's own ``scope`` never proves external-effect absence."""

    def test_a_heading_declared_production_verification_refuses(self):
        # THE R1 defect, in the form these gates are actually written (review j#93576 finding 1):
        # a marker-only detector folded this to clear=True and admitted with reason "ok".
        journals = [
            ("100", f"## Gate: start\n\n{CARVE_OUT_CLEARED}"),
            ("200", "## Gate: production_verification\n- 本番反映を確認した"),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_DECLARED)
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_a_heading_declared_release_refuses(self):
        journals = no_change_journals() + [("400", "## Gate: release\n- 0.14.0 を publish した")]
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_a_combined_heading_naming_a_carve_out_refuses(self):
        # Over-detection is the fail-closed direction for a refusal trigger, so every part of a
        # combined heading is tested — ``Close`` must not shield ``Release``.
        journals = [
            ("100", f"## Gate: start\n\n{CARVE_OUT_CLEARED}"),
            ("300", f"## Gate: Close + Release\n{waiver_marker()}"),
        ]
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_the_canonical_marker_producer_cannot_emit_these_gates(self):
        """Why the heading is the real surface, pinned rather than asserted in prose.

        Review j#93576 finding 1(b): R1's negatives used ``gate=release`` markers, but
        ``render_workflow_event_marker`` refuses every token outside the callback-bearing
        ``GATE_BEARING_KINDS``. Those negatives therefore exercised a marker form nothing in this
        repo can produce. Marker detection is kept as defence in depth; this pins WHY it cannot be
        the only surface.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (  # noqa: E501
            GATE_BEARING_KINDS,
            render_workflow_event_marker,
        )

        for token in ("release", "production_verification", "migration"):
            with self.subTest(gate=token):
                self.assertNotIn(token, GATE_BEARING_KINDS)
                with self.assertRaises(ValueError):
                    render_workflow_event_marker(gate=token)

    def test_a_carve_out_marker_still_refuses_as_defence_in_depth(self):
        journals = no_change_journals() + [
            ("400", "[mozyo:workflow-event:gate=migration:step=3]")
        ]
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_every_preset_carve_out_bullet_is_accounted_for(self):
        """The REVERSE direction: preset -> implementation, which R2 did not have.

        R2 only checked that each implementation token appears in the preset (implementation ->
        preset). That direction cannot detect an omission, and review j#93638 finding 1 found one
        by hand: ``外部副作用`` had no token, so ``## Gate: external_effect`` admitted.

        This enumerates the preset's carve-out bullets and requires each to be explicitly
        accounted for — either by a detection token or by a named OTHER conjunct that covers it.
        A bullet the preset adds fails here until it is classified, which is the whole point.
        """
        preset = (
            ROOT / ".mozyo-bridge" / "rules" / "presets" / "redmine-governed" / "agent-workflow.md"
        ).read_text(encoding="utf-8").splitlines()
        start = next(i for i, l in enumerate(preset) if "以下の **carve-out**" in l)
        bullets = []
        for line in preset[start + 1:]:
            if line.startswith("- "):
                bullets.append(line[2:].strip())
            elif bullets and line.strip():
                break
        self.assertTrue(bullets, "the preset carve-out list could not be located")

        # Each bullet -> how this implementation accounts for it. A token means detection; a
        # reason means another conjunct refuses it before this one is consulted.
        accounted = {
            "release / tag / publish / package distribution": "release",
            "guardrail / preset / router / skill / scaffold rule 変更": (
                "covered by fold_zero_change_record: any such edit IS repository change"
            ),
            "credential / secret / auth / permission / billing / 外部 service 設定": "credential",
            "destructive operation / data 削除 / migration": "destructive_operation",
            "production verification または外部副作用を伴う操作": "production_verification",
            "legal / compliance / security-sensitive な変更": "legal",
            "仕様・scope・stakeholder 判断が未確定な issue": (
                "covered by the canonical carve_out_check field: an undetermined issue cannot "
                "carry a `none` determination, and an absent field is UNRESOLVED"
            ),
            "cross-project / cross-workspace ownership や session registry の正本変更": (
                "covered by fold_zero_change_record: a registry 正本 change IS repository change"
            ),
            "issue または parent に owner_approval_required 相当が明示されたもの": (
                "covered by the canonical carve_out_check field: the coordinator records the "
                "reason there"
            ),
        }
        self.assertEqual(
            sorted(accounted), sorted(bullets),
            "the preset's carve-out list changed; classify each new bullet as a detection token "
            "or as a named other conjunct before this suite can pass",
        )
        for bullet, how in accounted.items():
            with self.subTest(bullet=bullet):
                if " " not in how:  # a bare token means detection
                    self.assertIn(how, HARD_CARVE_OUT_GATE_TOKENS)

    def test_every_token_is_anchored_to_a_phrase_the_preset_actually_uses(self):
        """The FORWARD direction: implementation -> preset, via an explicit anchor per token.

        R2 asserted that ``token.replace("_", " ")`` appears literally in the preset. That works
        only while every carve-out happens to be written in ASCII: the preset writes two of them
        in Japanese (``data 削除``, ``外部副作用``), so the naive check cannot anchor the tokens
        covering them. Declaring the phrase each token stands for anchors all of them and keeps
        the map itself reviewable.
        """
        anchors = {
            "release": "release",
            "tag": "tag",
            "publish": "publish",
            "package_distribution": "package distribution",
            "production_verification": "production verification",
            "external_effect": "外部副作用",
            "credential": "credential",
            "auth": "auth",
            "permission": "permission",
            "billing": "billing",
            "destructive_operation": "destructive operation",
            "data_deletion": "data 削除",
            "migration": "migration",
            "legal": "legal",
            "compliance": "compliance",
            "security": "security",
        }
        self.assertEqual(
            sorted(anchors), sorted(HARD_CARVE_OUT_GATE_TOKENS),
            "every detection token needs a declared preset phrase, and every declared phrase "
            "needs a token; an unanchored token is exactly the invented vocabulary R1 shipped",
        )
        section = self._carve_out_section()
        for token, phrase in anchors.items():
            with self.subTest(token=token):
                self.assertIn(phrase, section)

    def _carve_out_section(self):
        preset = (
            ROOT / ".mozyo-bridge" / "rules" / "presets" / "redmine-governed" / "agent-workflow.md"
        ).read_text(encoding="utf-8")
        start = preset.index("以下の **carve-out**")
        return preset[start:start + 1200]

    def test_an_absent_carve_out_check_refuses_rather_than_defaulting_clear(self):
        # "既定 clear にせず typed refusal": not-found and not-checked are different answers.
        journals = [
            ("100", "## Gate: start\n- issue: #14613"),  # no owner_close_approval at all
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_UNRESOLVED)
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT_UNRESOLVED)

    def test_a_stated_carve_out_reason_refuses_even_with_everything_else_clean(self):
        """Review j#93638 finding 1, the exact reproduction.

        The record says a carve-out applies, in the governed field the preset defines for saying
        exactly that. R2 read the ``work_unit`` proxy instead and admitted this with reason ``ok``.
        """
        journals = [
            ("100", "## Gate: start\n- work_unit: `leaf_issue`"),
            (
                "200",
                "## Gate: owner_close_approval\n- approval_source: direct_owner\n"
                "- carve_out_check: production verification",
            ),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_DECLARED)
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_an_unfilled_template_line_is_not_a_determination(self):
        # ``none | <該当理由>`` copied verbatim is not the literal ``none``; a template nobody
        # filled in has determined nothing, so it lands on the refusing side.
        journals = no_change_journals()
        journals[1] = ("200", "## Gate: owner_close_approval\n- carve_out_check: none | <該当理由>")
        self.assertFalse(fold_hard_carve_out(journals).clear)
        self.assertFalse(admit(journals).admissible)

    def test_a_conflict_that_names_a_reason_takes_the_stronger_refusal(self):
        # ``none`` beside ``release`` in one journal: the stated reason disqualifies outright, so
        # this reports DECLARED rather than the weaker UNRESOLVED. Both refuse; the reported
        # cause should be the more specific true one.
        journals = no_change_journals()
        journals[1] = (
            "200",
            "## Gate: owner_close_approval\n- carve_out_check: none\n- carve_out_check: release",
        )
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_DECLARED)
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_two_differing_none_spellings_are_not_uniquely_interpretable(self):
        # The conflict path proper: neither value states a reason, but the journal declares the
        # determination twice with textually different values, so it has determined neither.
        journals = no_change_journals()
        journals[1] = (
            "200",
            "## Gate: owner_close_approval\n- carve_out_check: none\n- carve_out_check: NONE",
        )
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_UNRESOLVED)

    def test_a_stated_reason_cannot_be_cleared_by_appending_a_later_note(self):
        """Review j#93704 finding 1: the override channel.

        Plain latest-wins let a ``carve_out_check: none`` appended after an existing
        ``carve_out_check: release`` produce clear=True. This record system cannot authenticate
        the writer (one account for every role, ruling #14219 j#86718), so "the coordinator
        corrected it" and "someone appended a clear" are indistinguishable — and a fence that
        cannot tell them apart must take the refusing one.
        """
        journals = [
            ("100", "## Gate: start"),
            ("150", "## Gate: owner_close_approval\n- carve_out_check: release"),
            ("200", CARVE_OUT_CLEARED),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_DECLARED)
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT)

    def test_a_fenced_only_determination_does_not_resolve(self):
        """Review j#93704 finding 2, reproduced end-to-end then pinned.

        Gate qualification went through the quote-aware canonical scan while the FIELD was matched
        against the raw note. A ``carve_out_check: none`` appearing only inside a code fence
        therefore resolved the determination and the full admission returned ``ok``.
        """
        journals = no_change_journals()
        journals[1] = (
            "200",
            "## Gate: owner_close_approval\n- approval_source: direct_owner\n\n"
            "```\n- carve_out_check: none\n```",
        )
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_UNRESOLVED)
        self.assertEqual(admit(journals).reason, REASON_HARD_CARVE_OUT_UNRESOLVED)

    def test_a_newer_approval_omitting_the_field_shadows_an_older_clean_one(self):
        # Supersede-by-EXISTING, the invariant this context applies to every issue-wide authority.
        journals = no_change_journals() + [
            ("400", "## Gate: owner_close_approval\n- approval_source: direct_owner")
        ]
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_UNRESOLVED)

    def test_the_field_is_read_only_from_an_owner_close_approval_journal(self):
        # Qualify, then read: a stray ``carve_out_check:`` line elsewhere is not the
        # coordinator's determination.
        journals = [
            ("100", "## Gate: start\n- carve_out_check: none"),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        self.assertEqual(fold_hard_carve_out(journals).reason, CARVE_OUT_UNRESOLVED)

    def test_prose_naming_a_release_is_not_a_recognized_fact(self):
        # Structured surfaces only. A review discussing a release, or a callback quoting one,
        # would trip a keyword scan while proving nothing — a prose scan is not authority.
        journals = no_change_journals() + [
            ("400", "## Progress Log\n- release / publish / migration について議論した")
        ]
        self.assertTrue(fold_hard_carve_out(journals).clear)
        # Reaching the writer-authority refusal is how this suite says "every other conjunct
        # passed" while the route itself admits nothing (review j#93776 finding 1).
        self.assertEqual(admit(journals).reason, REASON_WRITER_AUTHORITY_UNRESOLVED)

    def test_a_quoted_carve_out_heading_is_not_a_declaration(self):
        journals = no_change_journals() + [("400", "```\n## Gate: release\n```")]
        self.assertTrue(fold_hard_carve_out(journals).clear)
        self.assertEqual(admit(journals).reason, REASON_WRITER_AUTHORITY_UNRESOLVED)


class SupersessionAndMutationTest(unittest.TestCase):
    """Stale / post-waiver states must be zero-retire."""

    def test_a_newer_review_round_re_owes_the_review(self):
        journals = no_change_journals() + [("400", "## Gate: review request\n- 対象US: #14613")]
        facts = fold_issue_gate_facts(journals)
        self.assertFalse(facts.review_waived)
        self.assertEqual(admit(journals).reason, REASON_WAIVER_SUPERSEDED)

    def test_a_review_round_BEFORE_the_waiver_does_not_supersede_it(self):
        # The negative control: supersession is about ORDER, not about existence. Without this the
        # test above would pass for a rule that simply refused any record mentioning a review.
        journals = [
            ("100", f"## Gate: start\n\n{CARVE_OUT_CLEARED}"),
            ("150", "## Gate: review request"),
            ("300", f"## Gate: close\n{waiver_marker()}"),
        ]
        # The axis under test is ORDER, so assert the supersession fact itself; the admission
        # then stops at the writer-authority gate rather than at supersession.
        self.assertTrue(fold_issue_gate_facts(journals).review_waiver_unsuperseded)
        self.assertEqual(admit(journals).reason, REASON_WRITER_AUTHORITY_UNRESOLVED)

    def test_a_moved_head_is_post_waiver_mutation(self):
        self.assertEqual(
            admit(no_change_journals(), live_head=OTHER_HEAD).reason,
            REASON_POST_WAIVER_MUTATION,
        )

    def test_lane_commits_over_the_integration_branch_refuse(self):
        self.assertEqual(
            admit(no_change_journals(), live_commits_ahead=2).reason,
            REASON_LANE_COMMITS_PRESENT,
        )

    def test_an_unmeasurable_repository_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), live_head="", live_commits_ahead=None).reason,
            REASON_LANE_HEAD_UNMEASURED,
        )
        self.assertEqual(
            admit(no_change_journals(), live_commits_ahead=None).reason,
            REASON_LANE_HEAD_UNMEASURED,
        )

    def test_a_dirty_or_unreadable_worktree_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), worktree_clean=False).reason,
            REASON_WORKTREE_NOT_CLEAN,
        )

    def test_an_owed_callback_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), callbacks_drained=False).reason, REASON_CALLBACK_OWED
        )

    def test_an_unclosed_issue_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), close_recorded=False).reason,
            REASON_CLOSE_NOT_RECORDED,
        )


class ForeignIdentityTest(unittest.TestCase):
    """Evidence from another issue, lane or generation must never unlock the fence."""

    def test_a_foreign_issue_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), target_issue="99999").reason,
            REASON_WAIVER_ISSUE_MISMATCH,
        )

    def test_a_foreign_lane_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), expected_lane="issue_99999_other").reason,
            REASON_WAIVER_LANE_MISMATCH,
        )

    def test_a_superseded_generation_refuses(self):
        self.assertEqual(
            admit(no_change_journals(), expected_lane_generation=GENERATION + 1).reason,
            REASON_WAIVER_LANE_MISMATCH,
        )

    def test_an_unresolved_retire_target_refuses(self):
        # An identity that could not be measured fences nothing, so it must not pass. This is why
        # the expectation comes from the lane's own lifecycle row and never from argv.
        for gap in ({"expected_workspace": ""}, {"expected_lane": ""},
                    {"expected_lane_generation": 0}):
            with self.subTest(gap=gap):
                self.assertEqual(
                    admit(no_change_journals(), **gap).reason, REASON_WAIVER_LANE_MISMATCH
                )


class StrictMarkerGrammarTest(unittest.TestCase):
    """Only a marker the canonical producer could render may mint the authority."""

    def test_no_waiver_at_all_is_none_not_invalid(self):
        journals = [("100", "## Gate: start"), ("300", "## Gate: close")]
        self.assertEqual(fold_no_change_review_waiver(journals).state, WAIVER_NONE)
        self.assertEqual(admit(journals).reason, REASON_NO_WAIVER_RECORDED)

    def test_a_heading_declares_but_cannot_mint(self):
        journals = [
            ("100", "## Gate: start"),
            ("300", "## Gate: close\n## Gate: no_change_review_waiver\n- approval_source: direct_owner"),
        ]
        self.assertEqual(fold_no_change_review_waiver(journals).state, WAIVER_INVALID)
        self.assertEqual(admit(journals).reason, REASON_WAIVER_INVALID)

    def test_a_newer_malformed_waiver_shadows_an_older_valid_one(self):
        # Supersede-by-EXISTING. Skipping the malformed newer record would resurrect the stale
        # authority — the invariant #14539 j#92012 F1 fixed for the exemption.
        journals = [
            ("300", f"## Gate: close\n{waiver_marker()}"),
            ("400", "## Gate: no_change_review_waiver\n(rewritten, marker dropped)"),
        ]
        self.assertEqual(fold_no_change_review_waiver(journals).state, WAIVER_INVALID)

    def test_a_note_carrying_an_unreadable_sibling_marker_is_poisoned(self):
        # A clean marker beside a forged one must NOT read like a clean note.
        poisoned = f"## Gate: close\n{waiver_marker()}\n[mozyo:workflow-event:gate={NO_CHANGE_REVIEW_WAIVER_GATE}:bogus]"
        self.assertEqual(
            fold_no_change_review_waiver([("300", poisoned)]).state, WAIVER_INVALID
        )

    def test_two_declarations_in_one_note_decide_nothing(self):
        doubled = f"## Gate: close\n{waiver_marker()}\n{waiver_marker()}"
        self.assertEqual(
            fold_no_change_review_waiver([("300", doubled)]).state, WAIVER_INVALID
        )

    def test_an_extra_field_is_refused_not_ignored(self):
        extra = waiver_marker()[:-1] + ":extra=1]"
        self.assertEqual(
            fold_no_change_review_waiver([("300", extra)]).state, WAIVER_INVALID
        )

    def test_a_quoted_marker_is_not_a_marker(self):
        quoted = "## Gate: close\n```\n" + waiver_marker() + "\n```"
        self.assertEqual(fold_no_change_review_waiver([("300", quoted)]).state, WAIVER_NONE)

    def test_a_v1_marker_is_refused_because_its_determination_is_unbound(self):
        # The schema bump is load-bearing: a v1 marker carries no ``carve_out`` field, so
        # honouring it would honour a waiver whose determination is not bound to the lane
        # (review j#93704 finding 1).
        v1 = waiver_marker().replace("version=2:", "version=1:").replace("carve_out=none:", "")
        self.assertEqual(fold_no_change_review_waiver([("300", v1)]).state, WAIVER_INVALID)

    def test_every_constant_field_is_load_bearing(self):
        canonical = waiver_marker()
        for original, tampered in (
            ("decision=waived", "decision=declined"),
            ("approval_source=direct_owner", "approval_source=standing_delegation"),
            ("scope=no_change_investigation", "scope=release"),
            ("carve_out=none", "carve_out=release"),
            ("version=2", "version=3"),
        ):
            with self.subTest(field=original):
                self.assertIn(original, canonical)
                mutated = canonical.replace(original, tampered)
                self.assertEqual(
                    fold_no_change_review_waiver([("300", mutated)]).state, WAIVER_INVALID
                )

    def test_a_permuted_field_order_is_refused(self):
        canonical = waiver_marker()
        permuted = canonical.replace(
            "version=2:approval_source=direct_owner", "approval_source=direct_owner:version=2"
        )
        self.assertNotEqual(permuted, canonical)
        self.assertEqual(
            fold_no_change_review_waiver([("300", permuted)]).state, WAIVER_INVALID
        )

    def test_a_short_or_absent_head_is_refused(self):
        for bad in ("deadbeef", "", "A" * 40):
            with self.subTest(head=bad):
                with self.assertRaises(ValueError):
                    waiver_marker(head=bad)

    def test_a_non_positive_generation_is_refused_at_write_time(self):
        # The renderer must refuse what its own parser refuses: an unreadable durable record makes
        # the authority silently not count.
        for bad in (0, -1, "x"):
            with self.subTest(generation=bad):
                with self.assertRaises(ValueError):
                    waiver_marker(generation=bad)

    def test_the_rendered_marker_round_trips(self):
        facts = fold_no_change_review_waiver([("300", waiver_marker())])
        self.assertEqual(facts.state, WAIVER_WAIVED)
        self.assertEqual(facts.issue, ISSUE)
        self.assertEqual(facts.envelope.workspace, WORKSPACE)
        self.assertEqual(facts.envelope.lane, LANE)
        self.assertEqual(facts.envelope.lane_generation, GENERATION)
        self.assertEqual(facts.head, HEAD)


class ExistingFencesUnchangedTest(unittest.TestCase):
    """Acceptance: the #14539 exemption and the ordinary generation fence stay exactly as they were."""

    def test_a_lane_with_neither_authority_is_still_fenced(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            _resolve_latest_generation_admissible,
        )

        args = argparse.Namespace(
            issue=ISSUE,
            review_generation_json=None,
            review_exemption_json=None,
            no_change_review_waiver=False,
            latest_generation_admissible=False,
        )
        self.assertFalse(_resolve_latest_generation_admissible(args).admissible)

    def test_the_operator_assertion_still_works_when_no_route_is_supplied(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            _resolve_latest_generation_admissible,
        )

        args = argparse.Namespace(
            issue=ISSUE,
            review_generation_json=None,
            review_exemption_json=None,
            no_change_review_waiver=False,
            latest_generation_admissible=True,
        )
        self.assertTrue(_resolve_latest_generation_admissible(args).admissible)

    def test_opting_into_the_waiver_route_never_falls_back_to_the_hand_assert(self):
        # "measured input を渡した場合は operator assertion へ fall back しない" (#14695 j#93412 §4).
        # No target and no repo root -> the measured route refuses, and the True assertion beside
        # it must NOT rescue the retire.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            _resolve_latest_generation_admissible,
        )

        args = argparse.Namespace(
            issue=ISSUE,
            review_generation_json=None,
            review_exemption_json=None,
            no_change_review_waiver=True,
            latest_generation_admissible=True,
        )
        self.assertFalse(_resolve_latest_generation_admissible(args).admissible)

    def test_an_exemption_lane_is_unaffected_by_the_waiver_fold(self):
        # A ``codex_direct_edit`` record carries no waiver marker, so the new fold must leave it
        # in exactly the state #14539 established.
        journals = [
            ("100", "## Gate: start"),
            (
                "200",
                "## Gate: codex_direct_edit\n- role: 実装者\n- direct_edit: true\n"
                "- allowed_paths:\n  - `vibes/docs/rules/**`\n- reason: policy\n"
                "- follow_up_review: false",
            ),
            (
                "300",
                "## Gate: implementation done\n- commit_hash: `deadbeef1234567`\n"
                "- changed_paths:\n  - `vibes/docs/rules/a.md`",
            ),
        ]
        facts = fold_issue_gate_facts(journals)
        self.assertTrue(facts.review_exempt)
        self.assertFalse(facts.review_waived)
        self.assertEqual(fold_no_change_review_waiver(journals).state, WAIVER_NONE)


class CloseFamilyProjectionTest(unittest.TestCase):
    """Review j#93856: the refusal must be visible BEFORE Close, and only while it governs."""

    def _state(self, journals, *, issue_open):
        facts = fold_issue_gate_facts(journals)
        return facts, classify_lane_state(
            lane_signal_from_gate_facts(ISSUE, facts, issue_open=issue_open)
        )

    def _closed_lane(self, conclusion, *, with_waiver):
        marker = ("\n" + waiver_marker()) if with_waiver else ""
        return [
            ("100", "## Gate: start"),
            ("200", CARVE_OUT_CLEARED + marker),
            ("300", "## Gate: review request"),
            ("400", f"## Gate: review\n- 結論: {conclusion}"),
            ("500", "## Gate: close"),
        ]

    def test_the_pre_close_gate_blocks_too_not_only_close(self):
        """Finding 1, in the exact shape it was found.

        R6 put the suppression on the ``close`` branch alone while its comment claimed the refusal
        was visible BEFORE Close. The gate one step before Close is ``owner_close_approval``, which
        never consulted it — so a record about to be closed still read as ``close_waiting``.
        """
        journals = [
            ("100", "## Gate: start"),
            ("200", CARVE_OUT_CLEARED + "\n" + waiver_marker()),
        ]
        facts, open_state = self._state(journals, issue_open=True)
        self.assertEqual(facts.latest_gate, "owner_close_approval")
        self.assertTrue(facts.review_waiver_unsupported)
        self.assertEqual(open_state, LANE_STATE_BLOCKED)
        _, closed_state = self._state(journals, issue_open=False)
        self.assertEqual(closed_state, LANE_STATE_BLOCKED)

    def test_every_gate_kind_is_classified_for_an_unsupported_waiver(self):
        """Derived from GATE_KINDS, so a gate added anywhere lands here unclassified.

        Review j#93879 finding 3: the previous version asserted `_CLOSE_FAMILY_GATES` equalled a
        literal and then looped THAT SAME SET, so a gate added to the classifier but forgotten in
        the family set kept the suite green — while the comment and the Implementation Done both
        promised the omission would be caught. The population must come from somewhere the new
        gate necessarily appears.

        `GATE_KINDS` is that independent source. Every member is pinned here, so adding one fails
        this test until its behaviour under an unsupported waiver is decided deliberately.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            _CLOSE_FAMILY_GATES,
            GATE_KINDS,
            LaneSignal,
        )

        # The close family blocks; every other gate keeps the state its own rule already gives it,
        # because an unsupported waiver must not silently change an unrelated projection.
        expected = {
            "owner_close_approval": LANE_STATE_BLOCKED,
            "close": LANE_STATE_BLOCKED,
            "start": "implementing",
            "progress": "implementing",
            "implementation_done": LANE_STATE_REVIEW_WAITING,
            "review_request": LANE_STATE_REVIEW_WAITING,
            "review": LANE_STATE_REVIEW_WAITING,
            "blocked": LANE_STATE_BLOCKED,
            "none": "idle",
        }
        self.assertEqual(
            sorted(expected), sorted(GATE_KINDS),
            "a gate kind was added or removed; classify it under an unsupported waiver here "
            "before this suite can pass",
        )
        self.assertTrue(_CLOSE_FAMILY_GATES <= set(GATE_KINDS))
        for gate, want in sorted(expected.items()):
            with self.subTest(gate=gate):
                signal = LaneSignal(issue=ISSUE, latest_gate=gate, issue_open=True,
                                    review_waiver_unsupported=True)
                self.assertEqual(classify_lane_state(signal), want)

    def test_the_close_family_is_exactly_what_blocks_that_would_not_otherwise(self):
        """The other half: which gates the waiver actually CHANGES.

        Without this, the table above would still pass if the suppression stopped working and
        every gate happened to reach its expected state by another route.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_admission import (  # noqa: E501
            _CLOSE_FAMILY_GATES,
            GATE_KINDS,
            LaneSignal,
        )

        changed = set()
        for gate in sorted(GATE_KINDS):
            clean = classify_lane_state(
                LaneSignal(issue=ISSUE, latest_gate=gate, issue_open=True)
            )
            waived = classify_lane_state(
                LaneSignal(issue=ISSUE, latest_gate=gate, issue_open=True,
                           review_waiver_unsupported=True)
            )
            if clean != waived:
                changed.add(gate)
        self.assertEqual(changed, set(_CLOSE_FAMILY_GATES))

    def test_an_approved_review_returns_the_lane_to_the_no_waiver_projection(self):
        """Finding 2: a superseded waiver must stop influencing the ordinary review route."""
        with_waiver, state = self._state(
            self._closed_lane("承認", with_waiver=True), issue_open=False
        )
        self.assertFalse(with_waiver.review_waiver_unsuperseded)
        self.assertFalse(with_waiver.review_waiver_unsupported)
        _, control = self._state(self._closed_lane("承認", with_waiver=False), issue_open=False)
        self.assertEqual(state, control)
        self.assertEqual(state, LANE_STATE_RETIRE_READY)

    def test_an_unresolved_review_is_zero_close_zero_retire(self):
        """The design ruling on j#93875, and the correction of what R7 pinned.

        R7 asserted only that the waiver and no-waiver records AGREED, and both said
        ``retire_ready`` — pinning a terminal the issue's Acceptance forbids ("pending
        callback/review では zero-close・zero-retire"). I had argued the control's behaviour put it
        out of scope; the ruling holds that a defect in the control does not void an explicit
        Acceptance, and that keeping normal review-round state after Close is part of preserving
        the ordinary review fence.

        A Close journal arriving after an unresolved review does not resolve the review, so the
        lane reads as the audit it still owes — with a waiver and without one alike.
        """
        for with_waiver in (True, False):
            with self.subTest(with_waiver=with_waiver):
                facts, state = self._state(
                    self._closed_lane("要修正", with_waiver=with_waiver), issue_open=False
                )
                self.assertTrue(facts.review_round_unresolved)
                self.assertNotEqual(state, LANE_STATE_RETIRE_READY)
                # ``changes_requested`` means the work is back with the implementer, so the lane
                # is ``implementing`` — the state the LIVE rules already give that round. R8
                # flattened it to ``review_waiting``, which is coordinator-blocking and stopped
                # the pipeline on a lane that was merely being reworked (review j#94005 F2).
                self.assertEqual(state, "implementing")

    def test_an_unresolved_review_outranks_the_integration_precedence(self):
        """Review j#94005 finding 1: the fence must fire for COMMIT-BEARING lanes too.

        R8 evaluated the integration precedence first, so the correction only ever fired for
        zero-change records: a commit-bearing lane with an unanswered review_request went to
        ``integration_waiting``, sending unreviewed work to the integration drain. Integration
        follows review approval, so review is asked first.
        """
        commit_bearing = ("200", "## Gate: implementation done\n- commit_hash: `deadbeef1234567`")
        for label, extra in (
            ("unmerged", []),
            ("explicit_deferral", [("250", "## Integration disposition\n- disposition: explicit_deferral")]),
            ("integration_blocked", [("250", "## Integration disposition\n- disposition: integration_blocked")]),
        ):
            with self.subTest(case=label):
                journals = [("100", "## Gate: start"), commit_bearing] + extra + [
                    ("300", "## Gate: review request"),
                    ("400", "## Gate: close"),
                ]
                facts, state = self._state(journals, issue_open=False)
                self.assertTrue(facts.commit_bearing)
                self.assertTrue(facts.review_round_unresolved)
                self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_integration_precedence_still_applies_once_the_review_resolves(self):
        # The negative control: with the review APPROVED, commit-bearing unmerged work is still
        # ``integration_waiting``. Without this, the test above would also pass for a change that
        # simply disabled the integration precedence.
        journals = [
            ("100", "## Gate: start"),
            ("200", "## Gate: implementation done\n- commit_hash: `deadbeef1234567`"),
            ("300", "## Gate: review request"),
            ("400", "## Gate: review\n- 結論: 承認"),
            ("500", "## Gate: close"),
        ]
        _, state = self._state(journals, issue_open=False)
        self.assertEqual(state, "integration_waiting")

    def test_an_unanswered_review_request_is_also_unresolved(self):
        # The other unresolved shape: a request nothing ever answered, then a Close.
        journals = [
            ("100", "## Gate: start"),
            ("200", CARVE_OUT_CLEARED),
            ("300", "## Gate: review request"),
            ("400", "## Gate: close"),
        ]
        facts, state = self._state(journals, issue_open=False)
        self.assertTrue(facts.review_round_unresolved)
        self.assertEqual(state, LANE_STATE_REVIEW_WAITING)

    def test_the_two_terminals_are_distinguished_not_merely_equal(self):
        """approved vs changes_requested must reach DIFFERENT terminals (the ruling's core).

        R7's failure was asserting parity without asserting the terminals themselves, so both
        being wrong still passed.
        """
        _, approved = self._state(self._closed_lane("承認", with_waiver=True), issue_open=False)
        _, changes = self._state(self._closed_lane("要修正", with_waiver=True), issue_open=False)
        _, blocker = self._state(self._closed_lane("blocker", with_waiver=True), issue_open=False)
        # Each unresolved outcome keeps its OWN state past the Close, which is the point: the live
        # rules already distinguish them and a Close must not erase that (review j#94005 F2).
        self.assertEqual(approved, LANE_STATE_RETIRE_READY)
        self.assertEqual(changes, "implementing")
        self.assertEqual(blocker, LANE_STATE_BLOCKED)
        self.assertEqual(len({approved, changes, blocker}), 3)

    def test_a_waiver_with_nothing_newer_still_blocks(self):
        """The negative control for both: an UNsuperseded waiver is still refused."""
        journals = no_change_journals()
        facts, state = self._state(journals, issue_open=False)
        self.assertTrue(facts.review_waiver_unsuperseded)
        self.assertTrue(facts.review_waiver_unsupported)
        self.assertEqual(state, LANE_STATE_BLOCKED)


class OperatorFacingReasonTest(unittest.TestCase):
    """Review j#93807 finding 2: the typed reason must survive to what the operator sees.

    The domain returned ``waiver_writer_authority_unresolvable`` while the retire decision reported
    ``stale_review_generation`` — telling the operator to go find a review generation that, for
    this route, cannot exist. A reason that dies inside a pure function is not a typed refusal.
    """

    def _decision(self, reason, **overrides):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_integration_policy import (  # noqa: E501
            RetirePreflight,
            SublaneIntegrationPolicy,
            decide_retire_integration,
        )

        kwargs = dict(
            is_git_workspace=True, target_identity_known=True, verification_passed=True,
            issue_closed=True, callbacks_drained=True, durable_record_recorded=True,
            latest_generation_admissible=False, latest_generation_blocked_reason=reason,
        )
        kwargs.update(overrides)
        return decide_retire_integration(
            SublaneIntegrationPolicy(
                manage_worktree=True, integration_branch="main-next", merge_on_retire=False
            ),
            RetirePreflight(**kwargs),
        )

    def test_the_waiver_reason_reaches_the_blocked_reasons(self):
        decision = self._decision(REASON_WRITER_AUTHORITY_UNRESOLVED)
        self.assertIn(REASON_WRITER_AUTHORITY_UNRESOLVED, decision.blocked_reasons)
        self.assertEqual(decision.primary_reason, REASON_WRITER_AUTHORITY_UNRESOLVED)
        # …and is NOT confused with the generic token the other routes still use.
        self.assertNotIn("stale_review_generation", decision.blocked_reasons)

    def test_a_route_that_says_nothing_keeps_the_generic_token(self):
        # The review-generation and #14539 routes are unchanged: no typed reason, same behaviour
        # as before this issue existed.
        decision = self._decision("")
        self.assertEqual(decision.blocked_reasons, ("stale_review_generation",))

    def test_an_unrelated_blocker_still_outranks_the_waiver_reason(self):
        # The typed reason is appended after the known precedence, so a dirty worktree is still
        # the primary diagnosis rather than being displaced by a route-supplied token.
        decision = self._decision(REASON_WRITER_AUTHORITY_UNRESOLVED, worktree_dirty=True)
        self.assertEqual(decision.primary_reason, "dirty_worktree")
        self.assertIn(REASON_WRITER_AUTHORITY_UNRESOLVED, decision.blocked_reasons)


class LiveMeasurementTest(unittest.TestCase):
    """The git probes fail closed on anything they cannot read."""

    def test_missing_refs_measure_nothing(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            measure_lane_change,
        )

        measured = measure_lane_change(ROOT, branch="", integration_branch="main-next")
        self.assertEqual(measured.head, "")
        self.assertIsNone(measured.commits_ahead)
        self.assertFalse(measured.worktree_clean)

    def test_an_unknown_ref_yields_no_head_and_no_count(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            measure_lane_change,
        )

        measured = measure_lane_change(
            ROOT, branch="no/such/branch/14695", integration_branch="main-next"
        )
        self.assertEqual(measured.head, "")
        self.assertIsNone(measured.commits_ahead)

    def test_an_absent_worktree_is_not_clean(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            measure_lane_change,
        )

        measured = measure_lane_change(
            ROOT,
            branch="HEAD",
            integration_branch="HEAD",
            worktree="/nonexistent/lane/worktree/14695",
        )
        self.assertFalse(measured.worktree_clean)

    def _branch_of(self, path):
        import subprocess

        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _head_of(self, path):
        import subprocess

        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()

    def test_a_foreign_branch_measures_NOTHING_rather_than_a_free_zero(self):
        """Review j#93576 finding 2, reproduced then pinned.

        R1 resolved the head and the ahead-count from ``--branch`` in the REPO ROOT while taking
        cleanliness from ``--worktree``, with nothing tying the two to one checkout. Pointing
        ``--branch`` at the integration branch therefore returned a foreign head, zero commits
        ahead and a clean tree — a free "this lane changed nothing" reading. Measured on this very
        worktree: actual HEAD ``156b384f``, measured head ``735a5f88``.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            measure_lane_change,
        )

        measured = measure_lane_change(
            ROOT,
            branch="origin/main-next",
            integration_branch="origin/main-next",
            worktree=str(ROOT),
        )
        # Wholly unmeasured, not a partial reading: the checkout is not on that branch.
        self.assertEqual(measured.head, "")
        self.assertIsNone(measured.commits_ahead)
        self.assertFalse(measured.worktree_clean)

    def test_the_measured_head_is_the_checkouts_own_head(self):
        # The positive control for the case above: with the branch the checkout is ACTUALLY on,
        # the measurement reports that checkout's real HEAD — not a ref resolved elsewhere.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            measure_lane_change,
        )

        branch = self._branch_of(ROOT)
        if branch in ("", "HEAD"):
            self.skipTest("detached or unreadable checkout; nothing to correlate")
        measured = measure_lane_change(
            ROOT, branch=branch, integration_branch=branch, worktree=str(ROOT)
        )
        self.assertEqual(measured.head, self._head_of(ROOT).lower())
        self.assertEqual(measured.commits_ahead, 0)

    def test_an_omitted_worktree_measures_nothing(self):
        # No fallback to the repo root: that fallback IS the decorrelation finding 2 names, and a
        # coordinator repo's state says nothing about the lane.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.retire_admissibility import (  # noqa: E501
            measure_lane_change,
        )

        branch = self._branch_of(ROOT)
        if branch in ("", "HEAD"):
            self.skipTest("detached or unreadable checkout")
        measured = measure_lane_change(ROOT, branch=branch, integration_branch=branch)
        self.assertEqual(measured.head, "")
        self.assertIsNone(measured.commits_ahead)


if __name__ == "__main__":
    unittest.main()
