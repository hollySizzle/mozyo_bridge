"""Redmine #13809 — standard live-adopt owner-row backfill via the common service.

The #13810 F1 correction (adjudication j#78878; re-review j#78890): the standard live-adopt
path (``sublane create --no-dispatch --execute`` onto a live gateway+worker pair) skipped
``append_lane_column`` and so never declared the lane's lifecycle owner row — the measured
``original_identity_unknown`` that permanently blocked ``sublane hibernate`` (#13809).

This is the **isolated synthetic official-path regression** driving the real gate over RAW
``agent list`` rows with an isolated home — never the shared ``$HOME/.mozyo_bridge`` and
never a live pane / process / route mutation. It pins the fail-closed matrix the review
(j#78890) required: raw candidate multiplicity, stale shell residue, absent / stale startup
self-attestation, recycled generation (typed pins so a different live pair is NOT an
idempotent duplicate), owner conflict, and the ops-method wiring.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore  # noqa: E402
from mozyo_bridge.core.state.lane_lifecycle import (  # noqa: E402
    CAS_ALREADY_DECLARED,
    CAS_NOT_FOUND,
    CAS_STALE_REVISION,
    CAS_UNEXPECTED_STATE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    LaneLifecycleKey,
    LaneLifecycleStore,
    OWNER_ABSENT,
    OWNER_RESOLVED,
    DecisionPointer,
    ProcessGenerationPin,
    resolve_lane_owner,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E402
    ADOPT_DECL_ALREADY_OWNED,
    ADOPT_DECL_BACKFILLED,
    ADOPT_DECL_DECLARED,
    ADOPT_DECL_DUPLICATE_CANDIDATES,
    ADOPT_DECL_INCOMPLETE_PAIR,
    ADOPT_DECL_NO_ANCHOR,
    ADOPT_DECL_NOT_ADOPTED,
    ADOPT_DECL_OWNER_CONFLICT,
    ADOPT_DECL_OWNER_UNBOUND,
    ADOPT_DECL_STALE_SLOT,
    ADOPT_DECL_UNATTESTED,
    ADOPT_DECL_UNREADABLE,
    ADOPT_DECL_UNRESOLVED_UNIT,
    _worktree_token,
    declare_adopted_owner_row,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_actuation import (  # noqa: E402
    attest_retire_target,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_retire import (  # noqa: E402
    REASON_WORKTREE_BINDING_UNVERIFIED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (  # noqa: E402
    GATEWAY_ROLE,
    WORKER_ROLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402
    encode_assigned_name,
)

WS = "ws-shared-project"
ISSUE = "13735"
LANE = "issue_13735_parallel_ci"
JOURNAL = "78400"
PROVIDERS = ("codex", "claude")
GW_LOC = "w1:pG"
WK_LOC = "w1:pW"
ATTESTED_AT = "2026-07-15T00:00:00+00:00"


def _row(provider: str, locator: str, *, stale: bool = False) -> dict:
    row = {"name": encode_assigned_name(WS, provider, LANE), "pane_id": locator}
    if stale:
        row["agent"] = ""  # detected-agent field present but blank -> SLOT_STALE residue
    return row


def _pair_rows(gw: str = GW_LOC, wk: str = WK_LOC) -> list:
    return [_row("codex", gw), _row("claude", wk)]


class DeclareAdoptedOwnerRowTest(unittest.TestCase):
    """The gate + declaration over raw rows, hermetic via injected isolated-home stores."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.coord = Path(self._tmp.name) / "coord"
        self.coord.mkdir()
        self.worktree = str(Path(self._tmp.name) / "wt_lane")
        self.addCleanup(self._tmp.cleanup)

    def _attest(self, provider: str, locator: str) -> None:
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                assigned_name=encode_assigned_name(WS, provider, LANE),
                workspace_id=WS,
                role=provider,
                lane_id=LANE,
                locator=locator,
                verdict=VERDICT_PRESENT,
                observed_at=ATTESTED_AT,
            )
        )

    def _attest_pair(self, gw: str = GW_LOC, wk: str = WK_LOC) -> None:
        self._attest("codex", gw)
        self._attest("claude", wk)

    def _call(self, rows, **overrides) -> str:
        kwargs = dict(
            journal=JOURNAL,
            issue=ISSUE,
            lane_label=LANE,
            repo_root=self.coord,
            worktree_path=self.worktree,
            workspace_id=WS,
            lane_id=LANE,
            providers=PROVIDERS,
            rows=rows,
            store_factory=lambda: LaneDeclarationStore(home=self.home),
            attestation_store_factory=lambda: HerdrIdentityAttestationStore(home=self.home),
        )
        kwargs.update(overrides)
        return declare_adopted_owner_row(**kwargs)

    def _owner(self):
        return resolve_lane_owner(WS, ISSUE, home=self.home)

    def _row_for(self):
        return LaneLifecycleStore(home=self.home).get(LaneLifecycleKey(WS, LANE))

    # -- happy path: rowless live+attested pair is backfilled with typed pins ------

    def test_rowless_attested_pair_is_backfilled_with_typed_pins(self) -> None:
        self.assertEqual(self._owner().status, OWNER_ABSENT)
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_DECLARED)
        owner = self._owner()
        self.assertEqual(owner.status, OWNER_RESOLVED)
        self.assertEqual(owner.lane_id, LANE)
        row = self._row_for()
        self.assertEqual(row.lane_disposition, DISPOSITION_ACTIVE)
        # R3-F1: the typed live pins are stored (locators enter the identity).
        locators = sorted(p.locator for p in row.declared_pins)
        self.assertEqual(locators, sorted([GW_LOC, WK_LOC]))
        # R4-F1: runtime_revision is empty (no herdr runtime surface), but the verified
        # attestation's observed_at is stored as attested_at (real evidence, not discarded).
        self.assertTrue(all(p.runtime_revision == "" for p in row.declared_pins))
        self.assertTrue(all(p.attested_at == ATTESTED_AT for p in row.declared_pins))

    def test_raw_live_plus_stale_duplicate_is_zero_write(self) -> None:
        # R4-F2 / herdr-native-identity §3.4: a duplicate assigned name is multiple_matches
        # fail-closed EVEN when one row is live and the other a locator-bearing stale residue
        # (the multiplicity is raw, checked before the liveness filter).
        self._attest_pair()
        rows = [
            _row("codex", GW_LOC),
            _row("codex", "w9:pSTALE", stale=True),  # same name, stale residue
            _row("claude", WK_LOC),
        ]
        self.assertEqual(self._call(rows), ADOPT_DECL_DUPLICATE_CANDIDATES)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_same_live_pair_is_idempotent(self) -> None:
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_DECLARED)
        rev1 = self._row_for().revision
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_DECLARED)  # same pins
        self.assertEqual(self._row_for().revision, rev1)  # idempotent, no re-write

    def test_recycled_generation_is_not_an_idempotent_duplicate(self) -> None:
        # R3-F1 core: a DIFFERENT live pair (recycled process generation) must not read as
        # the same declaration and silently update — the stored locators distinguish it, so
        # the divergent re-declare is refused zero-write (the row keeps the original pins).
        # The lane stays owner-bound (its own row), so the outcome proceeds as already_owned
        # rather than the fresh/idempotent ``declared``.
        self._attest_pair(GW_LOC, WK_LOC)
        self.assertEqual(self._call(_pair_rows(GW_LOC, WK_LOC)), ADOPT_DECL_DECLARED)
        rev1 = self._row_for().revision
        self._attest_pair("w9:p9", "w9:p8")  # attest the recycled generation
        self.assertEqual(
            self._call(_pair_rows("w9:p9", "w9:p8")), ADOPT_DECL_ALREADY_OWNED
        )
        row = self._row_for()
        self.assertEqual(row.revision, rev1)  # unchanged — the recycled pins were NOT stored
        self.assertEqual(
            sorted(p.locator for p in row.declared_pins), sorted([GW_LOC, WK_LOC])
        )  # original generation kept (recycled != idempotent overwrite)

    # -- fail-closed matrix (raw inventory) --------------------------------------

    def test_duplicate_live_candidates_are_zero_write(self) -> None:
        # Two rows decoding to the codex slot (a duplicate mzb1 name) -> ambiguous, not a
        # "first live locator wins" collapse.
        self._attest_pair()
        rows = [_row("codex", "w1:pA"), _row("codex", "w1:pB"), _row("claude", WK_LOC)]
        self.assertEqual(self._call(rows), ADOPT_DECL_DUPLICATE_CANDIDATES)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_stale_shell_residue_is_zero_write(self) -> None:
        self._attest_pair()
        rows = [_row("codex", GW_LOC, stale=True), _row("claude", WK_LOC)]
        self.assertEqual(self._call(rows), ADOPT_DECL_STALE_SLOT)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_absent_attestation_is_zero_write(self) -> None:
        # A readable live pair with NO startup self-attestation record -> unattested.
        self._attest("claude", WK_LOC)  # only the worker attested
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_UNATTESTED)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_stale_attestation_generation_is_zero_write(self) -> None:
        # The attestation's recorded locator no longer matches the live locator -> a
        # different process generation -> stale -> unattested (zero-write).
        self._attest("codex", "w1:pOLD")  # attested a prior generation
        self._attest("claude", WK_LOC)
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_UNATTESTED)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_incomplete_pair_is_zero_write(self) -> None:
        self._attest("codex", GW_LOC)
        self.assertEqual(self._call([_row("codex", GW_LOC)]), ADOPT_DECL_INCOMPLETE_PAIR)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_missing_anchor_and_unit_are_zero_write(self) -> None:
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows(), journal=""), ADOPT_DECL_NO_ANCHOR)
        self.assertEqual(self._call(_pair_rows(), workspace_id=""), ADOPT_DECL_UNRESOLVED_UNIT)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_existing_owner_conflict_is_zero_write(self) -> None:
        LaneDeclarationStore(home=self.home).declare_lane(
            LaneLifecycleKey(WS, "issue_13735_original"),
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id="1"),
            issue_id=ISSUE,
        )
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_OWNER_CONFLICT)
        self.assertEqual(self._owner().lane_id, "issue_13735_original")
        self.assertIsNone(self._row_for())

    def test_hibernate_blocker_is_cleared(self) -> None:
        self.assertEqual(self._owner().status, OWNER_ABSENT)
        self._attest_pair()
        self._call(_pair_rows())
        self.assertTrue(self._owner().resolved)

    # -- legacy active owner row: missing worktree binding backfill (j#78944/j#78945) --

    def _seed_legacy_owner_row(self) -> None:
        """A pre-#13754 legacy owner row: ``active``, owns the issue, empty worktree binding.

        Exactly the residual #13809 measured (j#78944): the owner row EXISTS (unlike the
        rowless #13835 case) but its ``worktree_identity`` is empty, so ``declare_lane`` reads
        the live worktree as a divergent re-declare and retire stays ``worktree_binding_unverified``.
        """
        out = LaneDeclarationStore(home=self.home).declare_lane(
            LaneLifecycleKey(WS, LANE),
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL),
            issue_id=ISSUE,
        )
        self.assertTrue(out.applied)
        self.assertEqual(self._row_for().worktree_identity, "")

    def test_legacy_incomplete_owner_row_is_backfilled(self) -> None:
        # The pre-existing-incomplete-row correction, reported DISTINCTLY from a rowless
        # declaration: the existing active row's empty worktree + typed pins are filled.
        self._seed_legacy_owner_row()
        rev0 = self._row_for().revision
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_BACKFILLED)
        row = self._row_for()
        self.assertTrue(row.worktree_identity)  # the missing binding was filled
        self.assertEqual(row.revision, rev0 + 1)  # one bounded CAS write
        self.assertEqual(
            sorted(p.locator for p in row.declared_pins), sorted([GW_LOC, WK_LOC])
        )
        self.assertTrue(all(p.attested_at == ATTESTED_AT for p in row.declared_pins))
        # Still the same active owner of the same issue (ownership never disrupted).
        self.assertEqual(row.lane_disposition, DISPOSITION_ACTIVE)
        self.assertEqual(row.issue_id, ISSUE)
        self.assertEqual(self._owner().lane_id, LANE)

    def test_rowless_is_declared_but_legacy_is_backfilled(self) -> None:
        # The two paths are distinct outcomes: a rowless lane DECLARES a fresh owner row; a
        # pre-existing incomplete row is BACKFILLED. (Regressed apart per j#78945 item 4.)
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_DECLARED)

    def test_backfilled_row_is_idempotent_on_re_adopt(self) -> None:
        self._seed_legacy_owner_row()
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_BACKFILLED)
        rev = self._row_for().revision
        # Second identical adopt: declare_lane now sees an EXACT duplicate -> declared,
        # never a second write.
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_DECLARED)
        self.assertEqual(self._row_for().revision, rev)

    def test_recycled_generation_never_overwrites_a_filled_binding(self) -> None:
        # Once the worktree binding is filled, a DIFFERENT live pair (recycled generation) is
        # NOT backfilled over the row: the worktree is already non-empty, so the bounded
        # surface refuses zero-write and the lane proceeds as already_owned (its own row).
        self._seed_legacy_owner_row()
        self._attest_pair(GW_LOC, WK_LOC)
        self.assertEqual(self._call(_pair_rows(GW_LOC, WK_LOC)), ADOPT_DECL_BACKFILLED)
        rev = self._row_for().revision
        self._attest_pair("w9:p9", "w9:p8")  # attest the recycled generation
        self.assertEqual(
            self._call(_pair_rows("w9:p9", "w9:p8")), ADOPT_DECL_ALREADY_OWNED
        )
        row = self._row_for()
        self.assertEqual(row.revision, rev)  # unchanged — recycled pins NOT stored
        self.assertEqual(
            sorted(p.locator for p in row.declared_pins), sorted([GW_LOC, WK_LOC])
        )

    def test_unattested_live_pair_leaves_the_legacy_gap_zero_write(self) -> None:
        # A legacy row whose live pair cannot self-attest is never backfilled: the gate fails
        # before the backfill CAS, the row keeps its empty binding, and the lane is
        # already_owned (its owner row predates this adopt) rather than DECLARED / BACKFILLED.
        self._seed_legacy_owner_row()
        # no attestation seeded -> unattested gate failure.
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_ALREADY_OWNED)
        self.assertEqual(self._row_for().worktree_identity, "")

    def test_backfill_unblocks_the_retire_worktree_fence(self) -> None:
        # #13754 retire fence: a legacy row's empty worktree binding fails the retire
        # attestation closed; after the adopt backfill the SAME token attests (synthetic,
        # isolated home — no live pane/process/route mutation and no real #13754 lane).
        self._seed_legacy_owner_row()
        token = _worktree_token(self.coord, self.worktree, LANE)
        self.assertTrue(token)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            attested, reason, _ = attest_retire_target(
                WS, LANE, issue=ISSUE, worktree_identity=token
            )
        self.assertFalse(attested)  # legacy row: no worktree binding -> fail closed
        self.assertEqual(reason, REASON_WORKTREE_BINDING_UNVERIFIED)
        self._attest_pair()
        self.assertEqual(self._call(_pair_rows()), ADOPT_DECL_BACKFILLED)
        self.assertEqual(self._row_for().worktree_identity, token)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False):
            attested, reason, _ = attest_retire_target(
                WS, LANE, issue=ISSUE, worktree_identity=token
            )
        self.assertTrue(attested)  # binding filled -> the fence now passes
        self.assertEqual(reason, "")


class BackfillActiveBindingCasTest(unittest.TestCase):
    """The bounded missing-field backfill CAS in isolation (Redmine #13809 residual).

    Fills ONLY the empty worktree binding + declared-slot snapshot of an existing ``active``
    issue owner row, guarded on the exact revision. It never relaxes ``declare_lane``'s "a
    divergent re-declare must not overwrite": a non-empty mismatch, a different / non-active /
    project-gateway row, or a revision race is zero-write.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.store = LaneDeclarationStore(home=self.home)
        self.key = LaneLifecycleKey(WS, LANE)
        self.decision = DecisionPointer(
            source="redmine", issue_id=ISSUE, journal_id=JOURNAL
        )
        self.token = "wt_lane_token"

    def _pins(self, gw: str = GW_LOC, wk: str = WK_LOC) -> list:
        return [
            ProcessGenerationPin(
                role=GATEWAY_ROLE, provider="codex", assigned_name="gw", locator=gw
            ),
            ProcessGenerationPin(
                role=WORKER_ROLE, provider="claude", assigned_name="wk", locator=wk
            ),
        ]

    def _seed_legacy(self) -> int:
        out = self.store.declare_lane(self.key, decision=self.decision, issue_id=ISSUE)
        self.assertTrue(out.applied)
        return out.revision

    def _row(self):
        return LaneLifecycleStore(home=self.home).get(self.key)

    def test_fills_missing_worktree_and_pins(self) -> None:
        rev = self._seed_legacy()
        self.assertEqual(self._row().worktree_identity, "")
        out = self.store.backfill_active_binding(
            self.key,
            expected_revision=rev,
            issue_id=ISSUE,
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        self.assertTrue(out.applied)
        self.assertEqual(out.revision, rev + 1)
        row = self._row()
        self.assertEqual(row.worktree_identity, self.token)
        self.assertEqual(
            sorted(p.locator for p in row.declared_pins), sorted([GW_LOC, WK_LOC])
        )
        self.assertEqual(row.lane_disposition, DISPOSITION_ACTIVE)  # untouched
        self.assertEqual(row.issue_id, ISSUE)

    def test_exact_backfill_is_idempotent_no_op(self) -> None:
        rev = self._seed_legacy()
        first = self.store.backfill_active_binding(
            self.key,
            expected_revision=rev,
            issue_id=ISSUE,
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        second = self.store.backfill_active_binding(
            self.key,
            expected_revision=first.revision,
            issue_id=ISSUE,
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        self.assertTrue(second.applied)
        self.assertEqual(second.revision, first.revision)  # no re-write
        self.assertEqual(self._row().revision, first.revision)

    def test_revision_race_is_zero_write(self) -> None:
        rev = self._seed_legacy()
        out = self.store.backfill_active_binding(
            self.key,
            expected_revision=rev + 5,
            issue_id=ISSUE,
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_STALE_REVISION)
        self.assertEqual(self._row().worktree_identity, "")

    def test_non_empty_worktree_mismatch_is_zero_write(self) -> None:
        out0 = self.store.declare_lane(
            self.key,
            decision=self.decision,
            issue_id=ISSUE,
            worktree_identity="wt_original",
            declared_slots=self._pins(),
        )
        out = self.store.backfill_active_binding(
            self.key,
            expected_revision=out0.revision,
            issue_id=ISSUE,
            worktree_identity="wt_different",
            declared_slots=self._pins(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_ALREADY_DECLARED)
        self.assertEqual(self._row().worktree_identity, "wt_original")

    def test_different_issue_is_zero_write(self) -> None:
        rev = self._seed_legacy()
        out = self.store.backfill_active_binding(
            self.key,
            expected_revision=rev,
            issue_id="99999",
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._row().worktree_identity, "")

    def test_non_active_disposition_is_zero_write(self) -> None:
        rev = self._seed_legacy()
        LaneLifecycleStore(home=self.home).transition_disposition(
            self.key,
            expected_disposition=DISPOSITION_ACTIVE,
            expected_revision=rev,
            target=DISPOSITION_HIBERNATED,
            decision=self.decision,
        )
        row = self._row()
        out = self.store.backfill_active_binding(
            self.key,
            expected_revision=row.revision,
            issue_id=ISSUE,
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_UNEXPECTED_STATE)
        self.assertEqual(self._row().worktree_identity, "")

    def test_missing_row_is_not_found(self) -> None:
        out = self.store.backfill_active_binding(
            self.key,
            expected_revision=1,
            issue_id=ISSUE,
            worktree_identity=self.token,
            declared_slots=self._pins(),
        )
        self.assertFalse(out.applied)
        self.assertEqual(out.reason, CAS_NOT_FOUND)

    def test_empty_issue_or_worktree_raises(self) -> None:
        rev = self._seed_legacy()
        with self.assertRaises(ValueError):
            self.store.backfill_active_binding(
                self.key, expected_revision=rev, issue_id="", worktree_identity=self.token
            )
        with self.assertRaises(ValueError):
            self.store.backfill_active_binding(
                self.key, expected_revision=rev, issue_id=ISSUE, worktree_identity=""
            )


class HerdrAdoptOwnerRowWiringTest(unittest.TestCase):
    """The official ops method wiring, isolated home, synthetic raw inventory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.coord = Path(self._tmp.name) / "coord"
        self.coord.mkdir()
        self.worktree = str(Path(self._tmp.name) / "wt_lane")
        self.addCleanup(self._tmp.cleanup)

    def _attest_pair(self) -> None:
        for provider, loc in (("codex", GW_LOC), ("claude", WK_LOC)):
            HerdrIdentityAttestationStore(home=self.home).upsert(
                IdentityAttestationRecord(
                    assigned_name=encode_assigned_name(WS, provider, LANE),
                    workspace_id=WS, role=provider, lane_id=LANE, locator=loc,
                    verdict=VERDICT_PRESENT, observed_at=ATTESTED_AT,
                )
            )

    def _ops(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )

        return HerdrSublaneActuatorOps(
            repo_root=self.coord, lane_label=LANE, issue=ISSUE, journal=JOURNAL,
            env={"MOZYO_BRIDGE_HOME": str(self.home)}, runner=lambda *a, **k: None,
        )

    def _drive(self, *, adopted, rows) -> str:
        ops = self._ops()
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False), \
                patch.object(type(ops), "_live_rows", return_value=rows), \
                patch.object(type(ops), "_launch_providers", return_value=PROVIDERS), \
                patch.object(
                    type(ops), "_resolve_lane_slots", return_value=(WS, LANE, {})
                ):
            return ops.declare_adopted_lane_lifecycle(self.worktree, adopted=adopted)

    def _owner(self):
        return resolve_lane_owner(WS, ISSUE, home=self.home)

    def test_official_adopt_path_backfills_the_owner_row(self) -> None:
        self._attest_pair()
        self.assertEqual(self._drive(adopted=True, rows=_pair_rows()), ADOPT_DECL_DECLARED)
        owner = self._owner()
        self.assertEqual(owner.status, OWNER_RESOLVED)
        self.assertEqual(owner.lane_id, LANE)

    def test_create_path_adopted_false_is_a_no_op(self) -> None:
        self.assertEqual(
            self._drive(adopted=False, rows=_pair_rows()), ADOPT_DECL_NOT_ADOPTED
        )
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_unattested_inventory_writes_nothing_on_the_official_path(self) -> None:
        # Live pair present but no attestation seeded -> unattested -> zero-write.
        self.assertEqual(self._drive(adopted=True, rows=_pair_rows()), ADOPT_DECL_UNATTESTED)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def _drive_unreadable(self, *, workspace_segment) -> str:
        # R4-F3: the live inventory read RAISES at declaration time (herdr down). The ops
        # adapter must fall back to an ownership read (state DB), never proceed on inference.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
            HerdrSessionStartError,
        )

        ops = self._ops()

        def _boom(*a, **k):
            raise HerdrSessionStartError("herdr down")

        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.home)}, clear=False), \
                patch.object(type(ops), "_live_rows", _boom), \
                patch(
                    "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_"
                    "nested_handoff.application.sublane_actuator_herdr_ops."
                    "herdr_workspace_segment",
                    return_value=workspace_segment,
                ):
            return ops.declare_adopted_lane_lifecycle(self.worktree, adopted=True)

    def test_unreadable_inventory_owner_unbound_is_a_block_token(self) -> None:
        out = self._drive_unreadable(workspace_segment=WS)  # unbound: no prior owner row
        self.assertEqual(out, ADOPT_DECL_UNREADABLE)
        self.assertIn(out, ADOPT_DECL_OWNER_UNBOUND)
        self.assertEqual(self._owner().status, OWNER_ABSENT)

    def test_unreadable_inventory_on_an_already_owned_lane_proceeds(self) -> None:
        # A prior create/adopt already bound the lane: the state DB confirms ownership, so an
        # unreadable inventory does NOT block (the #13809 blocker is only the rowless case).
        LaneDeclarationStore(home=self.home).declare_lane(
            LaneLifecycleKey(WS, LANE),
            decision=DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL),
            issue_id=ISSUE,
        )
        out = self._drive_unreadable(workspace_segment=WS)
        self.assertEqual(out, ADOPT_DECL_ALREADY_OWNED)
        self.assertNotIn(out, ADOPT_DECL_OWNER_UNBOUND)


class PublicContractRegistryTest(unittest.TestCase):
    """R4-F1 (review j#78926): the owner-unbound public vocabulary is self-consistent."""

    def test_reason_adopt_owner_unbound_is_registered_and_exported(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
            sublane_actuation as actuation,
        )

        self.assertIn(actuation.REASON_ADOPT_OWNER_UNBOUND, actuation.BLOCKED_REASONS)
        self.assertIn("REASON_ADOPT_OWNER_UNBOUND", actuation.__all__)

    def test_unreadable_is_both_owner_unbound_and_zero_write(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_UNREADABLE,
            ADOPT_DECL_ZERO_WRITE,
        )

        # UNREADABLE fails closed (blocks dispatch) AND writes no owner row.
        self.assertIn(ADOPT_DECL_UNREADABLE, ADOPT_DECL_OWNER_UNBOUND)
        self.assertIn(ADOPT_DECL_UNREADABLE, ADOPT_DECL_ZERO_WRITE)

    def test_every_blocking_outcome_except_declare_error_is_zero_write(self) -> None:
        # A blocking adopt outcome wrote no owner row — except ``declare_error``, which is a
        # store failure surfaced to the caller, not a clean zero-write refusal.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_adopt_declaration import (  # noqa: E501
            ADOPT_DECL_DECLARE_ERROR,
            ADOPT_DECL_ZERO_WRITE,
        )

        self.assertEqual(
            ADOPT_DECL_OWNER_UNBOUND - {ADOPT_DECL_DECLARE_ERROR},
            (ADOPT_DECL_OWNER_UNBOUND - {ADOPT_DECL_DECLARE_ERROR}) & ADOPT_DECL_ZERO_WRITE,
        )


if __name__ == "__main__":
    unittest.main()
