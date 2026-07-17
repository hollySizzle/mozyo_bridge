"""Redmine #13920 — the adopt writer and the recover-pair reader must spell a slot alike.

The #13809 live-adopt writer pinned ``ProcessGenerationPin.role`` from
``domain.sublane_lifecycle``'s ``GATEWAY_ROLE`` / ``WORKER_ROLE`` (valued ``codex`` /
``claude``); the #13847 ``recover-pair`` reader looked its slots up with the same-NAMED
constants from ``domain.pair_launch_attestation`` (valued ``gateway`` / ``worker``). Nothing
validates the vocabulary, so an adopted lane that hibernated blocked on
``hibernated_record_missing_pins`` with its pins sitting in the row, unread.

The uncovered seam was exactly the JOIN: #13809's suite drove the writer, #13847's drove the
reader, each self-consistent with the vocabulary it imported, and no test made what the writer
wrote the input to the reader. So :class:`AdoptToRecoverPairSeamTest` runs the REAL
``declare_adopted_owner_row`` against a real isolated-home store, hibernates that row, and
hands the store to the REAL ``SublaneRecoverPairUseCase``. Reverting both halves to their
pre-#13920 state fails it — it reproduces the defect rather than describing it.

**What the join test does NOT prove**, measured by reverting each half alone rather than
assumed: with the reader's legacy read-compat in place, the join SURVIVES a writer that
regresses to the legacy spelling — compat rescues it, which is the point of compat but also
means the join cannot see that regression. The two guards mask each other. So the writer's
output is pinned separately and directly by
:meth:`AdoptToRecoverPairSeamTest.test_adopt_declares_the_canonical_vocabulary`; that test,
not the join, is what fails if a future edit copies a legacy constant import back in.

The rest pins the contract the join now rests on (Redmine #13920 acceptance 2/3): a pre-#13920
legacy row still resolves (read-compat — no migrating write), and every row that is non-empty
but does NOT name an unambiguous pair fails closed with nothing closed and nothing sent, so
"the row has pins" is never itself the proof.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    DecisionPointer,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessGenerationPin,
    ProcessPinError,
)
from mozyo_bridge.core.state.lane_pin_role import (  # noqa: E402
    PIN_PAIR_ABSENT,
    PIN_PAIR_DUPLICATE,
    PIN_PAIR_FOREIGN,
    PIN_PAIR_INCOMPLETE,
    PIN_PAIR_MIXED,
    PIN_PAIR_OK,
    PIN_PAIR_UNREADABLE,
    PIN_ROLE_GATEWAY,
    PIN_ROLE_WORKER,
    PIN_VOCABULARY_CANONICAL,
    PIN_VOCABULARY_LEGACY,
    canonical_pin_role,
    read_declared_pin_pair,
    resolve_declared_pin_pair,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402,E501
    ADOPT_DECL_DECLARED,
    declare_adopted_owner_row,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernated_pair_recovery import (  # noqa: E402,E501
    BLOCK_MISSING_PINS,
    RecoverPairRequest,
    SublaneRecoverPairUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_resume import (  # noqa: E402,E501
    ResumeOutcome,
    ResumePreflight,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernated_pair_recovery import (  # noqa: E402,E501
    SlotRecoveryObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E402,E501
    GATEWAY_ROLE as LEGACY_GATEWAY_ROLE,
    WORKER_ROLE as LEGACY_WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

WS = "ws-13920"
ISSUE = "13920"
LANE = "issue_13920_adopt_recover_role_vocabulary"
JOURNAL = "80573"
APPROVAL_JOURNAL = "80590"
ORIGINAL_IR_JOURNAL = "80573"
GW_PROVIDER = "codex"
WK_PROVIDER = "claude"
PROVIDERS = (GW_PROVIDER, WK_PROVIDER)
GW_LOC = "w28:p4R"
WK_LOC = "w28:p4S"
ATTESTED_AT = "2026-07-17T00:00:00+00:00"


def _decision(journal: str = JOURNAL) -> DecisionPointer:
    return DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=journal)


def _row(provider: str, locator: str) -> dict:
    return {"name": encode_assigned_name(WS, provider, LANE), "pane_id": locator}


def _pair_rows() -> list:
    return [_row(GW_PROVIDER, GW_LOC), _row(WK_PROVIDER, WK_LOC)]


def _healthy_obs() -> SlotRecoveryObservation:
    """A slot that is positively the pair's own bad generation (recoverable)."""
    return SlotRecoveryObservation(
        slot_absent=False, identity_resolved=True, belongs_to_pair=True,
        generation_not_newer=True, not_productive=True, no_pending_composer=True,
        worktree_readable=True, is_bad_generation=True, already_healthy=False,
    )


class _FakeOps:
    """Records every destructive effect so a fail-closed run can be proven zero-write."""

    def __init__(self) -> None:
        self.closed: list = []
        self.relaunched = False
        self.redispatched = None
        self.observed: list = []

    def workspace_id(self) -> str:
        return WS

    def observe_slot(self, *, role, provider, workspace_id, lane, record):
        self.observed.append((role, provider))
        locator = GW_LOC if role == PIN_ROLE_GATEWAY else WK_LOC
        return _healthy_obs(), locator, encode_assigned_name(WS, provider, lane)

    def close_bad_slot(self, *, role, provider, assigned_name, locator, action_id) -> bool:
        self.closed.append((role, provider, locator))
        return True

    def relaunch_pair(self, *, action_id) -> bool:
        self.relaunched = True
        return True

    def redispatch_to_gateway(self, **kw) -> str:
        self.redispatched = kw
        return "redispatched"


class _FakeResume:
    def __init__(self) -> None:
        self.ran = False

    def run(self, request, *, execute):
        self.ran = True
        pf = ResumePreflight(
            lane_hibernated=True, release_settled=True, issue_not_reowned=True,
            pair_both_slots_live=True, pair_attested=True,
        )
        return ResumeOutcome(
            executed=True, preflight=pf, issue=request.issue, lane=request.lane,
            detail="fake resume",
        )


class _HomeBackedCase(unittest.TestCase):
    """A hermetic isolated home — never the shared ``$HOME/.mozyo_bridge``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.coord = Path(self._tmp.name) / "coord"
        self.coord.mkdir()
        self.worktree = str(Path(self._tmp.name) / "wt_lane")
        self.key = LaneLifecycleKey(WS, LANE)
        self.addCleanup(self._tmp.cleanup)

    def _lifecycle(self) -> LaneLifecycleStore:
        return LaneLifecycleStore(home=self.home)

    def _rec(self):
        return self._lifecycle().get(self.key)

    def _hibernate(self) -> None:
        store = self._lifecycle()
        rec = store.get(self.key)
        out = store.transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rec.revision,
            target=DISPOSITION_HIBERNATED,
            decision=_decision(),
        )
        self.assertTrue(out.applied, f"seed hibernate refused: {out.reason}")

    def _recover(self, ops=None, resume=None):
        ops = ops if ops is not None else _FakeOps()
        use_case = SublaneRecoverPairUseCase(
            ops=ops,
            store=self._lifecycle(),
            resume=resume if resume is not None else _FakeResume(),
        )
        request = RecoverPairRequest(
            issue=ISSUE, lane=LANE, journal=APPROVAL_JOURNAL,
            implementation_request_journal=ORIGINAL_IR_JOURNAL,
        )
        return use_case.run(request, execute=True), ops

    def _declare_row(self, pins) -> None:
        """Seed an active owner row carrying exactly ``pins`` (bypasses the adopt gate)."""
        out = LaneDeclarationStore(home=self.home).declare_lane(
            self.key,
            decision=_decision(),
            binding_kind="issue",
            issue_id=ISSUE,
            declared_slots=pins,
            worktree_identity="wt_token_13920",
        )
        self.assertTrue(out.applied, f"seed declare refused: {out.reason}")


class AdoptToRecoverPairSeamTest(_HomeBackedCase):
    """The join #13920 is about: the REAL writer's row, read by the REAL reader.

    These drive the two production modules against one store, so they state the behaviour the
    issue asks for (acceptance 1: a new adopt -> hibernate -> recover-pair preflight resolves
    its pins) rather than either module's internals. Reverting both halves to pre-#13920 fails
    the join, so it reproduces the shipped defect. See the module docstring for the measured
    limit: the join alone cannot catch a writer-only regression, because the reader's
    read-compat absorbs it — ``test_adopt_declares_the_canonical_vocabulary`` covers that.
    """

    def _attest(self, provider: str, locator: str) -> None:
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                assigned_name=encode_assigned_name(WS, provider, LANE),
                workspace_id=WS, role=provider, lane_id=LANE, locator=locator,
                verdict=VERDICT_PRESENT, observed_at=ATTESTED_AT,
            )
        )

    def _adopt(self) -> str:
        self._attest(GW_PROVIDER, GW_LOC)
        self._attest(WK_PROVIDER, WK_LOC)
        return declare_adopted_owner_row(
            journal=JOURNAL, issue=ISSUE, lane_label=LANE,
            repo_root=self.coord, worktree_path=self.worktree,
            workspace_id=WS, lane_id=LANE, providers=PROVIDERS, rows=_pair_rows(),
            store_factory=lambda: LaneDeclarationStore(home=self.home),
            attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=self.home),
        )

    def test_adopted_then_hibernated_lane_resolves_its_pins_in_recover_pair(self) -> None:
        """Acceptance 1 — the defect: this blocked on ``missing_pins`` before #13920."""
        self.assertEqual(self._adopt(), ADOPT_DECL_DECLARED)
        self._hibernate()

        outcome, ops = self._recover()

        self.assertTrue(
            outcome.preflight.record_has_pins,
            "recover-pair read the adopt-declared row as pin-less: "
            f"{outcome.preflight.blocked_reasons}",
        )
        self.assertEqual(outcome.preflight.pins_reason, PIN_PAIR_OK)
        self.assertFalse(outcome.is_blocked, outcome.detail)
        # The pair resolved to the right halves, not merely "two pins".
        self.assertIsNotNone(outcome.preflight.gateway)
        self.assertIsNotNone(outcome.preflight.worker)
        self.assertEqual(outcome.preflight.gateway.provider, GW_PROVIDER)
        self.assertEqual(outcome.preflight.worker.provider, WK_PROVIDER)

    def test_recover_pair_reaches_the_live_pair_by_provider_not_by_slot_label(self) -> None:
        """The slot label must never reach a provider-keyed live lookup or a close.

        ``observe_slot`` / ``close_bad_slot`` resolve a live pane BY PROVIDER, so a canonical
        label arriving in that position would resolve nothing — the failure mode a vocabulary
        change most plausibly introduces downstream of the pins themselves.

        This characterizes the wiring; it does not guard the ``_slot_plan`` role-to-provider
        fallback that #13920 removed, which no test can bite: a decoded ``ProcessGenerationPin``
        cannot carry an empty provider (the model refuses one), so the fallback was unreachable
        in production and its removal is a clarity fix, not a behaviour fix.
        """
        self.assertEqual(self._adopt(), ADOPT_DECL_DECLARED)
        self._hibernate()

        outcome, ops = self._recover()

        self.assertEqual(
            sorted(ops.observed),
            sorted([(PIN_ROLE_GATEWAY, GW_PROVIDER), (PIN_ROLE_WORKER, WK_PROVIDER)]),
        )
        for _role, provider, _locator in ops.closed:
            self.assertIn(provider, PROVIDERS)
            self.assertNotIn(
                provider, (PIN_ROLE_GATEWAY, PIN_ROLE_WORKER),
                "a slot label leaked into the provider position",
            )
        self.assertIsNotNone(outcome.redispatch)

    def test_adopt_declares_the_canonical_vocabulary(self) -> None:
        """The writer's output, pinned directly — the ONLY guard that bites a writer regression.

        Measured, not assumed: with the reader's legacy read-compat in place, reverting this
        writer to ``codex`` / ``claude`` leaves every other test in this file green, including
        the join. Read-compat is what makes an existing legacy row recoverable, and the same
        breadth makes the join blind to a new writer that regresses into that spelling. Pin the
        write side where nothing can absorb it.
        """
        self.assertEqual(self._adopt(), ADOPT_DECL_DECLARED)
        roles = sorted(p.role for p in self._rec().declared_pins)
        self.assertEqual(roles, sorted([PIN_ROLE_GATEWAY, PIN_ROLE_WORKER]))
        self.assertNotIn(LEGACY_GATEWAY_ROLE, roles)
        self.assertNotIn(LEGACY_WORKER_ROLE, roles)


class LegacyRowCompatTest(_HomeBackedCase):
    """Acceptance 2 — an existing exact legacy row stays recoverable, without a rewrite."""

    def _legacy_pins(self) -> tuple:
        return (
            ProcessGenerationPin(
                role=LEGACY_GATEWAY_ROLE, provider=GW_PROVIDER,
                assigned_name=encode_assigned_name(WS, GW_PROVIDER, LANE), locator=GW_LOC,
            ),
            ProcessGenerationPin(
                role=LEGACY_WORKER_ROLE, provider=WK_PROVIDER,
                assigned_name=encode_assigned_name(WS, WK_PROVIDER, LANE), locator=WK_LOC,
            ),
        )

    def test_legacy_exact_row_is_recoverable(self) -> None:
        self._declare_row(self._legacy_pins())
        self._hibernate()

        outcome, _ops = self._recover()

        self.assertTrue(
            outcome.preflight.record_has_pins,
            f"a pre-#13920 exact row must stay recoverable: {outcome.preflight.blocked_reasons}",
        )
        self.assertFalse(outcome.is_blocked, outcome.detail)
        self.assertEqual(outcome.preflight.gateway.provider, GW_PROVIDER)
        self.assertEqual(outcome.preflight.worker.provider, WK_PROVIDER)

    def test_reading_a_legacy_row_does_not_rewrite_it(self) -> None:
        """Read-compat, not a migrating read: no shared-home row changes as a side effect."""
        self._declare_row(self._legacy_pins())
        self._hibernate()
        before = self._rec()

        self._recover()

        after = self._rec()
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(
            sorted(p.role for p in after.declared_pins),
            sorted([LEGACY_GATEWAY_ROLE, LEGACY_WORKER_ROLE]),
            "the legacy row was re-spelled by a read; repair-pins is the explicit rail",
        )

    def test_legacy_row_reads_complete_for_the_adopt_completeness_gate(self) -> None:
        """The adopt path's own reader must not newly fail-closed on rows it once wrote.

        ``_binding_has_required_pins`` gates ``already_owned``; regressing it would block
        dispatch on lanes #13810 established.
        """
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            _binding_has_required_pins,
        )

        self._declare_row(self._legacy_pins())
        self.assertTrue(_binding_has_required_pins(self._rec(), PROVIDERS))
        # A foreign / swapped provider pair is still refused (the #13809 j#78945 item 2 pin).
        self.assertFalse(_binding_has_required_pins(self._rec(), ("gemini", "claude")))
        self.assertFalse(_binding_has_required_pins(self._rec(), (WK_PROVIDER, GW_PROVIDER)))


class AmbiguousRowFailsClosedTest(_HomeBackedCase):
    """Acceptance 3 — mixed / partial / duplicate / foreign is zero-close, zero-send.

    Each row below is NON-EMPTY. That is the whole point: the pre-#13920 reader treated
    "resolved two roles" as proof, and its ``{role: pin}`` map silently kept the FIRST pin on
    a duplicate. A row that does not name an unambiguous pair must block instead.
    """

    def _pin(self, role: str, provider: str, locator: str) -> ProcessGenerationPin:
        return ProcessGenerationPin(
            role=role, provider=provider,
            assigned_name=encode_assigned_name(WS, provider, LANE), locator=locator,
        )

    def _assert_blocked(self, pins, expected_reason: str) -> None:
        self._declare_row(pins)
        self._hibernate()

        outcome, ops = self._recover()

        self.assertTrue(outcome.is_blocked, "an ambiguous row must not recover")
        self.assertFalse(outcome.preflight.record_has_pins)
        self.assertEqual(outcome.preflight.pins_reason, expected_reason)
        self.assertIn(
            f"{BLOCK_MISSING_PINS}:{expected_reason}", outcome.preflight.blocked_reasons
        )
        # zero-write / zero-send: nothing closed, nothing relaunched, nothing redelivered.
        self.assertEqual(ops.closed, [])
        self.assertFalse(ops.relaunched)
        self.assertIsNone(ops.redispatched)

    def test_mixed_vocabulary_row_fails_closed(self) -> None:
        """Two disagreeing writers touched one row; neither is authoritative."""
        self._assert_blocked(
            (
                self._pin(PIN_ROLE_GATEWAY, GW_PROVIDER, GW_LOC),
                self._pin(LEGACY_WORKER_ROLE, WK_PROVIDER, WK_LOC),
            ),
            PIN_PAIR_MIXED,
        )

    def test_duplicate_slot_row_fails_closed(self) -> None:
        """``codex`` + ``gateway`` are ONE slot — a duplicate the pin model cannot see.

        ``validate_declared_slots`` dedupes on the RAW ``(role, provider, assigned_name)``, so
        these two are distinct identities to it and the row stores happily. First-wins here
        would recover a pair whose halves were never established.
        """
        self._assert_blocked(
            (
                self._pin(LEGACY_GATEWAY_ROLE, GW_PROVIDER, GW_LOC),
                self._pin(PIN_ROLE_GATEWAY, WK_PROVIDER, WK_LOC),
            ),
            PIN_PAIR_MIXED,  # mixed is the more precise verdict; both are fail-closed
        )

    def test_same_vocabulary_duplicate_slot_row_fails_closed(self) -> None:
        self._assert_blocked(
            (
                self._pin(PIN_ROLE_GATEWAY, GW_PROVIDER, GW_LOC),
                self._pin(PIN_ROLE_GATEWAY, WK_PROVIDER, WK_LOC),
            ),
            PIN_PAIR_DUPLICATE,
        )

    def test_partial_pair_row_fails_closed(self) -> None:
        self._assert_blocked(
            (self._pin(PIN_ROLE_GATEWAY, GW_PROVIDER, GW_LOC),),
            PIN_PAIR_INCOMPLETE,
        )

    def test_foreign_role_row_fails_closed(self) -> None:
        self._assert_blocked(
            (
                self._pin(PIN_ROLE_GATEWAY, GW_PROVIDER, GW_LOC),
                self._pin("supervisor", WK_PROVIDER, WK_LOC),
            ),
            PIN_PAIR_FOREIGN,
        )

    def test_pinless_row_still_fails_closed(self) -> None:
        self._declare_row(())
        self._hibernate()

        outcome, ops = self._recover()

        self.assertTrue(outcome.is_blocked)
        self.assertEqual(outcome.preflight.pins_reason, PIN_PAIR_ABSENT)
        self.assertEqual(ops.closed, [])
        self.assertIsNone(ops.redispatched)


class PinWriterVocabularySourceTest(unittest.TestCase):
    """No pin writer may source a slot role from the legacy provider constants.

    The read-compat that keeps existing rows recoverable also absorbs a NEW writer regressing
    to the legacy spelling, so a behavioural test cannot see it at the consumer (measured — see
    the module docstring). ``sublane_adopt_declaration`` is pinned directly by its own output
    test; its two sibling writers are not cheaply drivable, and the trap that produced #13920 is
    precisely "copy the constant import from a sibling module".

    So this reads the import statements. It proves only that: a writer could still hard-code the
    literal ``"codex"`` as a role and slip past. That is the accepted limit — this guards the
    copy-the-import path, which is the one that actually happened, twice.
    """

    #: Every module that constructs a ``ProcessGenerationPin`` role (Redmine #13920 inventory).
    PIN_WRITERS = (
        "e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/application/sublane_adopt_declaration.py",
        "e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/application/sublane_hibernated_live_reconcile.py",
        "e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/application/sublane_hibernated_pin_repair.py",
    )

    def test_pin_writers_do_not_import_the_legacy_role_constants(self) -> None:
        src = _SRC / "mozyo_bridge"
        for rel in self.PIN_WRITERS:
            path = src / rel
            self.assertTrue(path.is_file(), f"pin-writer inventory is stale: {rel}")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "domain.sublane_lifecycle import",
                text.replace("\n", " "),
                f"{rel} imports from domain.sublane_lifecycle, whose GATEWAY_ROLE / WORKER_ROLE "
                "are the legacy provider tokens; pin roles come from core.state.lane_pin_role",
            )
            self.assertIn(
                "core.state.lane_pin_role import",
                text.replace("\n", " "),
                f"{rel} writes pin roles without sourcing them from the vocabulary owner",
            )


class PinRoleVocabularyTest(unittest.TestCase):
    """The owner boundary itself (pure)."""

    def test_canonical_roles_map_to_themselves(self) -> None:
        self.assertEqual(canonical_pin_role(PIN_ROLE_GATEWAY), PIN_ROLE_GATEWAY)
        self.assertEqual(canonical_pin_role(PIN_ROLE_WORKER), PIN_ROLE_WORKER)

    def test_legacy_roles_read_as_their_slot(self) -> None:
        self.assertEqual(canonical_pin_role(LEGACY_GATEWAY_ROLE), PIN_ROLE_GATEWAY)
        self.assertEqual(canonical_pin_role(LEGACY_WORKER_ROLE), PIN_ROLE_WORKER)

    def test_foreign_and_empty_roles_resolve_to_nothing(self) -> None:
        for raw in ("", "   ", "supervisor", "project_gateway", "gemini", None, 3):
            self.assertEqual(canonical_pin_role(raw), "", f"{raw!r} must not name a slot")

    def test_the_two_role_vocabularies_really_do_differ(self) -> None:
        """Pins the trap itself, so a 'tidy the imports' edit cannot silently re-arm it.

        These constants share their NAMES across two modules and differ in VALUE. The legacy
        pair cannot simply be repointed: those values are the herdr assigned-name role segment
        (a provider token) used for pane routing and name decoding.
        """
        self.assertEqual(PIN_ROLE_GATEWAY, "gateway")
        self.assertEqual(PIN_ROLE_WORKER, "worker")
        self.assertEqual(LEGACY_GATEWAY_ROLE, "codex")
        self.assertEqual(LEGACY_WORKER_ROLE, "claude")
        self.assertNotEqual(PIN_ROLE_GATEWAY, LEGACY_GATEWAY_ROLE)
        self.assertNotEqual(PIN_ROLE_WORKER, LEGACY_WORKER_ROLE)

    def test_pair_launch_attestation_stays_in_step_with_the_owner(self) -> None:
        """The attestation leaf duplicates these values by design; drift would re-split them."""
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            pair_launch_attestation as attest,
        )

        self.assertEqual(attest.GATEWAY_ROLE, PIN_ROLE_GATEWAY)
        self.assertEqual(attest.WORKER_ROLE, PIN_ROLE_WORKER)

    def _pin(self, role: str, provider: str = "codex") -> ProcessGenerationPin:
        return ProcessGenerationPin(
            role=role, provider=provider, assigned_name=f"mzb1_{provider}", locator="w1:p1",
        )

    def test_resolve_reports_the_vocabulary_it_read(self) -> None:
        canonical = resolve_declared_pin_pair(
            (self._pin(PIN_ROLE_GATEWAY, "codex"), self._pin(PIN_ROLE_WORKER, "claude"))
        )
        self.assertTrue(canonical.ok)
        self.assertEqual(canonical.vocabulary, PIN_VOCABULARY_CANONICAL)
        self.assertFalse(canonical.is_legacy)

        legacy = resolve_declared_pin_pair(
            (self._pin(LEGACY_GATEWAY_ROLE, "codex"), self._pin(LEGACY_WORKER_ROLE, "claude"))
        )
        self.assertTrue(legacy.ok)
        self.assertEqual(legacy.vocabulary, PIN_VOCABULARY_LEGACY)
        self.assertTrue(legacy.is_legacy)

    def test_resolution_assigns_the_halves_not_just_a_count(self) -> None:
        pair = resolve_declared_pin_pair(
            (self._pin(PIN_ROLE_WORKER, "claude"), self._pin(PIN_ROLE_GATEWAY, "codex"))
        )
        self.assertTrue(pair.ok)
        self.assertEqual(pair.gateway.provider, "codex")
        self.assertEqual(pair.worker.provider, "claude")

    def test_empty_is_absent_not_ok(self) -> None:
        pair = resolve_declared_pin_pair(())
        self.assertFalse(pair.ok)
        self.assertEqual(pair.reason, PIN_PAIR_ABSENT)
        self.assertIsNone(pair.gateway)

    def test_ok_requires_both_halves(self) -> None:
        """``ok`` is never true with a missing half, whatever the reason says."""
        from mozyo_bridge.core.state.lane_pin_role import DeclaredPinPair

        self.assertFalse(DeclaredPinPair(gateway=self._pin(PIN_ROLE_GATEWAY), reason=PIN_PAIR_OK).ok)
        self.assertFalse(DeclaredPinPair(worker=self._pin(PIN_ROLE_WORKER), reason=PIN_PAIR_OK).ok)

    def test_unreadable_snapshot_is_a_reason_not_a_raise(self) -> None:
        """``declared_pins`` raises on a corrupt envelope; the boundary reports it as no-pair."""

        @dataclass
        class _Corrupt:
            @property
            def declared_pins(self):
                raise ProcessPinError("declared slots version 9 is not exactly 1")

        pair = read_declared_pin_pair(_Corrupt())
        self.assertFalse(pair.ok)
        self.assertEqual(pair.reason, PIN_PAIR_UNREADABLE)


if __name__ == "__main__":
    unittest.main()
