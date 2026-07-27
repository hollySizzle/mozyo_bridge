"""Regression: the action-bound relaunch carries its exact v1 binding context (#14480).

The measured defect (#14479 j#88695, installed 0.14.0a3 dogfood): with the selected
identity-attestation store at v1, ``sublane recover-gateway --execute`` closed the exact old
gateway and then failed its relaunch twice with a bare ``effect_failed: launch`` — on a
committed-close replay whose lane authority read ``ok`` and whose sibling worker was live and
done. The lane was left with the gateway closed, no fresh gateway, no resume, and no public
statement of why.

Two distinct defects sit behind that one opaque token, and this module pins both:

1. **The launch had no binding context.** :meth:`LiveRecoveryActuatorPort.launch_action_bound`
   passed only ``replacement_action_id`` and called ``heal_lane_column`` unscoped. Under v1
   the action binding is a side record keyed on the exact participant, so
   ``launch_or_resume_v1_replacement`` refuses a partial context with
   ``replacement_binding_context_missing``. The relaunch was not "generic" — it could not
   launch at all.
2. **The typed reason died at the port.** A broad ``except`` mapped every cause to
   ``LAUNCH_ERROR``, and the generic actuator records a hardcoded ``detail="launch"``, so the
   operator could not tell a binding fence from a transient pane failure.

The first class drives the REAL chain — port -> ``heal_lane_column`` -> ``V1ReplacementDriver``
-> ``launch_or_resume_v1_replacement`` -> reserve / startup receipt / side bind — against an
isolated home with v1 selected. Only the process-launching seam is faked, and it is faked by
*doing what a real launch does* (write the startup-transaction receipt and the normal-v1
attestation row), so the v1 binding state machine is exercised rather than mocked away. On the
pre-fix source these tests fail at ``replacement_binding_context_missing``.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    VERDICT_PRESENT,
    HerdrIdentityAttestationStore,
    IdentityAttestationRecord,
)
from mozyo_bridge.core.state.replacement_preservation import (  # noqa: E402
    PreservationObservation,
)
from mozyo_bridge.core.state.replacement_transaction import (  # noqa: E402
    ParticipantPin,
    ReplacementTransactionKey,
    ReplacementTransactionStore,
)
from mozyo_bridge.core.state.startup_transaction_fence import (  # noqa: E402
    PHASE_COMPLETED_SUCCESS,
    Participant,
    StartupUnit,
    startup_action_id,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_actuator_herdr_ops as herdr_ops,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    sublane_actuator_v1_replacement as v1_drive,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery import (  # noqa: E402,E501
    GatewayRefreshRequest,
    GatewayRefreshUseCase,
    REFRESH_STATUS_PREFLIGHT,
    REFRESH_STATUS_STOPPED,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery import (  # noqa: E402,E501
    RecoveryRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live import (  # noqa: E402,E501
    LiveRecoveryActuatorPort,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E402,E501
    GatewayRefreshObservation,
    GatewayTurnObservation,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.lane_launch_authority import (  # noqa: E402,E501
    LAUNCH_AUTHORITY_OK,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    ATTEST_BOUND,
    CLOSE_DONE,
    LAUNCH_DONE,
    LAUNCH_ERROR,
    OLD_SLOT_PRESENT,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_launch_failure import (  # noqa: E402,E501
    LAUNCH_FAILURE_NONE,
    LAUNCH_FAILURE_UNTYPED,
    launch_failure_detail,
    normalize_launch_failure_reason,
    port_launch_failure_reason,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_runtime_fence import (  # noqa: E402,E501
    HEAL_REASON_PAIR_SPLIT,
    SublaneHealError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E402,E501
    SLOT_ADOPTED,
    SLOT_LAUNCHED,
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding import (  # noqa: E402,E501
    V1_BINDING_CONTEXT_MISSING,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E402,E501
    HEALTH_HEALTHY,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
LANE = "issue_14480_recover_gateway_v1_binding_r1"
GATEWAY_PROVIDER = "codex"
WORKER_PROVIDER = "claude"
MANAGED = (GATEWAY_PROVIDER, WORKER_PROVIDER)
TAB = "w3N:tW"
OLD_GATEWAY_LOCATOR = "w3N:p2J"
FRESH_GATEWAY_LOCATOR = "w3N:p3A"
WORKER_LOCATOR = "w3N:p2K"
ACTION_ID = "refresh-gateway:{lane}:codex:codex:gw:{loc}:r1".format(
    lane=LANE, loc=OLD_GATEWAY_LOCATOR
)

GATEWAY_NAME = encode_assigned_name(WS, GATEWAY_PROVIDER, LANE)
WORKER_NAME = encode_assigned_name(WS, WORKER_PROVIDER, LANE)


@contextlib.contextmanager
def _nolock(*_args, **_kwargs):
    yield


class _V1LaunchCase(unittest.TestCase):
    """Drive the REAL v1 action-bound launch chain over an isolated home.

    The lane state under test is the exact measured one: the old gateway is already closed
    (absent from the live inventory), the sibling worker is live at its own locator, and the
    transaction's gateway participant is ``launch_owed``.
    """

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.repo_root = self.home / "worktree"
        self.repo_root.mkdir()
        self.store = ReplacementTransactionStore(home=self.home)
        self.key = ReplacementTransactionKey(WS, ACTION_ID)
        # What the fake launch seam recorded, so a test can assert on the launch it drove
        # rather than on the fact that something was called.
        self.launch_calls: list[dict] = []
        # Post-heal placement of the two slots, as the read-back resolves them. The default is
        # the converged state: fresh gateway + surviving worker in ONE placement container.
        self.post_gateway = (FRESH_GATEWAY_LOCATOR, TAB)
        self.post_worker = (WORKER_LOCATOR, TAB)

    def _request(self) -> RecoveryRequest:
        return RecoveryRequest(
            issue="14480", lane=LANE, role=GATEWAY_PROVIDER, provider=GATEWAY_PROVIDER,
            assigned_name=GATEWAY_NAME, locator=OLD_GATEWAY_LOCATOR, journal="88698",
            action_id=ACTION_ID, action_generation=1, worker_revision="1",
            lane_revision="1", lane_generation="1",
        )

    def _pin(self, **overrides) -> ParticipantPin:
        base = dict(
            lane_id=LANE, role=GATEWAY_PROVIDER, provider=GATEWAY_PROVIDER,
            assigned_name=GATEWAY_NAME, old_locator=OLD_GATEWAY_LOCATOR, is_self=False,
            lane_revision="1", lane_generation="1",
        )
        base.update(overrides)
        return ParticipantPin(**base)

    def _port(self) -> LiveRecoveryActuatorPort:
        return LiveRecoveryActuatorPort(
            repo_root=self.repo_root, request=self._request(), store=self.store,
            key=self.key, env={}, attestation_home=self.home, lifecycle_home=self.home,
        )

    def _live_rows(self):
        """The measured live inventory: the old gateway is gone, the worker survives."""
        return [
            {
                "assigned_name": WORKER_NAME, "pane_id": WORKER_LOCATOR,
                "revision": "1", "agent_status": "turn_ended",
            }
        ]

    def _fake_launch(self, outer, worktree_path, **kwargs):
        """Stand in for the ONE process-launching seam, doing what a real launch does.

        A real ``prepare_session`` reserves the startup transaction under the nonce it was
        handed, records the participant receipt for the slot it actually started, marks the
        transaction durably successful, and leaves a normal-v1 startup self-attestation row at
        the fresh locator. All four are what the v1 side-bind then joins on, so faking the
        *subprocess* while performing the *durable effects* keeps the binding state machine
        under test instead of stubbed out.
        """
        nonce = kwargs.get("action_nonce", "")
        fence = kwargs.get("startup_fence")
        providers = kwargs.get("providers")
        self.launch_calls.append(
            {
                "worktree_path": worktree_path,
                "action_nonce": nonce,
                "providers": providers,
                "replacement_action_id": kwargs.get("replacement_action_id"),
            }
        )
        # ``providers=None`` means the pair-level launcher runs: ``prepare_session`` is
        # adopt-or-launch idempotent per slot, so the LIVE worker is adopted (not restarted)
        # and only the missing gateway is started.
        unit = StartupUnit(WS, LANE, MANAGED if providers is None else tuple(providers))
        action_id = startup_action_id(unit, nonce)
        fence.reserve(unit, nonce)
        fence.record_participant(
            action_id,
            Participant(
                role=GATEWAY_PROVIDER, assigned_name=GATEWAY_NAME,
                locator=FRESH_GATEWAY_LOCATOR, receipt=TAB,
            ),
        )
        fence.set_phase(action_id, PHASE_COMPLETED_SUCCESS)
        HerdrIdentityAttestationStore(home=self.home).upsert(
            IdentityAttestationRecord(
                assigned_name=GATEWAY_NAME, workspace_id=WS, role=GATEWAY_PROVIDER,
                lane_id=LANE, locator=FRESH_GATEWAY_LOCATOR, verdict=VERDICT_PRESENT,
            )
        )
        return SessionStartResult(
            workspace_id=WS, lane_id=LANE, action_id=action_id,
            herdr_workspace_id="w3N", herdr_tab_id=TAB,
            slots=[
                SlotResult(
                    provider=GATEWAY_PROVIDER, assigned_name=GATEWAY_NAME,
                    outcome=SLOT_LAUNCHED, locator=FRESH_GATEWAY_LOCATOR,
                    health=HEALTH_HEALTHY,
                ),
                SlotResult(
                    provider=WORKER_PROVIDER, assigned_name=WORKER_NAME,
                    outcome=SLOT_ADOPTED, locator=WORKER_LOCATOR, health=HEALTH_HEALTHY,
                ),
            ],
        )

    def _resolve_lane_slots(self, _self, _worktree, _rows, _managed=None):
        """The lane read-back. Pre-heal: gateway absent, worker live. Post-heal: converged."""
        self._resolve_calls = getattr(self, "_resolve_calls", 0) + 1
        if self._resolve_calls == 1:
            return WS, LANE, {WORKER_PROVIDER: (WORKER_LOCATOR, TAB)}
        healed = {}
        if self.post_gateway is not None:
            healed[GATEWAY_PROVIDER] = self.post_gateway
        if self.post_worker is not None:
            healed[WORKER_PROVIDER] = self.post_worker
        return WS, LANE, healed

    @contextlib.contextmanager
    def _real_v1_chain(self):
        """Patch only the external boundaries; the v1 binding logic stays real."""
        test = self
        with contextlib.ExitStack() as stack:
            for target, name, value in [
                (herdr_ops, "mozyo_bridge_home", lambda: test.home),
                (
                    herdr_ops, "evaluate_heal_runtime_fence",
                    lambda *a, **k: SimpleNamespace(ok=True, reason="", detail=""),
                ),
                (v1_drive, "selected_attestation_store_is_v1", lambda home: True),
                (v1_drive, "attestation_store_lock", _nolock),
            ]:
                stack.enter_context(mock.patch.object(target, name, value))
            for cls, name, value in [
                (herdr_ops.HerdrSublaneActuatorOps, "_live_rows",
                 lambda _self: test._live_rows()),
                (herdr_ops.HerdrSublaneActuatorOps, "_launch_providers",
                 lambda _self: MANAGED),
                (herdr_ops.HerdrSublaneActuatorOps, "_resolve_lane_slots",
                 test._resolve_lane_slots),
                (herdr_ops.HerdrSublaneActuatorOps, "_prepare_lane_session",
                 lambda _self, worktree_path, **kw: test._fake_launch(
                     _self, worktree_path, **kw
                 )),
            ]:
                stack.enter_context(mock.patch.object(cls, name, value))
            yield


class ExactBindingContextTests(_V1LaunchCase):
    """#14480 acceptance 1 + 3: the v1 launch receives the exact pin context and completes."""

    def test_committed_close_replay_launches_and_binds_under_selected_v1(self):
        """The negative control. Pre-fix this stops at ``replacement_binding_context_missing``.

        Nothing here is asserted through a mock of the thing under test: the launch drives the
        real ``heal_lane_column`` -> real ``V1ReplacementDriver`` -> real
        ``launch_or_resume_v1_replacement``, and the side bind must find the exact reserve,
        the exact startup receipt, and a clean normal-v1 attestation row.
        """
        port = self._port()
        with self._real_v1_chain():
            result = port.launch_action_bound(ACTION_ID, self._pin())
        self.assertEqual(result, LAUNCH_DONE)
        # A completed launch fenced on nothing — the typed field says "no fence", which is a
        # different statement from "we do not know why" (LAUNCH_FAILURE_UNTYPED).
        self.assertEqual(port.launch_failure_reason, LAUNCH_FAILURE_NONE)
        self.assertIsNone(port.launch_startup_health)
        # The v1 side binding for the EXACT action + participant is durable, which is what
        # ``verify_attestation`` re-checks. A launch that never bound would leave this absent.
        from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
            HerdrIdentityReplacementBindingStore,
        )

        intent = HerdrIdentityReplacementBindingStore(home=self.home).read(
            ACTION_ID, GATEWAY_NAME
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.assigned_name, GATEWAY_NAME)
        self.assertEqual(intent.role, GATEWAY_PROVIDER)
        self.assertEqual(intent.lane_id, LANE)
        self.assertEqual(intent.old_locator, OLD_GATEWAY_LOCATOR)

    def test_the_launch_is_driven_for_the_gateway_only_and_adopts_the_live_worker(self):
        """#14480 acceptance 2: the surviving worker is adopted, never relaunched or closed."""
        port = self._port()
        with self._real_v1_chain():
            self.assertEqual(port.launch_action_bound(ACTION_ID, self._pin()), LAUNCH_DONE)
        self.assertEqual(len(self.launch_calls), 1)
        call = self.launch_calls[0]
        # ``providers=None`` keeps the pair-level, adopt-or-launch-idempotent launcher: the
        # live worker is adopted in place. Restricting the launch to the target provider is
        # recover-pair's explicit both-absent mode and must NOT be switched on here — doing so
        # would start the gateway beside an unreserved sibling.
        self.assertIsNone(call["providers"])
        # The bind is reserved for the gateway participant alone; the worker's name never
        # enters the replacement binding store, so the worker holds no replacement authority.
        from mozyo_bridge.core.state.herdr_identity_attestation_replacement_binding import (
            HerdrIdentityReplacementBindingStore,
        )

        store = HerdrIdentityReplacementBindingStore(home=self.home)
        self.assertIsNotNone(store.read(ACTION_ID, GATEWAY_NAME))
        self.assertIsNone(store.read(ACTION_ID, WORKER_NAME))
        # The worker's live attestation row is untouched by the gateway's relaunch: the
        # recovery never writes, rebinds, or closes the sibling it is preserving.
        self.assertIsNone(
            HerdrIdentityAttestationStore(home=self.home).read(WORKER_NAME)
        )

    def test_an_incomplete_pin_context_is_reported_as_its_typed_reason(self):
        """#14480 acceptance 4: the v1 context fence reaches the public field, not a bare token.

        Drives the SAME real chain with a pin whose assigned name does not encode
        ``(workspace, provider, lane)`` — the exact shape v1 refuses. Pre-fix EVERY launch took
        this path and reported nothing; the point of the assertion is the reason, not the
        failure.
        """
        port = self._port()
        with self._real_v1_chain():
            result = port.launch_action_bound(
                ACTION_ID, self._pin(assigned_name="not-an-encoded-name")
            )
        self.assertEqual(result, LAUNCH_ERROR)
        self.assertEqual(port.launch_failure_reason, V1_BINDING_CONTEXT_MISSING)
        # And the port exposes it through the shared typed-capability reader the public
        # surfaces use, not only as a private attribute.
        self.assertEqual(port_launch_failure_reason(port), V1_BINDING_CONTEXT_MISSING)

    def test_a_live_pair_split_after_the_relaunch_still_fails_closed(self):
        """#14480 acceptance 5: target scoping did not weaken the same-tab pair invariant.

        Scoping the postcondition to one owed participant tolerates an ABSENT sibling (a later
        leg converges it). A LIVE sibling in a DIFFERENT placement container is a split, and a
        split is never healed over — that is the #13705 contract this scoping must not relax.
        """
        self.post_worker = (WORKER_LOCATOR, "w3N:tOTHER")
        port = self._port()
        with self._real_v1_chain():
            result = port.launch_action_bound(ACTION_ID, self._pin())
        self.assertEqual(result, LAUNCH_ERROR)
        self.assertEqual(port.launch_failure_reason, HEAL_REASON_PAIR_SPLIT)

    def test_a_still_absent_sibling_is_a_partial_state_not_a_launch_failure(self):
        """The other half of the scoping contract: an absent sibling converges later."""
        self.post_worker = None
        port = self._port()
        with self._real_v1_chain():
            result = port.launch_action_bound(ACTION_ID, self._pin())
        self.assertEqual(result, LAUNCH_DONE)
        self.assertEqual(port.launch_failure_reason, LAUNCH_FAILURE_NONE)


class ArgumentThreadingTests(_V1LaunchCase):
    """#14480 acceptance 1, pinned at the exact call boundary the defect lived on."""

    def test_the_pin_context_reaches_the_lane_actuator_and_the_scoped_heal(self):
        calls: list = []

        class FakeActuator:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def heal_lane_column(self, worktree, *, target_provider=None):
                calls.append(("heal", worktree, target_provider))

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_stale_worker_recovery_live.HerdrSublaneActuatorOps"
        )
        port = self._port()
        with mock.patch(module, FakeActuator):
            result = port.launch_action_bound(ACTION_ID, self._pin())

        self.assertEqual(result, LAUNCH_DONE)
        init = calls[0][1]
        self.assertEqual(init["replacement_action_id"], ACTION_ID)
        # The three fields the pre-fix call omitted entirely.
        self.assertEqual(init["replacement_assigned_name"], GATEWAY_NAME)
        self.assertEqual(init["replacement_old_locator"], OLD_GATEWAY_LOCATOR)
        self.assertEqual(calls[1], ("heal", str(self.repo_root), GATEWAY_PROVIDER))
        # Recover-pair's both-absent target-only mode is NOT switched on by this path.
        self.assertFalse(init.get("replacement_target_only", False))

    def test_a_worker_recovery_pin_scopes_to_the_worker_provider(self):
        """The port is shared with ``recover-stale``; the scoping follows the PIN, not a role
        constant. A ticket id / locator / hardcoded provider here would be the special-casing
        the request forbids."""
        calls: list = []

        class FakeActuator:
            def __init__(self, **kwargs):
                calls.append(("init", kwargs))

            def heal_lane_column(self, worktree, *, target_provider=None):
                calls.append(("heal", worktree, target_provider))

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_stale_worker_recovery_live.HerdrSublaneActuatorOps"
        )
        worker_pin = self._pin(
            role=WORKER_PROVIDER, provider=WORKER_PROVIDER,
            assigned_name=WORKER_NAME, old_locator=WORKER_LOCATOR,
        )
        port = self._port()
        with mock.patch(module, FakeActuator):
            self.assertEqual(port.launch_action_bound(ACTION_ID, worker_pin), LAUNCH_DONE)
        self.assertEqual(calls[0][1]["replacement_assigned_name"], WORKER_NAME)
        self.assertEqual(calls[0][1]["replacement_old_locator"], WORKER_LOCATOR)
        self.assertEqual(calls[1][2], WORKER_PROVIDER)

    def test_an_untyped_launch_failure_is_reported_as_untyped_not_as_no_failure(self):
        class ExplodingActuator:
            def __init__(self, **kwargs):
                pass

            def heal_lane_column(self, worktree, *, target_provider=None):
                raise RuntimeError("lane heal preflight fenced (inventory_unreadable): ...")

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_stale_worker_recovery_live.HerdrSublaneActuatorOps"
        )
        port = self._port()
        with mock.patch(module, ExplodingActuator):
            self.assertEqual(port.launch_action_bound(ACTION_ID, self._pin()), LAUNCH_ERROR)
        self.assertEqual(port.launch_failure_reason, LAUNCH_FAILURE_UNTYPED)

    def test_a_successful_launch_clears_a_previous_runs_failure_reason(self):
        """The field is an observation of the LAST launch, never a sticky accumulator."""
        class ExplodingActuator:
            def __init__(self, **kwargs):
                pass

            def heal_lane_column(self, worktree, *, target_provider=None):
                raise RuntimeError("boom")

        class FineActuator:
            def __init__(self, **kwargs):
                pass

            def heal_lane_column(self, worktree, *, target_provider=None):
                return None

        module = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.sublane_stale_worker_recovery_live.HerdrSublaneActuatorOps"
        )
        port = self._port()
        with mock.patch(module, ExplodingActuator):
            port.launch_action_bound(ACTION_ID, self._pin())
        self.assertEqual(port.launch_failure_reason, LAUNCH_FAILURE_UNTYPED)
        with mock.patch(module, FineActuator):
            port.launch_action_bound(ACTION_ID, self._pin())
        self.assertEqual(port.launch_failure_reason, LAUNCH_FAILURE_NONE)


class TypedReasonProjectionTests(unittest.TestCase):
    """The pure projection rule shared by every surface that renders a launch stop."""

    def test_a_closed_token_survives_verbatim(self):
        self.assertEqual(
            normalize_launch_failure_reason(V1_BINDING_CONTEXT_MISSING),
            V1_BINDING_CONTEXT_MISSING,
        )

    def test_absence_and_unknown_are_distinct_statements(self):
        self.assertEqual(normalize_launch_failure_reason(""), LAUNCH_FAILURE_NONE)
        self.assertEqual(normalize_launch_failure_reason(None), LAUNCH_FAILURE_NONE)
        self.assertNotEqual(LAUNCH_FAILURE_NONE, LAUNCH_FAILURE_UNTYPED)

    def test_a_value_bearing_string_never_reaches_a_public_field(self):
        """The shape guard: a path / locator / prose is reported as untyped, not published."""
        # Deliberately NOT written as real home / temp path literals: this repo forbids
        # home-shaped absolute paths in tracked files even inside a redaction test. The guard
        # under test keys on token SHAPE (separators, case, spaces), which these still carry.
        for hostile in (
            "/opt/lane/worktree",
            "w3N:p2J",
            "failed to start: /opt/x",
            "TOKEN_ABC123",
        ):
            self.assertEqual(
                normalize_launch_failure_reason(hostile), LAUNCH_FAILURE_UNTYPED, hostile
            )

    def test_only_a_launch_leg_stop_is_rewritten(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E501
            ACTUATION_EFFECT_FAILED,
            ACTUATION_LEASE_LOST,
        )

        self.assertEqual(
            launch_failure_detail(
                status=ACTUATION_EFFECT_FAILED, detail="launch",
                reason=V1_BINDING_CONTEXT_MISSING,
            ),
            f"launch:{V1_BINDING_CONTEXT_MISSING}",
        )
        # A close-leg failure, a lease loss, and a launch stop with no reason all keep their
        # own detail byte-for-byte — the projection adds information, it never rewrites.
        self.assertEqual(
            launch_failure_detail(
                status=ACTUATION_EFFECT_FAILED, detail="close",
                reason=V1_BINDING_CONTEXT_MISSING,
            ),
            "close",
        )
        self.assertEqual(
            launch_failure_detail(
                status=ACTUATION_LEASE_LOST, detail="lease not live",
                reason=V1_BINDING_CONTEXT_MISSING,
            ),
            "lease not live",
        )
        self.assertEqual(
            launch_failure_detail(
                status=ACTUATION_EFFECT_FAILED, detail="launch",
                reason=LAUNCH_FAILURE_NONE,
            ),
            "launch",
        )

    def test_a_port_without_the_capability_reports_no_fence(self):
        self.assertEqual(port_launch_failure_reason(object()), LAUNCH_FAILURE_NONE)

    def test_a_port_whose_attribute_raises_is_not_a_diagnosis(self):
        class Hostile:
            @property
            def launch_failure_reason(self):
                raise RuntimeError("unreadable")

        self.assertEqual(port_launch_failure_reason(Hostile()), LAUNCH_FAILURE_NONE)


class _FencedLaunchPort:
    """An actuator port whose launch leg fences with a typed reason."""

    def __init__(self, reason=V1_BINDING_CONTEXT_MISSING):
        self.launch_failure_reason = reason

    def observe_old_slot(self, pin) -> str:
        return OLD_SLOT_PRESENT

    def observe_preservation(self, pin) -> PreservationObservation:
        return PreservationObservation(identity_matches=True, attestation_fresh=True)

    def close_exact_generation(self, pin) -> str:
        return CLOSE_DONE

    def launch_action_bound(self, action_id: str, pin) -> str:
        return LAUNCH_ERROR

    def verify_attestation(self, action_id: str, pin) -> str:
        return ATTEST_BOUND


class _Ops:
    def __init__(self):
        self.resumes: list = []

    def observe_turn(self, request) -> GatewayTurnObservation:
        return GatewayTurnObservation(
            delivery_confirmed=True, turn_started=True, settled_turn_ended=True,
            expected_gate_absent=True, durable_source_fresh=True,
        )

    def observe_target(self, request) -> GatewayRefreshObservation:
        return GatewayRefreshObservation(
            identity_resolved=True, is_lane_implementation_gateway=True,
            issue_lane_matches=True, generation_matches=True, settled_idle=True,
            composer_clear=True, resume_anchor_present=True,
            worker_distinct_preserved=True, no_authority_conflict=True,
            launch_authority_current=True,
        )

    def lane_authority_reason(self, request) -> str:
        return LAUNCH_AUTHORITY_OK

    def resume_lane_authority(self, request) -> bool:
        return True

    def gateway_name_free_of_live_process(self, request) -> bool:
        return True

    def resume_rail_ready(self, request) -> bool:
        return True

    def resume_confirmed(self, continuation) -> bool:
        return False

    def resume_once(self, continuation) -> str:
        self.resumes.append(continuation)
        return "send_ok"


class PublicOutcomeTests(unittest.TestCase):
    """#14480 acceptance 4 at the public surface: a typed field plus a compatible detail."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.store = ReplacementTransactionStore(home=self.home)

    def _request(self) -> GatewayRefreshRequest:
        return GatewayRefreshRequest(
            issue="14480", lane=LANE, role=GATEWAY_PROVIDER, provider=GATEWAY_PROVIDER,
            assigned_name="gw", locator=OLD_GATEWAY_LOCATOR, journal="88698",
            action_id="", action_generation=2, gateway_revision="1",
            lane_revision="1", lane_generation="1",
            resume_anchor_journal="88697", resume_gate="review_request",
        )

    def _use_case(self, port):
        return GatewayRefreshUseCase(
            self.store, port, _Ops(), workspace_id=WS,
            clock=lambda: "2026-07-27T00:00:00+00:00",
        )

    def _actionable_request(self) -> GatewayRefreshRequest:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.gateway_turn_recovery import (  # noqa: E501
            gateway_refresh_action_id,
        )

        base = self._request()
        action_id = gateway_refresh_action_id(
            lane_id=base.lane, role=base.role, provider=base.provider,
            assigned_name=base.assigned_name, locator=base.locator,
            revision=base.gateway_revision,
        )
        return GatewayRefreshRequest(
            **{**base.__dict__, "action_id": action_id}
        )

    def test_a_fenced_launch_names_its_reason_in_a_typed_field_and_in_the_detail(self):
        port = _FencedLaunchPort()
        outcome = self._use_case(port).run(self._actionable_request(), execute=True)
        self.assertEqual(outcome.status, REFRESH_STATUS_STOPPED)
        self.assertEqual(outcome.launch_failure_reason, V1_BINDING_CONTEXT_MISSING)
        # The compatibility rendering: readers that predate the typed field still see the
        # reason instead of a bare ``launch``.
        self.assertIn(f"launch:{V1_BINDING_CONTEXT_MISSING}", outcome.detail)
        payload = outcome.as_payload()
        self.assertEqual(payload["launch_failure_reason"], V1_BINDING_CONTEXT_MISSING)
        # It is a fence name, never a path / locator / credential.
        self.assertNotIn("/", payload["launch_failure_reason"])
        self.assertNotIn(OLD_GATEWAY_LOCATOR, payload["launch_failure_reason"])
        # The lane authority axis is an INDEPENDENT observation and stays ``ok``: the launch
        # did not fail because the authority moved (review j#88485's rule).
        self.assertEqual(outcome.launch_authority_reason, LAUNCH_AUTHORITY_OK)

    def test_the_text_surface_shows_the_launch_failure_only_when_one_fired(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_gateway_recovery_cli import (  # noqa: E501
            format_recover_gateway_text,
        )

        stopped = self._use_case(_FencedLaunchPort()).run(
            self._actionable_request(), execute=True
        )
        self.assertIn(
            f"launch_failure: {V1_BINDING_CONTEXT_MISSING}",
            format_recover_gateway_text(stopped),
        )
        preflight = self._use_case(_FencedLaunchPort()).run(
            self._request(), execute=False
        )
        self.assertEqual(preflight.status, REFRESH_STATUS_PREFLIGHT)
        self.assertEqual(preflight.launch_failure_reason, LAUNCH_FAILURE_NONE)
        self.assertNotIn("launch_failure:", format_recover_gateway_text(preflight))

    def test_every_outcome_carries_the_key_so_absence_is_never_inferred(self):
        preflight = self._use_case(_FencedLaunchPort()).run(
            self._request(), execute=False
        )
        payload = preflight.as_payload()
        self.assertIn("launch_failure_reason", payload)
        self.assertIsNone(payload["launch_failure_reason"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
