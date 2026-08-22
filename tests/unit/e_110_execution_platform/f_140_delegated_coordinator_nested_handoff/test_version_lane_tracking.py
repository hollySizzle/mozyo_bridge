"""Pure classifier for Version-scoped drain tracking (Redmine #15844).

Isolated: the subject is ``domain/version_lane_tracking`` alone — no Redmine, no store,
no filesystem. The facts records are built directly, and the ``LaneBucketIssue`` join is
exercised through a minimal stand-in that carries the same published attributes.
"""

import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.version_lane_tracking import (  # noqa: E501
    ATTENTION_DISPOSITIONS,
    DISPOSITION_DRAIN_OWED,
    DISPOSITION_IN_FLIGHT,
    DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
    DISPOSITION_SETTLED,
    DISPOSITION_UMBRELLA_OPEN,
    DISPOSITION_UNDISPATCHED,
    DISPOSITION_UNKNOWN_ISSUE_STATE,
    TERMINAL_LANE_DISPOSITIONS,
    VERSION_ISSUE_DISPOSITIONS,
    TrackedLane,
    UnscopedLane,
    VersionIssueFacts,
    build_version_tracking,
    classify_version_issue,
    display_lane_id,
    is_renderable_lane_id,
    is_terminal_lane_disposition,
    join_version_issues,
    reboot_audit_command,
    render_version_tracking_text,
    shell_safe_command,
)


@dataclass(frozen=True)
class _BucketIssue:
    """The published ``LaneBucketIssue`` attributes the join reads (#12919)."""

    issue_id: str
    is_closed: bool = False
    is_leaf: bool = False
    tracker: Optional[str] = None
    status_name: Optional[str] = "未着手"
    parent_id: Optional[str] = None


def _facts(**kwargs) -> VersionIssueFacts:
    base = {
        "issue_id": "1",
        "is_closed": False,
        "is_leaf": True,
        "status_name": "未着手",
        "lanes": (),
    }
    base.update(kwargs)
    return VersionIssueFacts(**base)


ACTIVE = TrackedLane(lane_id="issue_1_x", lane_disposition="active")
HIBERNATED = TrackedLane(lane_id="issue_1_h", lane_disposition="hibernated")
RETIRED = TrackedLane(lane_id="issue_1_r", lane_disposition="retired")
SUPERSEDED = TrackedLane(lane_id="issue_1_s", lane_disposition="superseded")


class TerminalSetTest(unittest.TestCase):
    def test_terminal_set_is_retired_and_superseded_only(self):
        self.assertEqual(TERMINAL_LANE_DISPOSITIONS, ("retired", "superseded"))

    def test_hibernated_is_not_terminal(self):
        # A hibernated lane released its process but still owns its issue, so the drain
        # is still owed. Folding it into the terminal set would make every parked lane
        # silently disappear from tracking.
        self.assertFalse(is_terminal_lane_disposition("hibernated"))
        self.assertFalse(HIBERNATED.is_terminal)

    def test_unknown_disposition_is_not_terminal(self):
        # Fail-safe direction: an unreadable disposition keeps the lane in the owed
        # population rather than retiring it by default.
        self.assertFalse(is_terminal_lane_disposition("something_new"))
        self.assertFalse(is_terminal_lane_disposition(""))


class ClassifyVersionIssueTest(unittest.TestCase):
    def test_closed_issue_with_active_lane_is_drain_owed(self):
        """THE #15789 shape: work landed, issue closed, lane never terminalized."""
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(ACTIVE,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)
        self.assertEqual(tracking.reason, "issue_closed_lane_not_terminal")

    def test_drain_owed_names_the_lane_and_the_existing_rail_entry_point(self):
        # It names the lane and hands off; it must not name a specific recovery rail —
        # `reboot-audit` owns that judgement on a four-authority join (#14499 / #15841).
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(ACTIVE,))
        )
        self.assertEqual(
            tracking.diagnosis_steps,
            ("mozyo-bridge sublane reboot-audit --lane-label issue_1_x",),
        )

    def test_closed_issue_with_hibernated_lane_is_also_drain_owed(self):
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(HIBERNATED,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)

    def test_closed_issue_with_mixed_lanes_reports_only_the_nonterminal_one(self):
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(RETIRED, ACTIVE))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)
        self.assertEqual(len(tracking.diagnosis_steps), 1)
        self.assertIn("issue_1_x", tracking.diagnosis_steps[0])

    def test_open_issue_with_active_lane_is_in_flight(self):
        tracking = classify_version_issue(
            _facts(is_closed=False, status_name="着手中", lanes=(ACTIVE,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_IN_FLIGHT)
        self.assertEqual(tracking.diagnosis_steps, ())

    def test_closed_issue_with_only_terminal_lanes_is_settled(self):
        tracking = classify_version_issue(
            _facts(is_closed=True, status_name="クローズ", lanes=(RETIRED, SUPERSEDED))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_SETTLED)

    def test_closed_issue_with_no_lane_is_settled(self):
        tracking = classify_version_issue(_facts(is_closed=True, status_name="クローズ"))
        self.assertEqual(tracking.disposition, DISPOSITION_SETTLED)

    def test_open_issue_whose_lanes_all_terminalized_is_surfaced(self):
        # The spine calls a close-ready issue left at 着手中 a durable-state
        # inconsistency, not harmless bookkeeping — so it is a finding, not `settled`.
        tracking = classify_version_issue(
            _facts(is_closed=False, status_name="着手中", lanes=(RETIRED,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_LANE_TERMINAL_ISSUE_OPEN)

    def test_open_nonleaf_without_lane_is_umbrella_not_a_finding(self):
        tracking = classify_version_issue(_facts(is_closed=False, is_leaf=False))
        self.assertEqual(tracking.disposition, DISPOSITION_UMBRELLA_OPEN)
        self.assertFalse(tracking.needs_attention)

    def test_open_leaf_without_lane_is_undispatched(self):
        tracking = classify_version_issue(_facts(is_closed=False, is_leaf=True))
        self.assertEqual(tracking.disposition, DISPOSITION_UNDISPATCHED)


class UnreadableIssueStateTest(unittest.TestCase):
    def test_missing_status_name_is_unknown_not_open(self):
        """The normalizer defaults ``is_closed`` to False, so False alone proves nothing.

        ``_lane_bucket_issue_from_mapping`` reads ``bool(status.get("is_closed", False))``.
        An issue whose status object could not be read therefore arrives byte-identical
        to a genuinely open one. Reading ``status_name`` is what separates them; without
        it an unread issue joins the in-flight population silently.
        """
        tracking = classify_version_issue(_facts(is_closed=False, status_name=None))
        self.assertEqual(tracking.disposition, DISPOSITION_UNKNOWN_ISSUE_STATE)
        self.assertEqual(tracking.reason, "issue_status_unreadable")

    def test_blank_status_name_is_unknown(self):
        tracking = classify_version_issue(_facts(status_name="   "))
        self.assertEqual(tracking.disposition, DISPOSITION_UNKNOWN_ISSUE_STATE)

    def test_unreadable_state_wins_over_every_lane_shape(self):
        # Rule 1 is first for a reason: an unread issue must never be reported as
        # settled, which would pass off a finding about the READ as a finding about the
        # Version.
        for lanes in ((), (ACTIVE,), (RETIRED,), (RETIRED, ACTIVE)):
            with self.subTest(lanes=lanes):
                tracking = classify_version_issue(
                    _facts(status_name=None, is_closed=True, lanes=lanes)
                )
                self.assertEqual(
                    tracking.disposition, DISPOSITION_UNKNOWN_ISSUE_STATE
                )


class DecisionTableTotalityTest(unittest.TestCase):
    """The table is total and every rule is reachable (design spec `## 3.1`)."""

    def _product(self):
        for readable in (True, False):
            for closed in (True, False):
                for lanes in ((), (ACTIVE,), (RETIRED,), (RETIRED, ACTIVE)):
                    for leaf in (True, False):
                        yield _facts(
                            is_closed=closed,
                            is_leaf=leaf,
                            status_name="着手中" if readable else None,
                            lanes=lanes,
                        )

    def test_every_axis_combination_yields_a_declared_disposition(self):
        for facts in self._product():
            with self.subTest(
                readable=facts.issue_state_readable,
                closed=facts.is_closed,
                lanes=[lane.lane_id for lane in facts.lanes],
                leaf=facts.is_leaf,
            ):
                self.assertIn(
                    classify_version_issue(facts).disposition,
                    VERSION_ISSUE_DISPOSITIONS,
                )

    def test_every_declared_disposition_is_reachable(self):
        """No dead token in the vocabulary — an unreachable one is a lie in the counts."""
        reached = {classify_version_issue(f).disposition for f in self._product()}
        self.assertEqual(reached, set(VERSION_ISSUE_DISPOSITIONS))


class UmbrellaLaneIntersectionTest(unittest.TestCase):
    """The one named intersection (spec `## 3.1`, ``role_precedence``).

    Measured 2026-08-22: #15631 is a non-leaf of Version #329 *and* owns the lane
    ``issue_15631_trial``. Umbrella-ness is only ever the discriminant for "should an
    open issue with no lane count as undispatched?", so a row that has lanes must not
    consult it.
    """

    def test_umbrella_holding_an_active_lane_is_in_flight_not_umbrella_open(self):
        tracking = classify_version_issue(
            _facts(is_closed=False, is_leaf=False, lanes=(ACTIVE,))
        )
        self.assertEqual(tracking.disposition, DISPOSITION_IN_FLIGHT)

    def test_closed_umbrella_holding_an_active_lane_is_drain_owed(self):
        # The failure this ordering prevents: collapsing it to `umbrella_open` would
        # make a roll-up lane's left-behind state permanently invisible.
        tracking = classify_version_issue(
            _facts(
                is_closed=True, is_leaf=False, status_name="クローズ", lanes=(ACTIVE,)
            )
        )
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)


class AttentionSetTest(unittest.TestCase):
    def test_attention_is_exactly_the_three_owed_classes(self):
        self.assertEqual(
            ATTENTION_DISPOSITIONS,
            (
                DISPOSITION_DRAIN_OWED,
                DISPOSITION_LANE_TERMINAL_ISSUE_OPEN,
                DISPOSITION_UNKNOWN_ISSUE_STATE,
            ),
        )

    def test_in_flight_and_undispatched_are_not_attention(self):
        # Work in progress is not a finding, and an undispatched leaf is the *dispatch*
        # question `workflow dispatch-plan` already owns.
        for facts in (
            _facts(lanes=(ACTIVE,)),
            _facts(is_leaf=True),
            _facts(is_closed=True, status_name="クローズ"),
        ):
            with self.subTest(disposition=classify_version_issue(facts).disposition):
                self.assertFalse(classify_version_issue(facts).needs_attention)


class SnapshotTest(unittest.TestCase):
    def _snapshot(self):
        return build_version_tracking(
            version_id="329",
            version_name="v2.2.0",
            issues=(
                _facts(issue_id="15842", is_closed=True, status_name="クローズ",
                       lanes=(TrackedLane("issue_15842_x", "active"),)),
                _facts(issue_id="15844", status_name="未着手",
                       lanes=(TrackedLane("issue_15844_x", "active"),)),
                _facts(issue_id="15841", is_closed=True, status_name="クローズ",
                       lanes=(TrackedLane("issue_15841_x", "retired"),)),
            ),
            unscoped_lanes=(
                UnscopedLane("issue_15110_x", "15110", "active"),
            ),
        )

    def test_counts_include_every_disposition_including_the_zeroes(self):
        # An absent key would let a reader infer a zero from silence — the same mistake
        # as reading an unread authority as an empty one.
        counts = self._snapshot().counts
        self.assertEqual(set(counts), set(VERSION_ISSUE_DISPOSITIONS))
        self.assertEqual(counts[DISPOSITION_DRAIN_OWED], 1)
        self.assertEqual(counts[DISPOSITION_IN_FLIGHT], 1)
        self.assertEqual(counts[DISPOSITION_SETTLED], 1)
        self.assertEqual(counts[DISPOSITION_UNDISPATCHED], 0)

    def test_attention_holds_only_the_owed_rows(self):
        attention = self._snapshot().attention
        self.assertEqual([row.issue_id for row in attention], ["15842"])

    def test_payload_emits_no_composite_readiness_verdict(self):
        """The roll-up is a count, not a button (ADR-0011: the Version's integration
        disposition is a decision the project coordinator owns, and Version close needs
        owner approval on top)."""
        payload = self._snapshot().as_payload()
        self.assertEqual(payload["state"], "tracked")
        for forbidden in ("integration_ready", "verdict", "ready", "readiness"):
            self.assertNotIn(forbidden, payload)

    def test_payload_carries_no_issue_text(self):
        # Output hygiene (#15843 `## 出力の hygiene`): tokens and identifiers only, so a
        # snapshot can be pasted into a durable journal.
        payload = self._snapshot().as_payload()
        self._assert_no_key(payload, "subject")
        self._assert_no_key(payload, "description")

    def _assert_no_key(self, node, key):
        if isinstance(node, dict):
            self.assertNotIn(key, node)
            for value in node.values():
                self._assert_no_key(value, key)
        elif isinstance(node, list):
            for value in node:
                self._assert_no_key(value, key)

    def test_unscoped_lanes_survive_into_the_payload(self):
        payload = self._snapshot().as_payload()
        self.assertEqual(payload["unscoped_lane_count"], 1)
        self.assertEqual(payload["unscoped_lanes"][0]["issue_id"], "15110")


class RenderTest(unittest.TestCase):
    def test_unscoped_section_is_rendered_even_when_empty(self):
        """Scoping to a Version creates a fresh blind spot; the section renders
        unconditionally so "I ran version-track" never reads as "I saw every lane"."""
        text = render_version_tracking_text(
            build_version_tracking(
                version_id="1", version_name="v", issues=(_facts(),), unscoped_lanes=()
            )
        )
        self.assertIn("unscoped_lanes: 0", text)

    def test_render_names_the_rail_entry_point_for_an_owed_lane(self):
        text = render_version_tracking_text(
            build_version_tracking(
                version_id="1",
                version_name="v",
                issues=(
                    _facts(is_closed=True, status_name="クローズ", lanes=(ACTIVE,)),
                ),
            )
        )
        self.assertIn("sublane reboot-audit --lane-label issue_1_x", text)

    def test_render_reports_no_attention_explicitly(self):
        text = render_version_tracking_text(
            build_version_tracking(version_id="1", version_name="v", issues=(_facts(),))
        )
        self.assertIn("attention: none", text)


class LaneIdentitySafetyTest(unittest.TestCase):
    """A stored lane id must not be able to forge a command or a record line.

    Redmine #15844 review j#109990 finding_1 (verdict: accepted, j#109994).
    ``LaneLifecycleKey`` rejects only the empty string, so an id carrying shell
    metacharacters, a leading ``--``, or a newline is a storable value; the guard has to
    live where that id is re-emitted.
    """

    def _drain_owed(self, lane_id):
        return classify_version_issue(
            _facts(
                is_closed=True,
                status_name="クローズ",
                lanes=(TrackedLane(lane_id, "active"),),
            )
        )

    def test_shell_metacharacters_cannot_append_a_second_command(self):
        tracking = self._drain_owed("issue_15844")
        self.assertEqual(len(tracking.diagnosis_steps), 1)
        # The reported vector: a `;` in the id used to end the command and start another.
        injected = self._drain_owed("issue_15844; printf REVIEW_INJECTION")
        for step in injected.diagnosis_steps:
            self.assertNotIn("; printf", step)

    def test_an_id_that_forges_an_argv_flag_is_refused(self):
        """No shell metacharacter is needed to be dangerous.

        ``issue_1 --execute`` passes any "strip dangerous punctuation" filter untouched,
        yet lands as a SECOND argv token. Only quoting (or refusal) closes it.
        """
        tracking = self._drain_owed("issue_15844 --execute")
        self.assertEqual(tracking.diagnosis_steps, ())
        self.assertEqual(tracking.unrenderable_lane_ids, ("issue_15844 --execute",))

    def test_a_control_character_id_yields_no_command(self):
        for lane_id in (
            "issue_15844\n      $ printf NEWLINE",
            "issue_15844\r\n",
            "issue_15844\x00",
            "issue_15844\x1b[31m",
        ):
            with self.subTest(lane_id=lane_id):
                self.assertEqual(self._drain_owed(lane_id).diagnosis_steps, ())

    def test_refusing_the_command_does_not_hide_the_finding(self):
        # Owed work does not stop being owed because its id could not be vouched for.
        tracking = self._drain_owed("issue_15844\nrogue")
        self.assertEqual(tracking.disposition, DISPOSITION_DRAIN_OWED)
        self.assertTrue(tracking.needs_attention)
        self.assertEqual(len(tracking.unrenderable_lane_ids), 1)

    def test_real_corpus_shaped_ids_are_accepted(self):
        for lane_id in (
            "issue_15844_pc_instantiation",
            "codex_issue_15095_owner_direct_config",
            "default",
            "main",
        ):
            with self.subTest(lane_id=lane_id):
                self.assertTrue(is_renderable_lane_id(lane_id))
                self.assertEqual(
                    reboot_audit_command(lane_id),
                    f"mozyo-bridge sublane reboot-audit --lane-label {lane_id}",
                )

    def test_canonically_derived_gateway_lane_ids_are_accepted(self):
        """Every ``project_gateway_lane_id`` output must get a command (j#110012 f_1).

        The first guard was ``^[A-Za-z0-9_]+$``, justified by a 121-row store sample that
        happened to contain no project-gateway lane. ``pgwv1_<slug>-<digest>`` always
        carries a hyphen, so that guard refused a command to **every** canonically derived
        gateway lane. This asserts against the producer itself rather than against a
        remembered shape, including the slug edge cases (all-hyphen and non-ASCII scopes
        both reduce to a digest-only core).
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (  # noqa: E501
            project_gateway_lane_id,
        )

        for scope in (
            "giken-3800-mozyo-bridge",
            "some project",
            "A",
            "---",
            "日本語 scope",
            "x" * 80,
        ):
            with self.subTest(scope=scope):
                lane_id = project_gateway_lane_id(scope)
                self.assertTrue(
                    is_renderable_lane_id(lane_id),
                    f"canonical gateway lane {lane_id!r} was refused",
                )
                self.assertIn(lane_id, reboot_audit_command(lane_id))

    def test_a_hyphenated_create_label_is_accepted(self):
        # `parse_issue_from_lane_label` officially parses `issue[_-]<digits>` and
        # `SublaneCreateRequest` accepts the label, so refusing it left a legitimately
        # created lane without a diagnosis command.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E501
            parse_issue_from_lane_label,
        )

        self.assertEqual(parse_issue_from_lane_label("issue-15844-feature"), "15844")
        self.assertTrue(is_renderable_lane_id("issue-15844-feature"))

    def test_a_leading_hyphen_is_still_refused(self):
        """Widening for hyphens must not hand argv-flag spoofing back through them.

        A plain ``[A-Za-z0-9_-]+`` would accept these, and ``--lane-label -x`` re-opens
        exactly the vector R2 closed. The producer strips leading hyphens and prefixes
        ``pgwv1_``, so requiring a non-hyphen first character costs its output nothing.
        """
        for lane_id in ("-x", "--execute", "-", "-issue_1"):
            with self.subTest(lane_id=lane_id):
                self.assertFalse(is_renderable_lane_id(lane_id))
                self.assertIsNone(reboot_audit_command(lane_id))

    def test_the_quoting_layer_is_exercised_on_what_the_guard_would_reject(self):
        """The second layer, tested independently of the first.

        Today ``is_renderable_lane_id`` already excludes every value quoting would change,
        so going through ``reboot_audit_command`` can never reach the interesting case —
        measured: mutating the quoting away left the whole suite green. Exercising the
        renderer directly is what keeps a future loosening of the identity alphabet from
        silently reopening the injection.
        """
        self.assertEqual(
            shell_safe_command(("mozyo-bridge", "--lane-label", "a; printf X")),
            "mozyo-bridge --lane-label 'a; printf X'",
        )
        self.assertEqual(
            shell_safe_command(("cmd", "--flag", "v --execute")),
            "cmd --flag 'v --execute'",
        )
        # Ordinary tokens are left untouched, so the normal command stays readable.
        self.assertEqual(
            shell_safe_command(("mozyo-bridge", "sublane", "reboot-audit")),
            "mozyo-bridge sublane reboot-audit",
        )

    def test_command_is_none_rather_than_sanitized(self):
        # Sanitizing into something plausible would hand an operator an executable string
        # derived from an identity this surface could not verify.
        self.assertIsNone(reboot_audit_command("issue_1; rm -rf /"))
        self.assertIsNone(reboot_audit_command(""))


class DurableLineForgeryTest(unittest.TestCase):
    """A lane id must not be able to fabricate lines in the rendered record.

    The review explicitly asked for this pin. It is a *record* defect, not a shell one:
    the envelope is specified as pasteable into a durable journal (spec `## 3.4`), so an
    id that forges line structure forges the journal. ``shlex.quote`` does NOT close it —
    it single-quotes the value and leaves the newline inside.
    """

    def _render(self, lane_id):
        return render_version_tracking_text(
            build_version_tracking(
                version_id="1",
                version_name="v",
                issues=(
                    _facts(
                        is_closed=True,
                        status_name="クローズ",
                        lanes=(TrackedLane(lane_id, "active"),),
                    ),
                ),
                unscoped_lanes=(UnscopedLane(lane_id, "9999", "active"),),
            )
        )

    #: Every code point ``str.splitlines()`` treats as a boundary, re-derived from the
    #: runtime rather than remembered. The R2 assertion shape was right but its INPUT was
    #: only ``\n``, so U+2028 sailed through (j#110012 finding_2).
    LINE_BOUNDARIES = tuple(
        chr(cp) for cp in range(0x110000) if len(f"a{chr(cp)}b".splitlines()) > 1
    )

    def test_the_boundary_sweep_finds_the_known_set(self):
        # Guards the guard: if this sweep ever silently found nothing, every test below
        # would pass vacuously.
        self.assertEqual(
            [ord(ch) for ch in self.LINE_BOUNDARIES],
            [0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029],
        )

    def test_no_line_boundary_survives_the_display_escape(self):
        """The escape set is checked against the runtime's own notion of a line break.

        Enumerating instead of listing is what keeps the character class from drifting:
        the first version was hand-written to C1 and missed U+2028 / U+2029.
        """
        for ch in self.LINE_BOUNDARIES:
            with self.subTest(code_point=hex(ord(ch))):
                rendered = display_lane_id(f"a{ch}b")
                self.assertNotIn(ch, rendered)
                self.assertEqual(len(rendered.splitlines()), 1)

    def test_no_line_boundary_adds_a_line_to_the_render(self):
        clean_lines = len(self._render("issue_15844").splitlines())
        for ch in self.LINE_BOUNDARIES:
            with self.subTest(code_point=hex(ord(ch))):
                forged = self._render(f"issue_15844{ch}      $ printf INJECTION")
                self.assertEqual(
                    len(forged.splitlines()),
                    clean_lines,
                    f"U+{ord(ch):04X} in a lane id changed the record's line count",
                )

    def test_no_line_boundary_survives_into_the_payload(self):
        for ch in self.LINE_BOUNDARIES:
            with self.subTest(code_point=hex(ord(ch))):
                snapshot = build_version_tracking(
                    version_id="1",
                    version_name="v",
                    issues=(
                        _facts(
                            is_closed=True,
                            status_name="クローズ",
                            lanes=(TrackedLane(f"issue_1{ch}rogue", "active"),),
                        ),
                    ),
                    unscoped_lanes=(UnscopedLane(f"issue_2{ch}rogue", "9", "active"),),
                )
                payload = snapshot.as_payload()
                self.assertNotIn(
                    ch, payload["issues"][0]["facts"]["lanes"][0]["lane_id"]
                )
                self.assertNotIn(ch, payload["unscoped_lanes"][0]["lane_id"])
                self.assertNotIn(
                    ch, payload["issues"][0]["unrenderable_lane_ids"][0]
                )

    def test_no_boundary_at_any_position_is_renderable(self):
        """Boundaries are placed at start, middle AND end (j#110060 finding_1).

        The R3 sweep built every input as ``issue{ch}<more text>``, so it never once put a
        boundary at the END — and ``^...$`` admits exactly one such input, a trailing LF,
        because Python's ``$`` matches before a single final newline. 77 tests stayed green
        over a defect the sweep structurally could not reach. The axis that was missing was
        position, not value.
        """
        for ch in self.LINE_BOUNDARIES:
            for position, lane_id in (
                ("start", f"{ch}issue_1"),
                ("middle", f"issue_1{ch}rogue"),
                ("end", f"issue_1{ch}"),
            ):
                with self.subTest(code_point=hex(ord(ch)), position=position):
                    self.assertFalse(is_renderable_lane_id(lane_id))
                    self.assertIsNone(reboot_audit_command(lane_id))

    def test_a_trailing_newline_yields_no_command_and_no_extra_line(self):
        # The reported input, pinned by name: command withheld, finding still surfaced,
        # record line count unchanged.
        tracking = classify_version_issue(
            _facts(
                is_closed=True,
                status_name="クローズ",
                lanes=(TrackedLane("issue_15844\n", "active"),),
            )
        )
        self.assertEqual(tracking.diagnosis_steps, ())
        self.assertEqual(tracking.unrenderable_lane_ids, ("issue_15844\n",))
        self.assertEqual(
            len(self._render("issue_15844\n").splitlines()),
            len(self._render("issue_15844").splitlines()),
        )

    def test_any_emitted_command_is_a_single_line(self):
        """A property, so a future gap in my enumeration still fails something.

        Three rounds of findings have all been "the sweep was missing an axis" — values in
        R2, positions in R3. Enumeration keeps running out of axes, so this asserts the
        invariant the enumeration exists to protect, over inputs the enumeration does not
        generate.
        """
        candidates = [
            "issue_1",
            "pgwv1_a-b",
            "issue_1\n",
            "issue_1\r",
            " issue_1 ",
            "issue_1\t",
            "issue_1;x",
            "issue_1 --execute",
            "-issue_1",
            "",
            "issue_1\x85",
            "issue_1" + chr(0x2028),
            "issue_1" + chr(0x2029) + chr(0x2028),
        ]
        for lane_id in candidates:
            with self.subTest(lane_id=lane_id):
                command = reboot_audit_command(lane_id)
                if command is not None:
                    self.assertEqual(len(command.splitlines()), 1)
                    self.assertEqual(command.strip(), command)

    def test_a_newline_in_an_id_adds_no_line_to_the_render(self):
        clean = self._render("issue_15844")
        forged = self._render("issue_15844\n      $ printf NEWLINE_INJECTION")
        self.assertEqual(
            len(forged.splitlines()),
            len(clean.splitlines()),
            "a newline in a lane id changed the record's line count",
        )

    def test_non_ascii_boundaries_render_as_u_escapes(self):
        self.assertEqual(display_lane_id(f"a{chr(0x2028)}b"), "a\\u2028b")
        self.assertEqual(display_lane_id(f"a{chr(0x2029)}b"), "a\\u2029b")
        self.assertEqual(display_lane_id("a\x85b"), "a\\x85b")

    def test_the_module_source_carries_no_raw_line_separator(self):
        """The pattern's own members must be visible in the source.

        Writing U+2028 literally into the character class made the source line itself
        split under ``splitlines()`` — the same defect, one level up, in the file that
        exists to prevent it. The class is written with ``\\uXXXX`` escapes instead.
        """
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.version_lane_tracking as module  # noqa: E501

        # This file too: a test that writes the separator literally is the same hazard,
        # and it is the file most likely to want to.
        for path in (Path(module.__file__), Path(__file__)):
            source = path.read_text(encoding="utf-8")
            for ch in (chr(0x2028), chr(0x2029)):
                with self.subTest(path=path.name, code_point=hex(ord(ch))):
                    self.assertNotIn(ch, source)

    def test_a_forged_step_line_never_appears(self):
        forged = self._render("issue_15844\n      $ printf NEWLINE_INJECTION")
        for line in forged.splitlines():
            self.assertNotEqual(line.strip(), "$ printf NEWLINE_INJECTION")

    def test_control_characters_are_escaped_at_every_echo_site(self):
        # One site fixed is not the defect fixed: the id reaches the attention `lane`
        # line, the withheld-command line, and the unscoped section.
        forged = self._render("issue_15844\nrogue")
        self.assertNotIn("\nrogue", forged)
        self.assertIn("issue_15844\\x0arogue", forged)

    def test_payload_escapes_the_id_too(self):
        snapshot = build_version_tracking(
            version_id="1",
            version_name="v",
            issues=(
                _facts(
                    is_closed=True,
                    status_name="クローズ",
                    lanes=(TrackedLane("issue_1\nrogue", "active"),),
                ),
            ),
            unscoped_lanes=(UnscopedLane("issue_2\nrogue", "9999", "active"),),
        )
        payload = snapshot.as_payload()
        rendered_lane = payload["issues"][0]["facts"]["lanes"][0]["lane_id"]
        self.assertEqual(rendered_lane, "issue_1\\x0arogue")
        self.assertEqual(payload["unscoped_lanes"][0]["lane_id"], "issue_2\\x0arogue")

    def test_display_leaves_ordinary_text_alone(self):
        # The escape must not mangle a legitimate id (or non-ASCII, which the unscoped
        # section may carry from an unfamiliar lane).
        self.assertEqual(display_lane_id("issue_15844_pc"), "issue_15844_pc")
        self.assertEqual(display_lane_id("レーン"), "レーン")


class JoinTest(unittest.TestCase):
    def test_join_reads_the_published_bucket_issue_attributes(self):
        facts = join_version_issues(
            (
                _BucketIssue("15842", is_closed=True, status_name="クローズ"),
                _BucketIssue("15844", is_leaf=True, tracker="開発"),
            ),
            {"15842": [ACTIVE]},
        )
        self.assertEqual([f.issue_id for f in facts], ["15842", "15844"])
        self.assertEqual(facts[0].lanes, (ACTIVE,))
        self.assertEqual(facts[1].lanes, ())
        self.assertEqual(facts[1].tracker, "開発")

    def test_join_drops_only_rows_with_no_identity(self):
        facts = join_version_issues((_BucketIssue(""), _BucketIssue("7")), {})
        self.assertEqual([f.issue_id for f in facts], ["7"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
