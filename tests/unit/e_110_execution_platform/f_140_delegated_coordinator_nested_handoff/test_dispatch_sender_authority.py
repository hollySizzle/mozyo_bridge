"""Redmine #15706 — the delegated-gateway sender-authority decision (pure branches).

`evaluate_dispatch_sender_authority` extends the #13613 dispatch-sender contract with
exactly one admission: a launch-time attested delegated_coordinator lane's gateway slot
creating a child implementation lane under itself. These tests pin the DECISION —
every branch typed, fail-closed, and byte-invariant on every pre-#15706 outcome —
through the function's injection seams (no filesystem, no store, no subprocess: the
placement policy's unit bucket). The live compositions (real lifecycle / attestation
stores, real inventory) are pinned in
tests/integration/.../test_dispatch_sender_authority_live.py, and the fixed #15703
j#107980 symptom in tests/regressions/test_issue_15706_l2_child_create_authority.py.

Also pinned here, pure over fake rows: the `resolve_proxy_target` lane generalization
(the coordinator proxy rail's exactly-one live-attested policy resolving a SUBLANE
slot), and the #13305 tier-1 explicit-lane derivation the child gateway's callback to
its parent lane rides (design constraint 3).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
    LANE_KIND_IMPLEMENTATION,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E402,E501
    resolve_proxy_target,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_preflight import (  # noqa: E402,E501
    SENDER_GATEWAY_LIVE_AMBIGUOUS,
    SENDER_GATEWAY_LIVE_MISSING,
    SENDER_GATEWAY_UNATTESTED,
    SENDER_KIND_DEFAULT_COORDINATOR,
    SENDER_KIND_DELEGATED_GATEWAY,
    SENDER_LANE_LIFECYCLE_UNREADABLE,
    SENDER_LANE_NOT_DELEGATED_COORDINATOR,
    SENDER_LANE_UNESTABLISHED,
    SENDER_PROVIDER_NOT_GATEWAY,
    evaluate_dispatch_sender,
    evaluate_dispatch_sender_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E402,E501
    ACTUATE_BLOCKED,
    DELEGATED_SENDER_NEXT_ACTIONS,
    REASON_MISSING_IDENTITY,
    SublaneActuationOutcome,
    render_actuation_journal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.coordinator_proxy import (  # noqa: E402,E501
    TARGET_AMBIGUOUS,
    TARGET_MISSING,
    TARGET_OK,
    TARGET_UNATTESTED,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E402,E501
    LANE_BASIS_EXPLICIT,
    SenderIdentity,
    derive_target_lane,
)

WS = "gk3800-abcdef012345"
L2_LANE = "issue_15693_l2_trial"
PREFLIGHT = (
    "mozyo_bridge.e_110_execution_platform."
    "f_140_delegated_coordinator_nested_handoff.application."
    "sublane_actuator_herdr_preflight"
)
GUARD_GATE = (
    "mozyo_bridge.e_110_execution_platform."
    "f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate"
)


def _env(role: str = "codex", lane: str = L2_LANE) -> dict:
    return {
        "MOZYO_WORKSPACE_ID": WS,
        "MOZYO_AGENT_ROLE": role,
        "MOZYO_LANE_ID": lane,
    }


def _l2_record(
    *,
    disposition: str = "active",
    generation: int = 1,
    lane_kind: str = LANE_KIND_DELEGATED_COORDINATOR,
) -> SimpleNamespace:
    return SimpleNamespace(
        lane_disposition=disposition,
        lane_generation=generation,
        lane_kind=lane_kind,
    )


def _target(status: str = TARGET_OK) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        assigned_name=encode_assigned_name(WS, "codex", L2_LANE),
        locator="w9:p1",
        live=1 if status != TARGET_MISSING else 0,
        with_locator=1 if status != TARGET_MISSING else 0,
        attestation_state="" if status == TARGET_OK else "stale",
        attestation_reason="",
    )


def _decide(
    *,
    env=None,
    requested_lane_kind: str = LANE_KIND_IMPLEMENTATION,
    record=None,
    record_reader=None,
    gateway_provider: str = "codex",
    coordinator_provider: str = "claude",
    target=None,
):
    """Drive the decision with every live read replaced by an injected fact."""
    reader = record_reader or (lambda _lane: record)
    with patch(f"{PREFLIGHT}.read_anchor", return_value={"workspace_id": WS}):
        with patch(
            f"{GUARD_GATE}.resolve_coordinator_provider",
            return_value=coordinator_provider,
        ):
            return evaluate_dispatch_sender_authority(
                env if env is not None else _env(),
                Path("/nonexistent-repo"),
                requested_lane_kind=requested_lane_kind,
                lifecycle_record_reader=reader,
                gateway_provider_resolver=lambda: gateway_provider,
                agent_rows_reader=lambda: (),
                inventory_workspace_resolver=lambda: WS,
                lane_target_resolver=lambda rows, **kw: (target or _target()),
            )


class DefaultCoordinatorBranchByteInvarianceTest(unittest.TestCase):
    """Every pre-#15706 outcome is byte-identical to `evaluate_dispatch_sender`."""

    def test_default_lane_coordinator_admits_with_the_legacy_text(self) -> None:
        verdict = _decide(env=_env(role="claude", lane=DEFAULT_LANE))
        self.assertTrue(verdict.ok)
        self.assertEqual(
            verdict.detail,
            "sender identity matches the coordinator binding and default lane",
        )
        self.assertEqual(verdict.sender_kind, SENDER_KIND_DEFAULT_COORDINATOR)
        self.assertEqual(verdict.parent_lane_id, "")

    def test_default_lane_wrong_provider_keeps_the_legacy_refusal(self) -> None:
        verdict = _decide(env=_env(role="codex", lane=DEFAULT_LANE))
        self.assertFalse(verdict.ok)
        self.assertEqual(
            verdict.detail,
            "sender provider 'codex' is not the configured "
            "coordinator provider 'claude'",
        )

    def test_nondefault_lane_with_non_child_target_keeps_the_legacy_refusal(self) -> None:
        # Acceptance condition 2 (非 child 対象): a non-default-lane sender that is NOT
        # creating a child implementation lane refuses with the pre-#15706 outcome —
        # INCLUDING its precedence (review j#108076 finding_legacyrefusalprecedence):
        # a provider mismatch is reported BEFORE the lane mismatch.
        for kind in ("", "delegated_coordinator", "coordinator"):
            with self.subTest(requested=kind, provider="mismatch"):
                # role=codex vs coordinator=claude: the OLD evaluator reports the
                # provider refusal first, even from a non-default lane.
                verdict = _decide(requested_lane_kind=kind, record=_l2_record())
                self.assertFalse(verdict.ok)
                self.assertEqual(
                    verdict.detail,
                    "sender provider 'codex' is not the configured "
                    "coordinator provider 'claude'",
                )
            with self.subTest(requested=kind, provider="match"):
                verdict = _decide(
                    requested_lane_kind=kind,
                    record=_l2_record(),
                    coordinator_provider="codex",
                )
                self.assertFalse(verdict.ok)
                self.assertEqual(
                    verdict.detail,
                    f"sender lane {L2_LANE!r} is not the coordinator "
                    f"default lane {DEFAULT_LANE!r}",
                )

    def test_non_admission_grid_matches_the_legacy_evaluator_byte_for_byte(self) -> None:
        # j#108076 finding_legacyrefusalprecedence: for EVERY combination outside the
        # one new admission, the new function's (ok, detail) equals the pre-#15706
        # evaluator's — pinned by direct comparison, not by re-stating the texts.
        for role in ("claude", "codex"):
            for lane in (DEFAULT_LANE, L2_LANE):
                for kind in ("", "delegated_coordinator", "coordinator"):
                    with self.subTest(role=role, lane=lane, requested=kind):
                        env = _env(role=role, lane=lane)
                        with patch(
                            f"{PREFLIGHT}.read_anchor",
                            return_value={"workspace_id": WS},
                        ):
                            with patch(
                                f"{GUARD_GATE}.resolve_coordinator_provider",
                                return_value="claude",
                            ):
                                legacy = evaluate_dispatch_sender(
                                    env, Path("/nonexistent-repo")
                                )
                                verdict = evaluate_dispatch_sender_authority(
                                    env,
                                    Path("/nonexistent-repo"),
                                    requested_lane_kind=kind,
                                    lifecycle_record_reader=(
                                        lambda _lane: _l2_record()
                                    ),
                                    gateway_provider_resolver=lambda: "codex",
                                    agent_rows_reader=lambda: (),
                                    inventory_workspace_resolver=lambda: WS,
                                    lane_target_resolver=(
                                        lambda rows, **kw: _target()
                                    ),
                                )
                        self.assertEqual(
                            (verdict.ok, verdict.detail), (legacy[0], legacy[1])
                        )
                        self.assertEqual(verdict.reason, "")

    def test_missing_sender_env_reports_the_legacy_reason(self) -> None:
        verdict = _decide(env={})
        self.assertFalse(verdict.ok)
        self.assertIn("missing_sender_env", verdict.detail)

    def test_two_tuple_contract_is_untouched_for_existing_callers(self) -> None:
        # `evaluate_dispatch_sender` (the #13613 two-tuple surface other callers keep
        # using) still refuses a non-default lane outright — no delegated admission.
        with patch(f"{PREFLIGHT}.read_anchor", return_value={"workspace_id": WS}):
            with patch(
                f"{GUARD_GATE}.resolve_coordinator_provider", return_value="codex"
            ):
                ok, detail = evaluate_dispatch_sender(_env(), Path("/nonexistent-repo"))
        self.assertFalse(ok)
        self.assertEqual(
            detail,
            f"sender lane {L2_LANE!r} is not the coordinator "
            f"default lane {DEFAULT_LANE!r}",
        )


class DelegatedGatewayBranchTest(unittest.TestCase):
    """The one new admission, and its typed fail-closed refusals (constraint 1 / 4)."""

    def test_attested_l2_gateway_creating_child_implementation_admits(self) -> None:
        verdict = _decide(record=_l2_record())
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.sender_kind, SENDER_KIND_DELEGATED_GATEWAY)
        self.assertEqual(verdict.parent_lane_id, L2_LANE)
        self.assertEqual(verdict.reason, "")

    def test_unreadable_lifecycle_refuses_typed(self) -> None:
        def _boom(_lane):
            raise OSError("store unreachable")

        verdict = _decide(record_reader=_boom)
        self.assertFalse(verdict.ok)
        self.assertIn(SENDER_LANE_LIFECYCLE_UNREADABLE, verdict.detail)
        self.assertEqual(verdict.reason, SENDER_LANE_LIFECYCLE_UNREADABLE)
        self.assertEqual(verdict.parent_lane_id, "")

    def test_missing_or_inactive_or_zero_generation_row_refuses_typed(self) -> None:
        cases = (
            None,
            _l2_record(disposition="retired"),
            _l2_record(generation=0),
        )
        for record in cases:
            with self.subTest(record=record):
                verdict = _decide(record=record)
                self.assertFalse(verdict.ok)
                self.assertIn(SENDER_LANE_UNESTABLISHED, verdict.detail)
                self.assertEqual(verdict.reason, SENDER_LANE_UNESTABLISHED)

    def test_non_delegated_coordinator_lane_kind_refuses_typed(self) -> None:
        # Acceptance condition 2 (非 L2 sender): an implementation / unkinded lane's
        # occupant never gains child-create authority.
        for kind in ("", LANE_KIND_IMPLEMENTATION, "coordinator"):
            with self.subTest(stored=kind):
                verdict = _decide(record=_l2_record(lane_kind=kind))
                self.assertFalse(verdict.ok)
                self.assertIn(SENDER_LANE_NOT_DELEGATED_COORDINATOR, verdict.detail)
                self.assertEqual(
                    verdict.reason, SENDER_LANE_NOT_DELEGATED_COORDINATOR
                )

    def test_non_gateway_provider_refuses_typed(self) -> None:
        verdict = _decide(env=_env(role="claude"), record=_l2_record())
        self.assertFalse(verdict.ok)
        self.assertIn(SENDER_PROVIDER_NOT_GATEWAY, verdict.detail)
        self.assertEqual(verdict.reason, SENDER_PROVIDER_NOT_GATEWAY)

    def test_unresolved_gateway_provider_refuses_typed(self) -> None:
        def _unresolved():
            raise RuntimeError("no binding")

        with patch(f"{PREFLIGHT}.read_anchor", return_value={"workspace_id": WS}):
            with patch(
                f"{GUARD_GATE}.resolve_coordinator_provider", return_value="claude"
            ):
                verdict = evaluate_dispatch_sender_authority(
                    _env(),
                    Path("/nonexistent-repo"),
                    requested_lane_kind=LANE_KIND_IMPLEMENTATION,
                    lifecycle_record_reader=lambda _lane: _l2_record(),
                    gateway_provider_resolver=_unresolved,
                )
        self.assertFalse(verdict.ok)
        self.assertIn(SENDER_PROVIDER_NOT_GATEWAY, verdict.detail)

    def test_unattested_or_unlive_gateway_slot_refuses_typed(self) -> None:
        # Acceptance condition 2 (未 attested sender): only the exactly-one live,
        # launch-time attested occupant admits — every other liveness is typed.
        cases = (
            (TARGET_MISSING, SENDER_GATEWAY_LIVE_MISSING),
            (TARGET_AMBIGUOUS, SENDER_GATEWAY_LIVE_AMBIGUOUS),
            (TARGET_UNATTESTED, SENDER_GATEWAY_UNATTESTED),
        )
        for status, token in cases:
            with self.subTest(status=status):
                verdict = _decide(record=_l2_record(), target=_target(status))
                self.assertFalse(verdict.ok)
                self.assertIn(token, verdict.detail)
                self.assertEqual(verdict.reason, token)
                self.assertEqual(verdict.parent_lane_id, "")


class DelegatedSenderNextActionProjectionTest(unittest.TestCase):
    """j#108076 finding_typedreasonprojection: each branch token reaches the public
    payload / journal with its OWN next action; token-less refusals stay byte-invariant."""

    @staticmethod
    def _blocked(reasons: tuple) -> SublaneActuationOutcome:
        return SublaneActuationOutcome(
            status=ACTUATE_BLOCKED,
            execute=True,
            reason="dispatch sender attestation failed before actuation",
            issue="15706",
            lane_label="issue_15703_l3_child",
            blocked_reasons=reasons,
        )

    def test_each_branch_token_maps_to_its_own_next_action(self) -> None:
        for token, action in sorted(DELEGATED_SENDER_NEXT_ACTIONS.items()):
            with self.subTest(token=token):
                outcome = self._blocked(
                    (REASON_MISSING_IDENTITY, "sender_attestation", token)
                )
                payload = outcome.as_payload()
                self.assertEqual(payload["next_action"]["action"], action)
                self.assertEqual(payload["next_action"]["blocked_reason"], token)
                self.assertIn(token, payload["blocked_reasons"])
                rendered = render_actuation_journal(outcome)
                self.assertIn(f"next_action: {action} ({token})", rendered)
                self.assertNotIn("restore_attested_coordinator_shell", rendered)

    def test_token_less_sender_refusal_keeps_the_coordinator_shell_action(self) -> None:
        # The pre-#15706 refusal shape (no branch token) projects the EXACT legacy
        # next action — payload dict and journal text unchanged.
        outcome = self._blocked((REASON_MISSING_IDENTITY, "sender_attestation"))
        payload = outcome.as_payload()
        self.assertEqual(
            payload["next_action"],
            {
                "action": "restore_attested_coordinator_shell",
                "allowed_methods": [
                    "relaunch_from_fixed_session_start",
                    "verified_high_level_coordinator_proxy",
                ],
                "forbidden_methods": [
                    "manual_mozyo_env_injection",
                    "raw_herdr_send",
                ],
            },
        )
        rendered = render_actuation_journal(outcome)
        self.assertIn(
            "- next_action: restore an attested coordinator shell by relaunching "
            "from fixed session-start, or use a verified high-level coordinator "
            "proxy; do not inject MOZYO env manually or use raw herdr send",
            rendered,
        )


def _attested(_name, *, locator, terminal_id, workspace_id, provider, lane_id=DEFAULT_LANE):
    return True, "ok", "startup self-attestation present and generation-matched"


def _row(provider: str, lane: str, locator: str = "w9:p1") -> dict:
    row = {AGENT_KEY_NAME: encode_assigned_name(WS, provider, lane)}
    if locator:
        row[AGENT_KEY_LOCATOR] = locator
    return row


class ResolveProxyTargetLaneGeneralizationTest(unittest.TestCase):
    """The exactly-one live-attested policy, resolved against a SUBLANE slot (#15706)."""

    def test_sublane_gateway_slot_resolves_by_its_lane(self) -> None:
        rows = [
            _row("codex", ""),  # the default-lane coordinator — a different slot
            _row("codex", L2_LANE, locator="w9:p2"),
            _row("claude", L2_LANE, locator="w9:p3"),  # the lane's worker slot
        ]
        joined = {}

        def _join(_name, *, locator, terminal_id, workspace_id, provider, lane_id=DEFAULT_LANE):
            joined["lane_id"] = lane_id
            return True, "ok", "generation-matched"

        target = resolve_proxy_target(
            rows,
            workspace_id=WS,
            provider="codex",
            lane_id=L2_LANE,
            attestation_join=_join,
        )
        self.assertEqual(target.status, TARGET_OK)
        self.assertEqual(
            target.assigned_name, encode_assigned_name(WS, "codex", L2_LANE)
        )
        # The join verifies the SUBLANE slot's expected lane, not the default lane.
        self.assertEqual(joined["lane_id"], L2_LANE)

    def test_default_lane_call_is_byte_invariant_including_the_join_seam(self) -> None:
        rows = [_row("codex", ""), _row("codex", L2_LANE, locator="w9:p2")]
        seen_kwargs = {}

        def _legacy_join(_name, *, locator, terminal_id, workspace_id, provider):
            # The pre-#15706 seam signature (no lane kwarg) must keep working for the
            # default-lane call — the coordinator proxy rail's own resolution.
            seen_kwargs["provider"] = provider
            return True, "ok", "generation-matched"

        target = resolve_proxy_target(
            rows, workspace_id=WS, provider="codex", attestation_join=_legacy_join
        )
        self.assertEqual(target.status, TARGET_OK)
        self.assertEqual(target.assigned_name, encode_assigned_name(WS, "codex", ""))
        self.assertEqual(seen_kwargs["provider"], "codex")

    def test_two_live_occupants_of_the_lane_slot_are_ambiguous(self) -> None:
        rows = [
            _row("codex", L2_LANE, locator="w9:p2"),
            _row("codex", L2_LANE, locator="w9:p4"),
        ]
        target = resolve_proxy_target(
            rows,
            workspace_id=WS,
            provider="codex",
            lane_id=L2_LANE,
            attestation_join=_attested,
        )
        self.assertEqual(target.status, TARGET_AMBIGUOUS)


class ChildGatewayCallbackRouteTest(unittest.TestCase):
    """Design constraint 3: the child -> parent callback rides the EXISTING rail."""

    def test_explicit_parent_lane_derives_tier_one_on_the_existing_rail(self) -> None:
        # From the child lane, `--to codex --target-lane <parent_lane>` derives the
        # parent delegated_coordinator lane via the #13305 tier-1 explicit-lane rule —
        # no new routing surface is introduced for the L3 -> L2 callback.
        child_sender = SenderIdentity(
            workspace_id=WS, role="codex", lane_id="issue_15703_l3_child"
        )
        derivation = derive_target_lane(
            "codex", child_sender, explicit_lane=L2_LANE
        )
        self.assertEqual(derivation.lane, L2_LANE)
        self.assertEqual(derivation.basis, LANE_BASIS_EXPLICIT)


if __name__ == "__main__":
    unittest.main()
