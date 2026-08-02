"""The stored evidence cause reaches the launch preflight (#14741 j#97171 B6b1).

A relaunch that the receipt authority says was caused by an update must arm the
update-authority fence, and every other launch must be byte-invariant with the pre-#14741
one -- including its cost, which means querying no updater at all.

Three layers are pinned here: what the live port extracts from the pin, what the actuator
ops carries by default, and that the token really arrives at
``preflight_launch_providers``. Nothing launches: the chain is observed at its own seams.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.core.state.replacement_transaction_model import (  # noqa: E402
    ParticipantPin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E402,E501
    HerdrSublaneActuatorOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.replacement_launch_cause import (  # noqa: E402,E501
    launch_cause_for_pin,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.replacement_actuation import (  # noqa: E402,E501
    LAUNCH_ERROR,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_launch_composition import (  # noqa: E402,E501
    LAUNCH_CAUSE_GENERIC_FRESH,
    LAUNCH_CAUSE_UPDATE_RELAUNCH,
)

ACTION = "startup-ir1-" + "a" * 64


def _pin(**kw) -> ParticipantPin:
    base = dict(
        lane_id="issue_14741", role="gateway", provider="codex",
        assigned_name="mzb1_ws_codex_gateway", old_locator="ws:p1",
    )
    base.update(kw)
    return ParticipantPin(**base)


def _evidenced(cause: str = LAUNCH_CAUSE_UPDATE_RELAUNCH) -> ParticipantPin:
    return _pin(
        evidence_workspace_id="ws",
        evidence_startup_action_id=ACTION,
        evidence_cause=cause,
    )


class CauseExtractionTest(unittest.TestCase):
    """What the live port is willing to arm on."""

    def test_a_legacy_participant_launches_unarmed(self) -> None:
        self.assertEqual(launch_cause_for_pin(_pin()), LAUNCH_CAUSE_GENERIC_FRESH)

    def test_the_exact_update_token_arms(self) -> None:
        self.assertEqual(launch_cause_for_pin(_evidenced()), LAUNCH_CAUSE_UPDATE_RELAUNCH)

    def test_anything_else_refuses_rather_than_normalising(self) -> None:
        """A value nobody recorded must not become the value a fence arms on."""

        class _Subclass(str):
            def __new__(cls):
                return super().__new__(cls, LAUNCH_CAUSE_UPDATE_RELAUNCH)

        for label, value in (
            ("padded", " " + LAUNCH_CAUSE_UPDATE_RELAUNCH + " "),
            ("an unknown word", "whatever"),
            ("a str subclass", _Subclass()),
            ("a number", 7),
            ("a bool", True),
            ("absent", None),
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    launch_cause_for_pin(SimpleNamespace(evidence_cause=value)), ""
                )


class LivePortRefusalTest(unittest.TestCase):
    """An unusable cause is decided BEFORE the first Herdr write."""

    def test_an_unusable_cause_never_constructs_the_actuator(self) -> None:
        import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_stale_worker_recovery_live as live

        constructed = []
        original = live.HerdrSublaneActuatorOps

        class _Recording(original):
            def __init__(self, **kwargs):  # pragma: no cover - must not run
                constructed.append(kwargs)
                super().__init__(**kwargs)

        port = live.LiveRecoveryActuatorPort.__new__(live.LiveRecoveryActuatorPort)
        object.__setattr__(port, "repo_root", Path("/repo"))
        object.__setattr__(port, "request", SimpleNamespace(lane="l", issue="1", journal="2"))
        object.__setattr__(port, "env", {})
        object.__setattr__(port, "runner", None)
        object.__setattr__(port, "timeout", 1.0)
        live.HerdrSublaneActuatorOps = _Recording
        try:
            outcome = port.launch_action_bound(
                "a:gen1", SimpleNamespace(
                    evidence_cause="whatever", assigned_name="a", old_locator="w:1",
                    provider="codex",
                ),
            )
        finally:
            live.HerdrSublaneActuatorOps = original
        self.assertEqual(outcome, LAUNCH_ERROR)
        self.assertEqual(constructed, [], "zero Herdr writes: nothing was constructed")
        self.assertNotIn("whatever", str(port.launch_failure_reason))


class ActuatorOpsDefaultTest(unittest.TestCase):
    """Every existing caller omits the field, so every existing launch stays unarmed."""

    def test_the_default_cause_is_the_unarmed_one(self) -> None:
        ops = HerdrSublaneActuatorOps(
            repo_root=Path("/repo"), lane_label="l", issue="1", journal="2", env={},
        )
        self.assertEqual(ops.replacement_launch_cause, LAUNCH_CAUSE_GENERIC_FRESH)

    def test_the_cause_is_carried_when_a_recovery_names_one(self) -> None:
        ops = HerdrSublaneActuatorOps(
            repo_root=Path("/repo"), lane_label="l", issue="1", journal="2", env={},
            replacement_launch_cause=LAUNCH_CAUSE_UPDATE_RELAUNCH,
        )
        self.assertEqual(ops.replacement_launch_cause, LAUNCH_CAUSE_UPDATE_RELAUNCH)


class PropagationTest(unittest.TestCase):
    """The token really arrives at the provider preflight, on both replacement shapes."""

    def _capture(self, *, admission_lock_held: bool, launch_cause=None):
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start as start
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding as binding

        seen = {}

        def _fake(**kwargs):
            seen.update(kwargs)
            raise _Stop()

        class _Stop(Exception):
            pass

        original_locked = start._prepare_session_locked
        original_plain = start.prepare_session
        start._prepare_session_locked = _fake
        start.prepare_session = _fake
        try:
            kwargs = dict(
                worktree_path="/repo", config_repo_root=Path("/repo"),
                providers=["codex"], lane_id="l", env={}, runner=None, timeout=1.0,
                replacement_action_id="a:gen1", admission_lock_held=admission_lock_held,
            )
            if launch_cause is not None:
                kwargs["launch_cause"] = launch_cause
            try:
                binding.prepare_actuator_lane_session(**kwargs)
            except _Stop:
                pass
        finally:
            start._prepare_session_locked = original_locked
            start.prepare_session = original_plain
        return seen

    def test_the_update_cause_reaches_the_session_start_on_both_rails(self) -> None:
        for admission_lock_held in (True, False):
            with self.subTest(locked=admission_lock_held):
                seen = self._capture(
                    admission_lock_held=admission_lock_held,
                    launch_cause=LAUNCH_CAUSE_UPDATE_RELAUNCH,
                )
                self.assertEqual(seen["launch_cause"], LAUNCH_CAUSE_UPDATE_RELAUNCH)

    def test_an_omitted_cause_is_the_unarmed_default(self) -> None:
        for admission_lock_held in (True, False):
            with self.subTest(locked=admission_lock_held):
                seen = self._capture(admission_lock_held=admission_lock_held)
                self.assertEqual(seen["launch_cause"], LAUNCH_CAUSE_GENERIC_FRESH)

    def _reach_preflight(self, **kwargs):
        """Drive the REAL `_prepare_session_locked` until the provider preflight is called.

        A source-string assertion would be green for a hop that is never taken, so the last
        hop is taken: only the preflight itself is seamed, and it records what it was handed.
        """
        import tempfile
        from types import SimpleNamespace

        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start as start

        class _Stop(Exception):
            pass

        seen = {}
        home = Path(tempfile.mkdtemp())
        herdr = home / "herdr"
        herdr.write_text("#!/bin/sh\nexit 0\n")
        herdr.chmod(0o755)

        def _spy(providers, env, **kw):
            seen.update(kw)
            raise _Stop()

        original = start.preflight_launch_providers
        start.preflight_launch_providers = _spy
        try:
            with self.assertRaises(_Stop):
                start._prepare_session_locked(
                    repo_root=Path(tempfile.mkdtemp()),
                    providers=["codex"],
                    lane_id="l",
                    env={
                        "MOZYO_HERDR_BINARY": str(herdr),
                        "MOZYO_BRIDGE_HOME": str(home),
                    },
                    runner=lambda *a, **k: SimpleNamespace(
                        returncode=0, stdout="[]", stderr=""
                    ),
                    timeout=1.0,
                    dry_run=False,
                    **kwargs,
                )
        finally:
            start.preflight_launch_providers = original
        return seen

    def test_the_update_cause_really_arrives_at_the_provider_preflight(self) -> None:
        seen = self._reach_preflight(launch_cause=LAUNCH_CAUSE_UPDATE_RELAUNCH)
        self.assertEqual(seen["launch_cause"], LAUNCH_CAUSE_UPDATE_RELAUNCH)

    def test_an_ordinary_launch_arrives_unarmed(self) -> None:
        """The byte-invariance half: a caller that names no cause arms nothing."""
        seen = self._reach_preflight()
        self.assertEqual(seen["launch_cause"], LAUNCH_CAUSE_GENERIC_FRESH)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
