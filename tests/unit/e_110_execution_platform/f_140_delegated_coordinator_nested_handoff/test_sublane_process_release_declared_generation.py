"""Pure-function tests for the project-gateway action-time exact-generation fence.

Redmine #13811 R1 F1 (review j#79318): the two pure helpers a project-gateway lifecycle
action uses at action time to prove the CURRENT live inventory is exactly the lane's
declared, attested generation before it releases anything —

- :func:`declared_generation_exactly_live` — the ``(role, provider, assigned_name,
  locator)`` identity match (F1 item 1: ``pin.provider`` is a real match axis, never
  collapsed to ``role``; item 2: ``runtime_revision`` is a discriminant only when BOTH
  sides observe it, per ``managed-state-model.md`` ``### ...world state`` / #13846 — the
  strict "declared-non-empty vs live-unobserved fails closed" reading is superseded by the
  documented action-time contract, which the #13846 false-conflict fix hardened).
- :func:`declared_generation_attested` — the per-live-slot locator-bound startup
  attestation re-read (F1 item 4).

The design authority is #13780 j#78386 §1-2 (exact pair pins; provider-bound identity;
runtime_revision as evidence; startup self-attestation re-read; newer generation / stale
approval / unattested -> zero-actuation).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    VERDICT_CONFLICT,
    VERDICT_MISSING,
    VERDICT_PRESENT,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.lane_lifecycle_model import ProcessGenerationPin
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    declared_generation_attested,
    declared_generation_exactly_live,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

WS = "wProj"
LANE = "pgwv1_scope_abc"
GW_NAME = encode_assigned_name(WS, "codex", LANE)
WK_NAME = encode_assigned_name(WS, "claude", LANE)
GW_LOC = "wProj:p2"
WK_LOC = "wProj:p3"


def _pin(role: str, provider: str, name: str, locator: str, rev: str = "") -> ProcessGenerationPin:
    return ProcessGenerationPin(
        role=role, provider=provider, assigned_name=name, locator=locator, runtime_revision=rev
    )


def _declared(rev_gw: str = "", rev_wk: str = "") -> list[ProcessGenerationPin]:
    # CANONICAL slot-role vocabulary (Redmine #13920) — the shape the CURRENT writer
    # (`sublane_adopt_declaration`) emits: role names the slot (`gateway`/`worker`), provider
    # is a separate field. The live pair's decoded role is the PROVIDER token (`codex` /
    # `claude`), resolved to the same canonical slot for the match. Using canonical fixtures
    # (not legacy `codex`/`claude` roles) is what surfaces the R2 F1 regression.
    return [
        _pin("gateway", "codex", GW_NAME, GW_LOC, rev_gw),
        _pin("worker", "claude", WK_NAME, WK_LOC, rev_wk),
    ]


def _declared_legacy() -> list[ProcessGenerationPin]:
    # The pre-#13920 spelling the #13809 adopt writer shipped — read-compatible only.
    return [
        _pin("codex", "codex", GW_NAME, GW_LOC),
        _pin("claude", "claude", WK_NAME, WK_LOC),
    ]


def _row(name: str, locator: str, **extra) -> dict:
    row = {"name": name, "pane_id": locator}
    row.update(extra)
    return row


def _live(**extra_gw) -> list[dict]:
    return [_row(GW_NAME, GW_LOC, **extra_gw), _row(WK_NAME, WK_LOC)]


def _match(declared, rows) -> bool:
    return declared_generation_exactly_live(
        declared, rows, workspace_id=WS, lane_id=LANE
    )


class DeclaredGenerationExactlyLiveTest(unittest.TestCase):
    # --- positive ---------------------------------------------------------------
    def test_canonical_declaration_matches_live_pair(self) -> None:
        # Redmine #13811 R2 F1 regression: the CURRENT writer emits canonical slot roles
        # (`gateway`/`worker`); the live pair decodes to provider roles (`codex`/`claude`).
        # Both resolve to the same canonical slot, so a healthy canonical declaration matches
        # — the old raw-role matcher forced this to False (healthy lane could not hibernate).
        self.assertTrue(_match(_declared(), _live()))

    def test_legacy_declaration_read_compatible_matches(self) -> None:
        # A pre-#13920 legacy `codex`/`claude` declaration still resolves to the same slots.
        self.assertTrue(_match(_declared_legacy(), _live()))

    def test_declared_slot_that_is_gone_does_not_block(self) -> None:
        # A declared slot with no live row is a dead process, not a mismatch (0/1/2-slot).
        self.assertTrue(_match(_declared(), [_row(GW_NAME, GW_LOC)]))

    def test_empty_inventory_matches(self) -> None:
        self.assertTrue(_match(_declared(), []))

    # --- F1 item 1: provider is a real match axis -------------------------------
    def test_provider_rebind_fails_closed(self) -> None:
        # Declared gateway pin's provider is `foreign`; live surfaces the normal codex.
        # The old matcher collapsed to (role, assigned_name, locator) and matched — the bug.
        declared = [
            _pin("gateway", "foreign", GW_NAME, GW_LOC),
            _pin("worker", "claude", WK_NAME, WK_LOC),
        ]
        self.assertFalse(_match(declared, _live()))

    def test_live_row_provider_field_disagrees_fails_closed(self) -> None:
        # The live row explicitly surfaces a different provider than declared.
        self.assertFalse(_match(_declared(), _live(provider="rebound")))

    def test_live_row_agent_field_disagrees_fails_closed(self) -> None:
        # The detected-agent field also carries the provider on a live pane (#13846).
        self.assertFalse(_match(_declared(), _live(agent="rebound")))

    def test_provider_match_via_explicit_row_field(self) -> None:
        self.assertTrue(_match(_declared(), _live(provider="codex", agent="codex")))

    # --- F1 item 2: runtime_revision is a both-observed-only discriminant --------
    def test_declared_revision_live_unobserved_does_not_block(self) -> None:
        # 正本 (managed-state-model.md action-time match / #13846): a declared non-empty
        # revision with an unobserved (empty) live revision is NOT a mismatch. A strict
        # fail-closed here would re-introduce the #13846 false conflict.
        self.assertTrue(_match(_declared(rev_gw="runtime-v2"), _live()))

    def test_both_observed_revision_mismatch_fails_closed(self) -> None:
        # When a richer live surface DOES carry a runtime revision and it differs, that is a
        # re-launched newer generation — fail closed.
        self.assertFalse(
            _match(_declared(rev_gw="runtime-v2"), _live(runtime_revision="runtime-v3"))
        )

    def test_both_observed_revision_match_holds(self) -> None:
        self.assertTrue(
            _match(_declared(rev_gw="runtime-v2"), _live(runtime_revision="runtime-v2"))
        )

    # --- recycled / renamed / undeclared / ambiguous ----------------------------
    def test_recycled_locator_fails_closed(self) -> None:
        rows = [_row(GW_NAME, "wProj:p99"), _row(WK_NAME, WK_LOC)]
        self.assertFalse(_match(_declared(), rows))

    def test_foreign_lane_live_row_is_filtered_not_a_false_match(self) -> None:
        # A live row for a DIFFERENT lane is not part of this unit — it is filtered, never
        # mistaken for this lane's codex slot. Only the matching worker remains live and its
        # declared codex peer is simply gone, so the fence passes (no false mismatch, no
        # false close of the foreign row).
        other_lane = encode_assigned_name(WS, "codex", "other_lane")
        rows = [_row(other_lane, GW_LOC), _row(WK_NAME, WK_LOC)]
        self.assertTrue(_match(_declared(), rows))

    def test_incomplete_declaration_fails_closed(self) -> None:
        # Only the worker slot is declared (a half pair) — resolve_declared_pin_pair returns
        # INCOMPLETE, so the declaration is not an unambiguous authority and fails closed.
        declared = [_pin("worker", "claude", WK_NAME, WK_LOC)]
        self.assertFalse(_match(declared, _live()))

    def test_ambiguous_slot_two_locators_fails_closed(self) -> None:
        # Same assigned name at two locators -> raw name multiplicity (also ambiguous slot).
        rows = [_row(GW_NAME, GW_LOC), _row(GW_NAME, "wProj:p88"), _row(WK_NAME, WK_LOC)]
        self.assertFalse(_match(_declared(), rows))

    def test_mixed_vocabulary_declaration_fails_closed(self) -> None:
        # One canonical (`gateway`) + one legacy (`claude`) pin — two disagreeing writers
        # touched the row; its provenance is not established (#13920 MIXED) -> fail closed.
        declared = [
            _pin("gateway", "codex", GW_NAME, GW_LOC),
            _pin("claude", "claude", WK_NAME, WK_LOC),
        ]
        self.assertFalse(_match(declared, _live()))

    def test_duplicate_slot_declaration_fails_closed(self) -> None:
        # Two declared pins that resolve to the SAME canonical slot (`gateway` + legacy
        # `codex`) — the model's stable_identity dedupe cannot see it (#13920 DUPLICATE).
        declared = [
            _pin("gateway", "codex", GW_NAME, GW_LOC),
            _pin("codex", "codex", GW_NAME, GW_LOC),
        ]
        self.assertFalse(_match(declared, [_row(GW_NAME, GW_LOC)]))

    # --- F3: raw duplicate inventory (name-uniqueness violation) -----------------
    def test_raw_exact_duplicate_row_fails_closed(self) -> None:
        # The SAME assigned name + locator appears twice. A set would collapse it to one; the
        # raw name-count catches it as a herdr name-uniqueness violation (Redmine #13811 R2 F3).
        rows = [_row(GW_NAME, GW_LOC), _row(GW_NAME, GW_LOC), _row(WK_NAME, WK_LOC)]
        self.assertFalse(_match(_declared(), rows))


class _FakeAttestReader:
    def __init__(self, records, *, raises_for=()):
        self._records = dict(records)
        self._raises_for = set(raises_for)

    def __call__(self, assigned_name):
        if assigned_name in self._raises_for:
            raise OSError("attestation store unreadable")
        return self._records.get(assigned_name)


def _att(name: str, role: str, locator: str, *, verdict=VERDICT_PRESENT, lane=LANE, ws=WS):
    return IdentityAttestationRecord(
        assigned_name=name,
        workspace_id=ws,
        role=role,
        lane_id=lane,
        locator=locator,
        verdict=verdict,
        observed_at="2026-07-20T00:00:00Z",
    )


class DeclaredGenerationAttestedTest(unittest.TestCase):
    def _attested(self, rows, reader) -> bool:
        return declared_generation_attested(rows, WS, LANE, reader)

    def test_all_live_slots_attested(self) -> None:
        reader = _FakeAttestReader(
            {
                GW_NAME: _att(GW_NAME, "codex", GW_LOC),
                WK_NAME: _att(WK_NAME, "claude", WK_LOC),
            }
        )
        self.assertTrue(self._attested(_live(), reader))

    def test_empty_inventory_is_vacuously_attested(self) -> None:
        self.assertTrue(self._attested([], _FakeAttestReader({})))

    def test_missing_attestation_fails_closed(self) -> None:
        reader = _FakeAttestReader({WK_NAME: _att(WK_NAME, "claude", WK_LOC)})
        self.assertFalse(self._attested(_live(), reader))

    def test_stale_locator_drift_fails_closed(self) -> None:
        reader = _FakeAttestReader(
            {
                GW_NAME: _att(GW_NAME, "codex", "wProj:pOLD"),  # locator drifted
                WK_NAME: _att(WK_NAME, "claude", WK_LOC),
            }
        )
        self.assertFalse(self._attested(_live(), reader))

    def test_conflict_verdict_fails_closed(self) -> None:
        reader = _FakeAttestReader(
            {
                GW_NAME: _att(GW_NAME, "codex", GW_LOC, verdict=VERDICT_CONFLICT),
                WK_NAME: _att(WK_NAME, "claude", WK_LOC),
            }
        )
        self.assertFalse(self._attested(_live(), reader))

    def test_missing_verdict_fails_closed(self) -> None:
        reader = _FakeAttestReader(
            {
                GW_NAME: _att(GW_NAME, "codex", GW_LOC, verdict=VERDICT_MISSING),
                WK_NAME: _att(WK_NAME, "claude", WK_LOC),
            }
        )
        self.assertFalse(self._attested(_live(), reader))

    def test_foreign_record_fails_closed(self) -> None:
        reader = _FakeAttestReader(
            {
                GW_NAME: _att(GW_NAME, "codex", GW_LOC, lane="foreign_lane"),
                WK_NAME: _att(WK_NAME, "claude", WK_LOC),
            }
        )
        self.assertFalse(self._attested(_live(), reader))

    def test_unreadable_attestation_fails_closed(self) -> None:
        reader = _FakeAttestReader(
            {WK_NAME: _att(WK_NAME, "claude", WK_LOC)}, raises_for=(GW_NAME,)
        )
        self.assertFalse(self._attested(_live(), reader))


if __name__ == "__main__":
    unittest.main()
