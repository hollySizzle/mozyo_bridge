"""Redmine #15712 — L2→L1 callback ``target_unavailable`` (receiver generation proof).

Measured live (#15693 j#108152 / j#108159 / j#108167): every ``handoff reply`` to the
idle default-lane coordinator zero-sent as ``target_unavailable`` although role
authority resolved and the live attestation was exactly-one. The join that refused is
the delivery-time launch proof: the coordinator relaunch's startup transaction settled
``rollback_owed`` (the bounded health probe outlived by the Claude boot), and on a
runtime without a conditional-close primitive that debt can never be cleared while the
pane lives — so a live, attested, generation-finalized, receipt-bound pair was
permanently unprovable under the ``completed_success``-only conjunct.

These regressions pin the fix (receipt-proof-gated ``rollback_owed`` acceptance in
``completed_generation_startup_token``) and every refusal boundary it must not widen:
no receipt proof, foreign terminal receipt, missing / foreign attestation event,
rolled-back action, mid-startup phases, closed / foreign participant, pending
generation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
from mozyo_bridge.core.state.herdr_launch_generation import (
    HerdrLaunchGenerationStore,
    verified_generation_token,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import native_name_for
from mozyo_bridge.core.state.startup_execution_events import (
    STAGE_ATTESTATION_WRITE_SUCCEEDED,
    append_execution_event,
)
from mozyo_bridge.core.state.startup_transaction_fence import (
    PHASE_COMPLETED_ROLLED_BACK,
    PHASE_COMPLETED_SUCCESS,
    PHASE_HEALTH_CHECK,
    PHASE_LAUNCHING,
    PHASE_ROLLBACK_OWED,
    PHASE_SUCCESS_OWED,
    Participant,
    StartupTransactionFence,
    StartupUnit,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_generation_binding import (  # noqa: E501
    verified_terminal_generation_token,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_transaction import (  # noqa: E501
    pane_bound_receipt,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    _norm,
    _norm_lane,
)

WS = "wsL1"
ROLE = "claude"
LANE = "default"
LOCATOR = "w:4"
NAME = "coordinator-slot"
TERMINAL_ID = "terminal-L1"


def _tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _receipt(*, name: str = NAME, terminal_id: str = TERMINAL_ID) -> str:
    return pane_bound_receipt(
        target_workspace="w1",
        target_tab="w1:t1",
        native_name=native_name_for(name),
        terminal_id=terminal_id,
    )


def _seed_action(
    home: Path,
    *,
    phase: str = PHASE_ROLLBACK_OWED,
    name: str = NAME,
    locator: str = LOCATOR,
    closed: bool = False,
    receipt: str | None = None,
    attestation_event_participant: str | None = NAME,
) -> str:
    """Reserve the action, record the participant, append (or omit) the wrapper's own
    attestation event, and drive the action to ``phase``. Returns the action token."""
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=WS, lane_id=LANE, providers=(ROLE,)),
        "nonce-15712",
    )
    token = action.action_id
    fence.record_participant(
        token,
        Participant(
            role=ROLE,
            assigned_name=name,
            locator=locator,
            receipt=receipt if receipt is not None else _receipt(name=name),
            closed=closed,
        ),
    )
    if attestation_event_participant is not None:
        assert append_execution_event(
            fence,
            token,
            STAGE_ATTESTATION_WRITE_SUCCEEDED,
            participant=attestation_event_participant,
        )
    fence.set_phase(token, phase)
    return token


def _seed_generation(home: Path, token: str, *, finalize: bool = True) -> None:
    store = HerdrLaunchGenerationStore(home=home)
    store.reserve_pending(
        assigned_name=NAME,
        startup_action_id=token,
        workspace_id=WS,
        role=ROLE,
        lane_id=LANE,
    )
    if finalize:
        store.finalize(
            assigned_name=NAME,
            startup_action_id=token,
            workspace_id=WS,
            role=ROLE,
            lane_id=LANE,
            locator=LOCATOR,
            terminal_id=TERMINAL_ID,
            verdict=VERDICT_PRESENT,
            observed_at="2026-08-18T12:55:42+00:00",
        )


def _delivery_token(home: Path) -> str:
    """The queue-enter delivery join: wrapper with the terminal-bound receipt proof."""
    return verified_terminal_generation_token(
        home,
        assigned_name=NAME,
        workspace_id=WS,
        role=ROLE,
        lane_id=LANE,
        locator=LOCATOR,
        terminal_id=TERMINAL_ID,
    )


def _bare_token(home: Path) -> str:
    """The same authority WITHOUT a receipt proof (recovery-style direct caller)."""
    return verified_generation_token(
        home,
        assigned_name=NAME,
        workspace_id=WS,
        role=ROLE,
        lane_id=LANE,
        locator=LOCATOR,
        live_terminal_id=TERMINAL_ID,
        norm=_norm,
        norm_lane=_norm_lane,
    )


class LivePreservedRollbackOwedDelivery(unittest.TestCase):
    """The measured L2→L1 shape: live + attested + receipt-bound, action rollback_owed."""

    def test_live_receipt_bound_rollback_owed_pair_yields_the_delivery_token(self):
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), token)

    def test_completed_success_acceptance_is_unchanged(self):
        home = _tmp()
        token = _seed_action(home, phase=PHASE_HEALTH_CHECK)
        fence = StartupTransactionFence(home=home)
        fence.set_phase(token, PHASE_SUCCESS_OWED)
        fence.set_phase(token, PHASE_COMPLETED_SUCCESS)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), token)
        self.assertEqual(_bare_token(home), token)


class RollbackOwedRefusalBoundaries(unittest.TestCase):
    """Everything the widened acceptance must NOT admit stays fail-closed."""

    def test_rollback_owed_without_the_receipt_proof_stays_refused(self):
        # A caller that cannot bind the participant receipt to the current terminal
        # (recovery-style direct verified_generation_token) keeps the strict
        # completed_success-only behavior.
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token)
        self.assertEqual(_bare_token(home), "")

    def test_a_receipt_minted_for_a_replacement_terminal_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, receipt=_receipt(terminal_id="terminal-B"))
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_receipt_minted_for_another_slot_name_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, receipt=_receipt(name="other-slot"))
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_missing_attestation_event_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, attestation_event_participant=None)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_foreign_participants_attestation_event_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, attestation_event_participant="other-slot")
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_rolled_back_action_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, phase=PHASE_COMPLETED_ROLLED_BACK)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_an_open_launch_set_stays_refused(self):
        # `success_owed` and `health_check` left this pin in Redmine #15748 (verdict
        # j#108925): both are written only once `settle` has been entered, so the
        # action's launch set is closed, and both are now admitted under the SAME
        # receipt-proof gate. `launching` — where `record_participant` can still add a
        # role — stays refused. The #15748 regression file owns the new pins; the
        # `rollback_owed` acceptance and every boundary below are byte-unchanged.
        home = _tmp()
        token = _seed_action(home, phase=PHASE_LAUNCHING)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_closed_participant_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, closed=True)
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_foreign_locator_participant_stays_refused(self):
        home = _tmp()
        token = _seed_action(home, locator="w:OTHER")
        _seed_generation(home, token)
        self.assertEqual(_delivery_token(home), "")

    def test_a_pending_generation_stays_refused(self):
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token, finalize=False)
        self.assertEqual(_delivery_token(home), "")

    def test_a_replacement_live_terminal_stays_refused(self):
        # The pane at the locator was replaced after the launch: the live terminal no
        # longer equals the generation row's terminal, so no token regardless of phase.
        home = _tmp()
        token = _seed_action(home)
        _seed_generation(home, token)
        self.assertEqual(
            verified_terminal_generation_token(
                home,
                assigned_name=NAME,
                workspace_id=WS,
                role=ROLE,
                lane_id=LANE,
                locator=LOCATOR,
                terminal_id="terminal-B",
            ),
            "",
        )


# ---------------------------------------------------------------------------------------------
# Composition regressions (review j#108298 finding_callbackregression): the rollback_owed
# durable fixture wired through the ACTUAL binding join (no mocked generation verifier) and
# through the standard callback transport shape (`--to <resolved coordinator provider>
# --target coordinator --mode standard`, the exact argv family `callback_send_port` pins).
# The launch-generation proof conjunct is consumed by the queue-enter binding (spec
# `herdr-native-identity.md`: the terminal-bound attested v2 row is required for the
# queue-enter receipt), so the proof refusal pins ride the queue-enter transport; the
# standard-rail legs pin the callback rail's idle `sent` and busy `precondition_not_idle`
# (zero injection) for the same live coordinator fixture.
# ---------------------------------------------------------------------------------------------

import contextlib
import io
import os
import stat
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    record_identity_attestation,
)
from mozyo_bridge.core.state.workspace_registry import (  # noqa: E402
    read_anchor,
    register_workspace,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)
from tests.integration.e_110_execution_platform.f_130_handoff_routing.test_herdr_transport_wiring import (  # noqa: E402,E501
    _FakeHerdr,
    _outcome_from,
)
from tests.support.redmine_anchor_authority import (  # noqa: E402
    matching_redmine_anchor_source_patch,
)

COORD_LOCATOR = "wT:pT"
COORD_TERMINAL = "terminal-A"


class _CoordinatorFakeHerdr(_FakeHerdr):
    """The wiring fake plus the coordinator route's tmux-availability probe.

    The `--target coordinator` resolution first asks the host whether tmux exists
    (`sh -c command -v tmux`); this pure-herdr fixture answers "no tmux" so the
    herdr-native coordinator route is taken, exactly like the live pure-herdr host.
    """

    def run(self, argv, capture_output=None, text=None, timeout=None, **kw):
        if list(argv[:2]) == ["sh", "-c"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return super().run(
            argv, capture_output=capture_output, text=text, timeout=timeout, **kw
        )


def _seed_coordinator_proof(
    home: Path,
    workspace_id: str,
    name: str,
    *,
    phase: str = PHASE_ROLLBACK_OWED,
    with_event: bool = True,
    finalize: bool = True,
    receipt_terminal: str = COORD_TERMINAL,
) -> str:
    """Seed the REAL durable stores with the measured L1 coordinator shape.

    An attested identity record, a launch-generation row, and a startup transaction
    (participant + pane-bound receipt + the wrapper's own attestation event) whose
    phase is ``rollback_owed`` — the exact live join j#108257 confirmed for `w1Q:p4`.
    """
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(
        StartupUnit(workspace_id=workspace_id, lane_id="default", providers=("claude",)),
        "nonce-15712-l1",
    )
    fence.record_participant(
        action.action_id,
        Participant(
            role="claude",
            assigned_name=name,
            locator=COORD_LOCATOR,
            receipt=pane_bound_receipt(
                target_workspace="w1",
                target_tab="w1:t1",
                native_name=native_name_for(name),
                terminal_id=receipt_terminal,
            ),
        ),
    )
    if with_event:
        assert append_execution_event(
            fence,
            action.action_id,
            STAGE_ATTESTATION_WRITE_SUCCEEDED,
            participant=name,
        )
    fence.set_phase(action.action_id, phase)
    store = HerdrLaunchGenerationStore(home=home)
    store.reserve_pending(
        assigned_name=name,
        startup_action_id=action.action_id,
        workspace_id=workspace_id,
        role="claude",
        lane_id="default",
    )
    if finalize:
        store.finalize(
            assigned_name=name,
            startup_action_id=action.action_id,
            workspace_id=workspace_id,
            role="claude",
            lane_id="default",
            locator=COORD_LOCATOR,
            terminal_id=COORD_TERMINAL,
            verdict=VERDICT_PRESENT,
            observed_at="2026-08-18T12:55:42+00:00",
        )
    record_identity_attestation(
        IdentityAttestationRecord(
            assigned_name=name,
            workspace_id=workspace_id,
            role="claude",
            lane_id="default",
            locator=COORD_LOCATOR,
            verdict=VERDICT_PRESENT,
            observed_at="2026-08-18T12:55:42+00:00",
            terminal_id=COORD_TERMINAL,
        ),
        home=home,
    )
    return action.action_id


def _run_coordinator_callback(
    *,
    mode: str,
    get_states,
    wait_results,
    seed=None,
    coordinator_status: str = "idle",
    enter_clears_composer: bool = False,
):
    """Drive the REAL `orchestrate_handoff` for the coordinator callback argv shape.

    Modeled on the #13261 pure-herdr wiring harness, WITHOUT patching
    ``observe_queue_enter_gateway_binding`` / ``verified_terminal_generation_token``:
    the queue-enter binding runs its actual durable-store join against the seeded
    fixture home. The argv mirrors ``callback_send_port`` (`--to` = the provider the
    rebound coordinator role resolves to, `--target coordinator`, `--kind reply`);
    ``--target-repo auto`` resolution is #15707's pinned surface and is out of this
    composition's scope.
    """
    from mozyo_bridge.application import commands  # noqa: F401 (import side effects)
    from mozyo_bridge.application.cli import build_parser

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        home = Path(tmp) / "home"
        home.mkdir()
        (repo / ".mozyo-bridge").mkdir()
        (repo / ".mozyo-bridge" / "config.yaml").write_text(
            "version: 2\n"
            "agents:\n"
            "  profiles:\n"
            "    implementation:\n"
            "      provider: claude\n"
            "    coordination:\n"
            "      provider: codex\n"
            "  roles:\n"
            "    coordinator: implementation\n"
            "terminal_transport:\n"
            "  backend: herdr\n",
            encoding="utf-8",
        )
        register_workspace(repo, home=home)
        workspace_id = read_anchor(repo)["workspace_id"]
        coordinator_name = encode_assigned_name(workspace_id, "claude", "default")
        seeded_action = ""
        if seed is not None:
            seeded_action = seed(home, workspace_id, coordinator_name)
        rows = [
            {
                "name": encode_assigned_name(workspace_id, "codex", "lane-1"),
                "pane_id": "wS:pS",
                "terminal_id": "terminal-S",
                "revision": 7,
                "agent": "codex",
                "agent_status": "idle",
            },
            {
                "name": coordinator_name,
                "pane_id": COORD_LOCATOR,
                "terminal_id": COORD_TERMINAL,
                "revision": 7,
                "agent": "claude",
                "agent_status": coordinator_status,
            },
        ]
        herdr = _CoordinatorFakeHerdr(
            rows,
            get_states=get_states,
            wait_results=wait_results,
            enter_clears_composer=enter_clears_composer,
        )
        herdr_bin = repo / "fake-herdr"
        herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        herdr_bin.chmod(
            herdr_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
        argv = [
            "handoff", "send", "--to", "claude", "--target", "coordinator",
            "--source", "redmine", "--issue", "15712", "--journal", "108292",
            "--kind", "reply", "--mode", mode,
            "--landing-timeout", "0.05", "--submit-delay", "0",
        ]
        args = build_parser().parse_args(argv)
        args.repo = str(repo)
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["MOZYO_HERDR_BINARY"] = str(herdr_bin)
        env["MOZYO_REPO"] = str(repo)
        env["MOZYO_BRIDGE_HOME"] = str(home)
        env["MOZYO_WORKSPACE_ID"] = workspace_id
        env["MOZYO_AGENT_ROLE"] = "codex"
        env["MOZYO_LANE_ID"] = "lane-1"
        with contextlib.ExitStack() as stack:
            stack.enter_context(matching_redmine_anchor_source_patch())
            stack.enter_context(patch("subprocess.run", herdr.run))
            stack.enter_context(patch("subprocess.Popen", herdr.popen))
            stack.enter_context(
                patch("mozyo_bridge.application.commands.time.sleep")
            )
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            err = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            try:
                result = args.func(args)
            except BaseException as exc:  # noqa: BLE001 - blocked outcomes die
                result = exc
        return result, herdr, seeded_action, out.getvalue(), err.getvalue()


def _send_texts(herdr) -> list:
    return [op for op in herdr.sends if op[0] == "send_text"]


def _injections(herdr) -> list:
    return [op for op in herdr.sends if op[0] in ("send_text", "send_keys")]


class QueueEnterRealBindingComposition(unittest.TestCase):
    """The queue-enter transport driven through the ACTUAL durable-store binding join."""

    def test_rollback_owed_fixture_delivers_with_the_real_binding(self):
        result, herdr, action_id, out, err = _run_coordinator_callback(
            mode="queue-enter",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=_seed_coordinator_proof,
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        self.assertEqual(len(_send_texts(herdr)), 1, msg=herdr.sends)
        binding = outcome["queue_enter_turn_start_observation"]["gateway_binding"]
        # The binding was produced by the real join and carries the seeded
        # rollback_owed action token — not a synthetic fixture value.
        self.assertEqual(binding["startup_action_id"], action_id, msg=out)
        self.assertEqual(binding["locator"], COORD_LOCATOR, msg=out)

    def _assert_zero_typed_target_unavailable(self, seed):
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="queue-enter",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=seed,
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "target_unavailable", msg=out)
        self.assertFalse(_injections(herdr), msg=herdr.sends)

    def test_a_missing_generation_row_zero_sends_before_typing(self):
        self._assert_zero_typed_target_unavailable(
            lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, finalize=False
            )
        )

    def test_a_rolled_back_action_zero_sends_before_typing(self):
        self._assert_zero_typed_target_unavailable(
            lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, phase=PHASE_COMPLETED_ROLLED_BACK
            )
        )

    def test_a_foreign_terminal_receipt_zero_sends_before_typing(self):
        self._assert_zero_typed_target_unavailable(
            lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, receipt_terminal="terminal-B"
            )
        )

    def test_a_missing_attestation_event_zero_sends_before_typing(self):
        self._assert_zero_typed_target_unavailable(
            lambda home, ws, name: _seed_coordinator_proof(
                home, ws, name, with_event=False
            )
        )

    def test_an_unproven_target_with_no_stores_zero_sends_before_typing(self):
        self._assert_zero_typed_target_unavailable(None)


class StandardCallbackRailComposition(unittest.TestCase):
    """The production callback rail shape (`--mode standard --target coordinator`)."""

    def test_idle_coordinator_callback_is_sent_with_one_body_and_enter(self):
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="standard",
            get_states=["idle"],
            wait_results=[(0, "")],
            seed=_seed_coordinator_proof,
        )
        self.assertEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "sent", msg=out)
        self.assertEqual(outcome.get("reason"), "ok", msg=out)
        self.assertEqual(len(_send_texts(herdr)), 1, msg=herdr.sends)
        enters = [op for op in herdr.sends if op[0] == "send_keys"]
        self.assertEqual(len(enters), 1, msg=herdr.sends)
        self.assertTrue(
            [op for op in herdr.sends if op[0] == "wait"], msg=herdr.sends
        )
        # Every injection reached the resolved coordinator locator, never the sender.
        self.assertEqual(
            {op[1] for op in _injections(herdr)}, {COORD_LOCATOR}, msg=herdr.sends
        )

    def test_busy_coordinator_stays_precondition_not_idle_with_zero_injection(self):
        result, herdr, _action, out, err = _run_coordinator_callback(
            mode="standard",
            get_states=["working"],
            wait_results=[(0, "")],
            seed=_seed_coordinator_proof,
        )
        self.assertNotEqual(result, 0, msg=f"out={out}\nerr={err}")
        outcome = _outcome_from(out)
        self.assertEqual(outcome.get("status"), "blocked", msg=out)
        self.assertEqual(outcome.get("reason"), "precondition_not_idle", msg=out)
        self.assertFalse(
            [op for op in herdr.sends if op[0] in ("send_text", "send_keys", "wait")],
            msg=herdr.sends,
        )


if __name__ == "__main__":
    unittest.main()
