"""`sublane quarantine-inspect` — public generation-bound approval observation (Redmine #14234).

The gap: `sublane quarantine --execute` requires `--assigned-name` / `--locator` /
`--action-generation` / `--approved-revision` / `--approval-observed-at`, the preflight observed
all of them but surfaced only the classification label, and `sublane list` returns none of them.
So a positive approval could only come from raw Herdr, the internal API, pane body, or a guess.

These tests pin the six scenarios acceptance 5 names — inspection→approval→execute, stale
revision, new locator, uncorrelated pending, known-marker q-enter, unreadable — plus the
value-non-exposure invariant (acceptance 2) and the typed fail-closed vocabulary (acceptance 3).
"""

from __future__ import annotations

import json
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    sublane_quarantine_inspect as inspect_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine_inspect import (  # noqa: E501
    INVENTORY_READ_INTERNAL_ERROR,
    INVENTORY_READ_PROVIDER_COMMAND_FAILED,
    INVENTORY_READ_REASONS,
    QuarantineInspectRequest,
    SublaneQuarantineInspectUseCase,
    classify_inventory_read_failure,
    format_inspect_text,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
    QuarantineInspection,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.quarantine_approval import (  # noqa: E501
    APPROVAL_ATTESTATION_UNREADABLE,
    APPROVAL_COMPOSER_UNREADABLE,
    APPROVAL_DUPLICATE_RECEIVER,
    APPROVAL_INVENTORY_UNREADABLE,
    APPROVAL_JOURNAL_PLACEHOLDER,
    APPROVAL_KNOWN_MARKER_REQUIRES_Q_ENTER,
    APPROVAL_NOT_QUARANTINE_CANDIDATE,
    APPROVAL_READY,
    APPROVAL_REASONS,
    APPROVAL_RECEIVER_ABSENT,
    APPROVAL_REVISION_UNREADABLE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_pending_composer import (  # noqa: E501
    AGENT_WORKING,
    PendingComposerSignal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.generation_mismatch_disposition import (  # noqa: E501
    DISPOSITION_COMPOSER_GENERATION_UNAVAILABLE,
    DISPOSITION_LIFECYCLE_ABSENT,
    DISPOSITION_LIFECYCLE_PINS_INVALID,
    DISPOSITION_LIFECYCLE_UNREADABLE,
    DISPOSITION_READY,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    TerminalTransportError,
)

WS = "wProj"
ISSUE = "14234"
LANE = "issue_14234_quarantine_inspection"
ROLE = "claude"
NAME = encode_assigned_name(WS, ROLE, LANE)
LOCATOR = f"{WS}:p44"
NEW_LOCATOR = f"{WS}:p77"
REVISION = 7
ATTESTED_AT = "2026-07-21T00:00:00+00:00"
#: The composer body must never reach any output. Used as a probe, exactly like the #13763 CLI test.
SECRET_BODY = "coordinatorへ… private unsent draft"


def _row(name=NAME, locator=LOCATOR, revision=REVISION):
    return {"name": name, "pane_id": locator, "revision": revision}


def _signal(**kw) -> PendingComposerSignal:
    """An attested, idle receiver holding an uncorrelatable composer (the quarantine case)."""
    base = dict(
        inventory_readable=True,
        has_pending=True,
        agent_state="awaiting_input",
        identity_attested=True,
        generation_matches=True,
        correlated_marker_ids=(),
        correlation_ambiguous=False,
    )
    base.update(kw)
    return PendingComposerSignal(**base)


class _FakeOps:
    """Stands in for the #13763 inspector, recording the request it was driven with."""

    def __init__(self, inspection: QuarantineInspection):
        self._inspection = inspection
        self.requests: list = []

    def inspect(self, request):
        self.requests.append(request)
        return self._inspection


def _inspection(**kw) -> QuarantineInspection:
    base = dict(
        workspace_id=WS,
        signal=_signal(),
        row_revision=REVISION,
        attested_at=ATTESTED_AT,
        receiver_present=True,
        composer_generation="opaque-provider-draft-generation-1",
        detail="classified_without_persisting_composer_body",
    )
    base.update(kw)
    return QuarantineInspection(**base)


class _Case(unittest.TestCase):
    def _run(
        self,
        *,
        rows=None,
        inspection=None,
        workspace=WS,
        rows_raise=False,
        lifecycle=(1, 7),
    ):
        if rows is None:
            rows = [_row()]

        def _reader():
            if rows_raise:
                raise OSError("inventory unavailable")
            return rows

        ops = _FakeOps(inspection or _inspection())
        self.ops = ops
        use_case = SublaneQuarantineInspectUseCase(
            repo_root=Path("/tmp/repo"),
            rows_reader=_reader,
            ops_factory=lambda _rows: ops,
            lifecycle_reader=(
                lifecycle
                if callable(lifecycle)
                else lambda _workspace, _lane: lifecycle
            ),
        )
        original = inspect_module.repo_scope_workspace_id
        inspect_module.repo_scope_workspace_id = lambda _root: workspace
        try:
            return use_case.run(QuarantineInspectRequest(issue=ISSUE, lane=LANE, role=ROLE))
        finally:
            inspect_module.repo_scope_workspace_id = original


class ReadyApprovalTest(_Case):
    """Acceptance 1/2: the exact tokens are returned and mint a bindable approval."""

    def test_exact_tokens_are_surfaced(self):
        out = self._run()
        self.assertEqual(out.approval_reason, APPROVAL_READY)
        self.assertTrue(out.approval_ready)
        f = out.facts
        self.assertEqual(f.assigned_name, NAME)
        self.assertEqual(f.locator, LOCATOR)
        self.assertEqual(f.agent_revision, REVISION)
        self.assertEqual(f.attested_at, ATTESTED_AT)
        self.assertEqual(f.workspace_id, WS)
        self.assertEqual(f.action_generation, f"quarantine:{LANE}:{ROLE}:{LOCATOR}")
        self.assertTrue(f.observed_at)

    def test_payload_carries_every_token_the_execute_flags_need(self):
        payload = self._run().as_payload()
        for key in (
            "assigned_name", "locator", "agent_revision", "attested_at",
            "action_generation", "observed_at", "classification", "approval_reason",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["approval_ready"], True)
        self.assertEqual(payload["is_blocked"], False)

    def test_rendered_command_matches_the_quarantine_execute_contract(self):
        out = self._run()
        argv = out.as_payload()["approval_command"]
        # Every required --execute flag must be present with the observed exact value.
        pairs = dict(zip(argv[3::2], argv[4::2]))
        self.assertEqual(pairs["--assigned-name"], NAME)
        self.assertEqual(pairs["--locator"], LOCATOR)
        self.assertEqual(pairs["--action-generation"], f"quarantine:{LANE}:{ROLE}:{LOCATOR}")
        self.assertEqual(pairs["--approved-revision"], str(REVISION))
        self.assertEqual(pairs["--approval-observed-at"], ATTESTED_AT)
        self.assertIn("--execute", argv)

    def test_approval_journal_id_is_a_placeholder_never_a_predicted_id(self):
        # The approval journal does not exist until the owner posts it; inventing one would be
        # a fabricated durable anchor.
        out = self._run()
        self.assertIn(APPROVAL_JOURNAL_PLACEHOLDER, out.approval_template)

    def test_template_is_pasteable_and_names_the_exact_generation(self):
        template = self._run().approval_template
        self.assertIn("Owner Approval", template)
        for token in (NAME, LOCATOR, str(REVISION), ATTESTED_AT):
            self.assertIn(token, template)

    def test_inspector_is_driven_with_the_discovered_identity(self):
        # Acceptance 4: the classification comes from the ONE #13763 read seam, driven with the
        # discovered (not guessed) identity and the observed revision.
        self._run()
        req = self.ops.requests[0]
        self.assertEqual(req.assigned_name, NAME)
        self.assertEqual(req.locator, LOCATOR)
        self.assertEqual(req.approved_revision, REVISION)


class ValueNonExposureTest(_Case):
    """Acceptance 2: no body / hash / length / raw ANSI / path / credential is emitted."""

    def test_composer_body_never_appears_in_any_rendering(self):
        rows = [{"name": NAME, "pane_id": LOCATOR, "revision": REVISION, "composer": SECRET_BODY}]
        out = self._run(rows=rows)
        blob = json.dumps(out.as_payload(), ensure_ascii=False) + format_inspect_text(out)
        self.assertNotIn(SECRET_BODY, blob)

    def test_payload_has_no_body_hash_length_or_path_field(self):
        payload = self._run().as_payload()
        for forbidden in ("body", "composer", "hash", "digest", "length", "path", "repo_root",
                          "cwd", "credential", "api_key"):
            self.assertNotIn(forbidden, payload)

    def test_facts_record_cannot_carry_a_body_by_shape(self):
        import dataclasses

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.quarantine_approval import (  # noqa: E501
            ApprovalFacts,
        )

        names = {f.name for f in dataclasses.fields(ApprovalFacts)}
        self.assertEqual(
            names & {"body", "composer", "hash", "length", "excerpt", "path"}, set()
        )


class TypedFailClosedTest(_Case):
    """Acceptance 3: each refusal is typed, and none of them mints a template."""

    def _assert_refused(self, out, reason):
        self.assertEqual(out.approval_reason, reason)
        self.assertIn(reason, APPROVAL_REASONS)
        self.assertFalse(out.approval_ready)
        self.assertTrue(out.is_blocked)
        self.assertEqual(out.approval_template, "")
        payload = out.as_payload()
        self.assertIsNone(payload["approval_template"])
        self.assertIsNone(payload["approval_command"])

    def test_unreadable_inventory(self):
        self._assert_refused(self._run(rows_raise=True), APPROVAL_INVENTORY_UNREADABLE)

    def test_unreadable_inventory_details_why_from_a_closed_vocabulary(self):
        # Redmine #14259: the refusal stays fail-closed, but the detail must name the ROOT
        # cause rather than echo the refusal, and must be a bare token — the exception message
        # carries raw herdr stderr and absolute paths.
        out = self._run(rows_raise=True)
        self.assertIn(out.inspection_detail, INVENTORY_READ_REASONS)
        self.assertNotEqual(out.inspection_detail, out.approval_reason)

    def test_receiver_absent_on_empty_inventory(self):
        # An empty inventory is a POSITIVE absence, never "unreadable".
        out = self._run(rows=[])
        self._assert_refused(out, APPROVAL_RECEIVER_ABSENT)
        self.assertIs(out.receiver_present, False)

    def test_duplicate_receiver_is_never_disambiguated_by_picking_one(self):
        out = self._run(rows=[_row(), _row(locator=NEW_LOCATOR)])
        self._assert_refused(out, APPROVAL_DUPLICATE_RECEIVER)
        self.assertEqual(out.facts.locator, "")  # nothing was picked

    def test_foreign_identity_is_not_matched(self):
        foreign = _row(name=encode_assigned_name("otherWs", ROLE, LANE))
        self._assert_refused(self._run(rows=[foreign]), APPROVAL_RECEIVER_ABSENT)

    def test_unreadable_revision(self):
        out = self._run(
            rows=[_row(revision="not-an-int")], inspection=_inspection(row_revision=-1)
        )
        self._assert_refused(out, APPROVAL_REVISION_UNREADABLE)

    def test_unreadable_attestation(self):
        self._assert_refused(
            self._run(inspection=_inspection(attested_at="")), APPROVAL_ATTESTATION_UNREADABLE
        )

    def test_unreadable_composer(self):
        out = self._run(
            inspection=_inspection(signal=_signal(inventory_readable=False, has_pending=None))
        )
        self._assert_refused(out, APPROVAL_COMPOSER_UNREADABLE)

    def test_working_agent_is_not_a_quarantine_candidate(self):
        out = self._run(inspection=_inspection(signal=_signal(agent_state="busy")))
        self._assert_refused(out, APPROVAL_NOT_QUARANTINE_CANDIDATE)
        self.assertEqual(out.classification.label, AGENT_WORKING)

    def test_workspace_unresolved(self):
        out = self._run(workspace="")
        self.assertFalse(out.approval_ready)
        self.assertEqual(out.approval_template, "")


class KnownMarkerTest(_Case):
    """Acceptance 5: a correlated known marker routes to q-enter, NOT to replacement."""

    def test_known_marker_refuses_approval_and_recommends_q_enter(self):
        out = self._run(
            inspection=_inspection(
                signal=_signal(correlated_marker_ids=("[mozyo:handoff:x]",))
            )
        )
        self.assertEqual(out.approval_reason, APPROVAL_KNOWN_MARKER_REQUIRES_Q_ENTER)
        self.assertTrue(out.classification.q_enter_recommended)
        self.assertEqual(out.approval_template, "")
        self.assertIn("q_enter", format_inspect_text(out))


class GenerationDriftTest(_Case):
    """Acceptance 5: a stale revision / new locator changes the generation the approval binds."""

    def test_new_locator_changes_the_action_generation(self):
        first = self._run().facts
        moved = self._run(
            rows=[_row(locator=NEW_LOCATOR)],
            inspection=_inspection(),
        ).facts
        self.assertNotEqual(first.action_generation, moved.action_generation)
        self.assertEqual(moved.action_generation, f"quarantine:{LANE}:{ROLE}:{NEW_LOCATOR}")
        # An approval minted against the old locator therefore cannot match the new receiver.
        self.assertIn(LOCATOR, first.action_generation)
        self.assertNotIn(NEW_LOCATOR, first.action_generation)

    def test_stale_revision_is_reported_so_a_reissued_approval_rebinds(self):
        fresh = self._run(
            rows=[_row(revision=REVISION + 1)],
            inspection=_inspection(row_revision=REVISION + 1),
        )
        self.assertEqual(fresh.facts.agent_revision, REVISION + 1)
        argv = fresh.as_payload()["approval_command"]
        self.assertIn(str(REVISION + 1), argv)
        # The previously rendered approval carried the OLD revision; the execute-time fence
        # compares them, so the drift is detectable rather than silently applied.
        self.assertNotEqual(fresh.facts.agent_revision, REVISION)


class DispositionLifecyclePinsTest(_Case):
    def _mismatch(
        self, composer_generation="opaque-provider-draft-generation-1", **kw
    ):
        return _inspection(
            signal=_signal(
                generation_matches=False,
                generation_axes=("pair",),
                **kw,
            ),
            composer_generation=composer_generation,
        )

    def test_positive_pins_reach_the_ready_command(self):
        out = self._run(inspection=self._mismatch(), lifecycle=(3, 11))
        self.assertEqual(out.disposition_reason, DISPOSITION_READY)
        argv = out.as_payload()["disposition"]["disposition_command"]
        self.assertEqual(argv[argv.index("--approved-lane-generation") + 1], "3")
        self.assertEqual(argv[argv.index("--approved-lifecycle-revision") + 1], "11")

    def test_missing_provider_composer_generation_mints_no_template(self):
        out = self._run(
            inspection=self._mismatch(composer_generation=""), lifecycle=(3, 11)
        )
        self.assertEqual(
            out.disposition_reason, DISPOSITION_COMPOSER_GENERATION_UNAVAILABLE
        )
        self.assertEqual(out.disposition_template, "")

    def test_absent_lifecycle_mints_no_template(self):
        out = self._run(inspection=self._mismatch(), lifecycle=None)
        self.assertEqual(out.disposition_reason, DISPOSITION_LIFECYCLE_ABSENT)
        self.assertEqual(out.disposition_template, "")

    def test_unreadable_lifecycle_mints_no_template(self):
        def unreadable(_workspace, _lane):
            raise OSError("unreadable")

        out = self._run(inspection=self._mismatch(), lifecycle=unreadable)
        self.assertEqual(out.disposition_reason, DISPOSITION_LIFECYCLE_UNREADABLE)
        self.assertEqual(out.disposition_template, "")

    def test_invalid_or_partial_lifecycle_pins_mint_no_template(self):
        for lifecycle in ((0, 7), (1, 0), ("1", 7), (True, 7), (1,)):
            with self.subTest(lifecycle=lifecycle):
                out = self._run(inspection=self._mismatch(), lifecycle=lifecycle)
                self.assertEqual(
                    out.disposition_reason, DISPOSITION_LIFECYCLE_PINS_INVALID
                )
                self.assertEqual(out.disposition_template, "")


class TextRenderingTest(_Case):
    def test_ready_text_includes_the_pasteable_block(self):
        text = format_inspect_text(self._run())
        self.assertIn("approval_ready: True", text)
        self.assertIn("paste into the approval journal", text)

    def test_refusal_text_states_approval_cannot_be_built(self):
        text = format_inspect_text(self._run(rows=[]))
        self.assertIn("positive owner approval cannot be built", text)
        self.assertIn(APPROVAL_RECEIVER_ABSENT, text)


# ---------------------------------------------------------------------------
# Inventory-read contract (Redmine #14259).
#
# These assert the MODULE'S PUBLIC CONTRACT — the trusted environment the read uses, and the
# shape of the classifier's answer — rather than the recurrence of the #14259 symptom, which is
# pinned in `tests/regressions/test_issue_14259_quarantine_inventory_parity.py`. The split
# follows `vibes/docs/logics/tests-placement-discovery-policy.md`: R3-b makes `regressions` a
# FILE-level type whose every test must be recurrence detection, so a contract assertion placed
# there disqualifies the whole file (review j#94358, verdict j#94361).
# ---------------------------------------------------------------------------

#: Stands for the operator-private material a raised read carries in its message: a non-zero
#: `herdr agent list` is re-raised with that process's raw stderr, and a failed binary
#: resolution names the absolute path it tried.
SECRET_MESSAGE = "SECRET-STDERR /" + "Users/private/path/herdr exited 3"


class InventoryReadEnvContractTest(unittest.TestCase):
    """Which trusted environment the read is performed with, and what a failure may emit."""

    def _run(self, rows_or_exc, *, env_kwargs=None):
        seen: dict = {}

        def _fake_list_rows(env):
            seen["env"] = env
            # As strict about its argument as the real reader, whose first act is this lookup.
            env.get("MOZYO_HERDR_BINARY")
            if isinstance(rows_or_exc, BaseException):
                raise rows_or_exc
            return rows_or_exc

        use_case = SublaneQuarantineInspectUseCase(
            repo_root=Path("/tmp/repo"), **(env_kwargs or {})
        )
        with mock.patch.object(inspect_module, "list_herdr_agent_rows", _fake_list_rows), \
                mock.patch.object(inspect_module, "repo_scope_workspace_id", lambda _r: WS):
            outcome = use_case.run(
                QuarantineInspectRequest(issue=ISSUE, lane=LANE, role=ROLE)
            )
        self.seen_env = seen.get("env", "<never called>")
        return outcome

    def test_explicit_env_still_wins_so_tests_and_callers_stay_hermetic(self):
        custom = {"MOZYO_HERDR_BINARY": "/opt/herdr"}
        self._run([_row()], env_kwargs={"env": custom})
        self.assertEqual(dict(self.seen_env), custom)

    def test_no_rendering_carries_the_exception_message(self):
        # Value non-exposure (the module's standing invariant) must survive the read failure
        # now carrying diagnostic information: the token crosses, the message never does.
        outcome = self._run(HerdrSessionStartError(SECRET_MESSAGE))
        blob = json.dumps(outcome.as_payload(), ensure_ascii=False) + format_inspect_text(
            outcome
        )
        for fragment in ("SECRET-STDERR", "/Users/private", "exited 3"):
            self.assertNotIn(fragment, blob)


class InventoryReadClassifierTest(unittest.TestCase):
    """`classify_inventory_read_failure` answers one closed-vocabulary token, always."""

    def test_every_classification_is_a_bare_closed_vocabulary_token(self):
        for exc in (
            AttributeError(SECRET_MESSAGE),
            HerdrSessionStartError(SECRET_MESSAGE),
            TerminalTransportError(SECRET_MESSAGE, reason=REASON_BINARY_NOT_FOUND),
            OSError(SECRET_MESSAGE),
        ):
            with self.subTest(exc=type(exc).__name__):
                token = classify_inventory_read_failure(exc)
                self.assertIn(token, INVENTORY_READ_REASONS)
                # A bare token: no whitespace, no path separator, no message fragment.
                self.assertNotIn(" ", token)
                self.assertNotIn("/", token)

    def test_cyclic_cause_chain_terminates(self):
        a = HerdrSessionStartError("a")
        b = HerdrSessionStartError("b")
        a.__cause__ = b
        b.__cause__ = a
        self.assertEqual(
            classify_inventory_read_failure(a), INVENTORY_READ_PROVIDER_COMMAND_FAILED
        )

    def test_a_reason_outside_the_transport_vocabulary_is_not_adopted(self):
        # `reason` is a common attribute name; only the closed transport vocabulary counts.
        exc = OSError("x")
        exc.reason = "totally-made-up"  # type: ignore[attr-defined]
        self.assertEqual(
            classify_inventory_read_failure(exc), INVENTORY_READ_INTERNAL_ERROR
        )

    def test_nested_cause_is_still_found(self):
        root = TerminalTransportError("x", reason=REASON_BINARY_UNCONFIGURED)
        mid = HerdrSessionStartError("mid")
        mid.__cause__ = root
        top = HerdrSessionStartError("top")
        top.__cause__ = mid
        self.assertEqual(
            classify_inventory_read_failure(top), REASON_BINARY_UNCONFIGURED
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
