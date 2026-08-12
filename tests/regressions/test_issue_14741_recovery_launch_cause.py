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
from mozyo_bridge.core.state.herdr_session_start_gate import (  # noqa: E402
    session_start_gate,
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
from tests.support.herdr_fake import FakeHerdr  # noqa: E402

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

    def _capture_through_binding(self, *, admission_lock_held: bool, launch_cause=None):
        """Drive the binding rail and capture what the SESSION START entry receives.

        Only the innermost entry is seamed. The public `prepare_session` is NOT replaced --
        the first cut faked it, so the very transition it claimed to prove was skipped and a
        dropped kwarg stayed green (audit j#97177 F1).
        """
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start as start
        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_v1_replacement_binding as binding

        class _Stop(Exception):
            pass

        seen = {}

        def _fake_locked(**kwargs):
            seen.update(kwargs)
            raise _Stop()

        original = start._prepare_session_locked
        start._prepare_session_locked = _fake_locked
        try:
            kwargs = dict(
                worktree_path="/repo", config_repo_root=Path("/repo"),
                providers=["codex"], lane_id="l", env={}, runner=None, timeout=1.0,
                replacement_action_id="a:gen1", admission_lock_held=admission_lock_held,
                dry_run=True,
            )
            if launch_cause is not None:
                kwargs["launch_cause"] = launch_cause
            try:
                binding.prepare_actuator_lane_session(
                    **{k: v for k, v in kwargs.items() if k != "dry_run"}
                )
            except _Stop:
                pass
        finally:
            start._prepare_session_locked = original
        return seen

    def _capture_through_public_wrapper(self, **kwargs):
        """Run the REAL public `prepare_session` and capture the inner call's kwargs.

        `dry_run=True` keeps it on the no-lock path and stops it before any side effect,
        while still going through the public wrapper's own argument binding and call dict --
        which is where the token was being dropped.
        """
        import stat
        import tempfile

        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start as start

        seen = {}
        with tempfile.TemporaryDirectory() as task_root:
            binary = Path(task_root) / "herdr"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
            original = start._prepare_session_locked
            start._prepare_session_locked = lambda **kw: seen.update(kw)
            try:
                start.prepare_session(
                    repo_root=Path(task_root), providers=["codex"], lane_id="l",
                    env={"MOZYO_HERDR_BINARY": str(binary)},
                    runner=FakeHerdr().run,
                    dry_run=True,
                    **kwargs,
                )
            finally:
                start._prepare_session_locked = original
        return seen

    def test_the_binding_rail_hands_the_cause_to_the_locked_entry(self) -> None:
        """The lock-held rail, actually driven.

        Scoped to `admission_lock_held=True`: the no-lock rail goes through the public
        `prepare_session`, which acquires a real lock on a real home, so it is covered by
        the public-wrapper tests below instead of being driven from here. Measured: this
        path does NOT catch a kwarg dropped at the public wrapper's call dict, so it is not
        offered as evidence for that hop -- only for this one.
        """
        seen = self._capture_through_binding(
            admission_lock_held=True, launch_cause=LAUNCH_CAUSE_UPDATE_RELAUNCH
        )
        self.assertEqual(seen["launch_cause"], LAUNCH_CAUSE_UPDATE_RELAUNCH)

    def test_the_binding_rail_defaults_to_unarmed(self) -> None:
        seen = self._capture_through_binding(admission_lock_held=True)
        self.assertEqual(seen["launch_cause"], LAUNCH_CAUSE_GENERIC_FRESH)

    def test_the_public_wrapper_forwards_an_armed_cause(self) -> None:
        seen = self._capture_through_public_wrapper(
            launch_cause=LAUNCH_CAUSE_UPDATE_RELAUNCH
        )
        self.assertEqual(seen.get("launch_cause"), LAUNCH_CAUSE_UPDATE_RELAUNCH)

    def test_the_public_wrapper_defaults_to_unarmed(self) -> None:
        seen = self._capture_through_public_wrapper()
        self.assertEqual(seen.get("launch_cause"), LAUNCH_CAUSE_GENERIC_FRESH)

    def _reach_preflight(self, **kwargs):
        """Drive the REAL `_prepare_session_locked` until the provider preflight is called.

        A source-string assertion would be green for a hop that is never taken, so the last
        hop is taken: only the preflight itself is seamed, and it records what it was handed.

        The `env=` argument is what the *rail* carries, not what the process resolves:
        on the non-dry-run path `_prepare_session_locked` calls
        `register_workspace(repo_root)` with no `home=`, so the home contract is read
        from `os.environ`. Passing `MOZYO_BRIDGE_HOME` only in the kwarg therefore
        registered a throwaway workspace into the operator's live registry on every
        invocation -- two rows per run, which is the producer #14757 j#100381
        identified in the real registry. The process environment is pinned here as
        well, and `TMPDIR` with it so the temp dirs stay inside the same task root.
        """
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch

        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start as start

        class _Stop(Exception):
            pass

        seen = {}

        def _spy(providers, env, **kw):
            seen.update(kw)
            raise _Stop()

        with tempfile.TemporaryDirectory() as task_root:
            home = Path(task_root) / "mozyo-home"
            tmp = Path(task_root) / "tmp"
            repo = Path(task_root) / "repo"
            for directory in (home, tmp, repo):
                directory.mkdir(parents=True)
            herdr = home / "herdr"
            herdr.write_text("#!/bin/sh\nexit 0\n")
            herdr.chmod(0o755)

            original = start.preflight_launch_providers
            start.preflight_launch_providers = _spy
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "MOZYO_BRIDGE_HOME": str(home),
                        "TMPDIR": str(tmp),
                        "TMP": str(tmp),
                        "TEMP": str(tmp),
                    },
                ), session_start_gate(home, exclusive=False) as lease:
                    with self.assertRaises(_Stop):
                        start._prepare_session_locked(
                            repo_root=repo,
                            providers=["codex"],
                            lane_id="l",
                            env={
                                "MOZYO_HERDR_BINARY": str(herdr),
                                "MOZYO_BRIDGE_HOME": str(home),
                            },
                            runner=FakeHerdr().run,
                            timeout=1.0,
                            dry_run=False,
                            _session_gate_lease=lease,
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
