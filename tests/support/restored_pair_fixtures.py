"""Real-store fixtures for the restored-pair rails' "server swap" shape (Redmine #15811).

Shared by `tests/regressions/test_issue_15811_cold_pair_recovery.py` and
`tests/scenarios/test_issue_15811_cold_pair_adopt_acceptance.py`. Two test modules use it,
so `tests-placement-discovery-policy.md` `## 配置決定木` branch 1 puts it here rather than
inside either test module (review j#109452 `finding_sharedtestsupport`: a scenario importing
a regression module is a reverse dependency between buckets).

What it builds, on REAL stores under a caller-supplied temp home:

- an ACTIVE issue lifecycle row whose `declared_slots` snapshot is EMPTY by default — the
  create-path shape #15811 exists for, where `read_declared_pin_pair` reports the typed
  `declared_pins_absent` and no pin was ever declared;
- a completed startup transaction whose participants carry `pane_bound_v2` receipts;
- launch-generation rows and startup self-attestations, all recorded at the PRE-restore
  locator / terminal.

The caller supplies the LIVE inventory (`preserved_pane_rows` / `moved_pane_rows`), which
carries the same server-owned `mzb1` stamps at NEW terminal ids — the herdr server
generation change. Nothing here touches a real Herdr, a real pane, or the operator home:
only the host probes are faked (`FakeHostProbes`), every store join is real.
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.herdr_launch_generation import HerdrLaunchGenerationStore
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.lane_declaration import LaneDeclarationStore
from mozyo_bridge.core.state.lane_lifecycle import (
    BINDING_KIND_ISSUE,
    DecisionPointer,
    LaneLifecycleKey,
)
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_SUCCESS,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

ISSUE = "15811"
JOURNAL = "109241"
WS = "ws_main"
LANE = "issue_15811_lane"
TOKEN = "wt_issue_15811_token"
GW_PROVIDER = "codex"
WK_PROVIDER = "claude"
GW_NAME = encode_assigned_name(WS, GW_PROVIDER, LANE)
WK_NAME = encode_assigned_name(WS, WK_PROVIDER, LANE)
GW_OLD, WK_OLD = "w1:%1", "w1:%2"
GW_NEW, WK_NEW = "w9:%11", "w9:%12"
GW_TERM_OLD, WK_TERM_OLD = "term-gw-1", "term-wk-1"
GW_TERM_NEW, WK_TERM_NEW = "term-gw-2", "term-wk-2"
KEY = LaneLifecycleKey(WS, LANE)
DECISION = DecisionPointer(source="redmine", issue_id=ISSUE, journal_id=JOURNAL)
OBSERVED_AT = "2026-08-20T09:31:00+00:00"

#: The repo root the faked host probes report. A host-local path that must never surface in
#: operator-facing output.
REPO_ROOT = Path("/lane/issue_15811")


def inventory_row(
    name: str,
    locator: str,
    terminal: str,
    provider: str,
    *,
    surfaced_provider: str | None = None,
    detected_agent: str | None = None,
) -> dict:
    """One raw ``agent list`` row.

    The surfaced provider / detected agent default to the slot's provider and are
    overridable so a squatting (foreign provider on the expected name) or shell-residue
    (both fields blank) shape can be built.
    """
    return {
        "name": name,
        "pane_id": locator,
        "terminal_id": terminal,
        "provider": provider if surfaced_provider is None else surfaced_provider,
        "agent": provider if detected_agent is None else detected_agent,
    }


def preserved_pane_rows() -> list[dict]:
    """The measured restore shape: same stamps, same pane ids, NEW terminals."""
    return [
        inventory_row(GW_NAME, GW_OLD, GW_TERM_NEW, GW_PROVIDER),
        inventory_row(WK_NAME, WK_OLD, WK_TERM_NEW, WK_PROVIDER),
    ]


def moved_pane_rows() -> list[dict]:
    """The restore also moved the panes: same stamps, NEW locators + terminals."""
    return [
        inventory_row(GW_NAME, GW_NEW, GW_TERM_NEW, GW_PROVIDER),
        inventory_row(WK_NAME, WK_NEW, WK_TERM_NEW, WK_PROVIDER),
    ]


class FakeHostProbes:
    """Host-probe seams faked; every durable-store join stays real against the temp home.

    Mixed in BEFORE the live ops class so it overrides
    :class:`...restored_pair_store_seams.RestoredPairStoreSeams`. Subclasses set
    ``test_workspace`` / ``test_token`` / ``test_branch`` / ``test_providers`` /
    ``test_rows``.
    """

    def _resolve_root(self):
        return self.repo_root

    def _workspace_id(self, root):
        return self.test_workspace

    def _worktree_identity(self, root, lane):
        return self.test_token

    def _worktree_readable(self, root):
        return True

    def _branch(self, root):
        return self.test_branch

    def _providers(self, root):
        return self.test_providers

    def _rows(self):
        return list(self.test_rows)


#: The launch-time truth, slot by slot: (provider, assigned name, locator, terminal).
LAUNCH_TIME_SLOTS = (
    (GW_PROVIDER, GW_NAME, GW_OLD, GW_TERM_OLD),
    (WK_PROVIDER, WK_NAME, WK_OLD, WK_TERM_OLD),
)


def declare_lane_row(home: Path, *, slots=()) -> None:
    """Declare the ACTIVE issue lifecycle row. ``slots`` defaults to the EMPTY snapshot."""
    outcome = LaneDeclarationStore(home=home).declare_lane(
        KEY,
        decision=DECISION,
        binding_kind=BINDING_KIND_ISSUE,
        issue_id=ISSUE,
        declared_slots=slots,
        worktree_identity=TOKEN,
    )
    assert outcome.applied, outcome.reason


def seed_startup_action(home: Path, *, phase: str = PHASE_COMPLETED_SUCCESS) -> str:
    """One session-start action that launched BOTH slots at the launch-time values."""
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=WS, lane_id=LANE, providers=(GW_PROVIDER, WK_PROVIDER)),
        "nonce-15811",
    )
    for provider, name, locator, terminal in LAUNCH_TIME_SLOTS:
        fence.record_participant(
            action.action_id,
            Participant(
                role=provider,
                assigned_name=name,
                locator=locator,
                receipt=pane_bound_receipt(
                    target_workspace="w1",
                    target_tab="w1:t1",
                    native_name=native_name_for(name),
                    terminal_id=terminal,
                ),
            ),
        )
        assert append_execution_event(
            fence, action.action_id, STAGE_ATTESTATION_WRITE_SUCCEEDED, participant=name
        )
    fence.set_phase(action.action_id, phase)
    return action.action_id


def seed_generation_row(
    home: Path,
    token: str,
    *,
    name: str,
    provider: str,
    locator: str,
    terminal: str,
    lane: str = LANE,
    finalize: bool = True,
) -> None:
    """Reserve (and by default finalize) one slot's launch-generation row.

    ``finalize=False`` leaves the row ``pending`` — the "no usable ATTESTED row" shape.
    ``lane`` is overridable so a row FOREIGN to the slot can be built.
    """
    store = HerdrLaunchGenerationStore(home=home)
    store.reserve_pending(
        assigned_name=name,
        startup_action_id=token,
        workspace_id=WS,
        role=provider,
        lane_id=lane,
    )
    if not finalize:
        return
    store.finalize(
        assigned_name=name,
        startup_action_id=token,
        workspace_id=WS,
        role=provider,
        lane_id=lane,
        locator=locator,
        terminal_id=terminal,
        verdict=VERDICT_PRESENT,
        observed_at=OBSERVED_AT,
    )


def upsert_attestation(
    home: Path,
    name: str,
    provider: str,
    locator: str,
    terminal: str,
    *,
    workspace: str = WS,
    verdict: str = VERDICT_PRESENT,
) -> None:
    """Record one slot's startup self-attestation.

    ``workspace`` / ``verdict`` are overridable so a FOREIGN record or a non-``present``
    boot verdict — neither of which is the restore signature — can be built.
    """
    HerdrIdentityAttestationStore(home=home).upsert(
        IdentityAttestationRecord(
            assigned_name=name,
            workspace_id=workspace,
            role=provider,
            lane_id=LANE,
            locator=locator,
            verdict=verdict,
            observed_at=OBSERVED_AT,
            terminal_id=terminal,
        )
    )


def seed_restored_lane_fixture(home: Path, *, slots=()) -> str:
    """Build the whole #15811 shape on REAL stores under ``home``; return the action id.

    ``slots`` overrides the lifecycle row's declared-pin snapshot; the default EMPTY tuple
    is the create-path shape (`declared_pins_absent`). Everything else — fence participants,
    launch-generation rows, self-attestations — is recorded at the PRE-restore locator /
    terminal, so the caller's live inventory supplies the post-restore side.
    """
    declare_lane_row(home, slots=slots)
    token = seed_startup_action(home)
    for provider, name, locator, terminal in LAUNCH_TIME_SLOTS:
        seed_generation_row(
            home, token, name=name, provider=provider, locator=locator,
            terminal=terminal,
        )
        upsert_attestation(home, name, provider, locator, terminal)
    return token


__all__ = (
    "DECISION",
    "FakeHostProbes",
    "LAUNCH_TIME_SLOTS",
    "declare_lane_row",
    "seed_generation_row",
    "seed_startup_action",
    "upsert_attestation",
    "GW_NAME",
    "GW_NEW",
    "GW_OLD",
    "GW_PROVIDER",
    "GW_TERM_NEW",
    "GW_TERM_OLD",
    "ISSUE",
    "JOURNAL",
    "KEY",
    "LANE",
    "OBSERVED_AT",
    "REPO_ROOT",
    "TOKEN",
    "WK_NAME",
    "WK_NEW",
    "WK_OLD",
    "WK_PROVIDER",
    "WK_TERM_NEW",
    "WK_TERM_OLD",
    "WS",
    "inventory_row",
    "moved_pane_rows",
    "preserved_pane_rows",
    "seed_restored_lane_fixture",
)
