"""Redmine #15192 — macOS and Linux register ONE OS-native tick behind ONE CLI contract.

#15183 gave Linux a systemd user service + timer, but left the two hosts visibly different: macOS
still registered TWO LaunchAgents (a `--run-once` reconcile agent and a `--drain-only` drain agent,
installed as an atomic pair), the OS cadence was a Linux-only 60s knob that `--tick-interval`
silently dropped on macOS, and `--service-status` answered a different shape per host.

These pin the acceptance contract as the issue body defines it. They are deliberately written
against the operator-facing surface (the CLI payload and the adapters' owned identities) rather than
adapter internals, because the acceptance is about what an operator sees and manages:

1. exactly ONE OS registration per host, running the bounded `workflow supervisor --run-once`;
2. `--drain-only` / `--watch` remain reachable as manual / event-driven entry points, and NEITHER is
   registered with an OS scheduler;
3. one interval surface (`--tick-interval`) with one measured portable default, strictly finer than
   the supervisor body's own ~300s provider cadence, which stays 300s;
4. the same status vocabulary on both hosts (実行内容 / 次回起動 / 直近の終了結果);
5. an unsupported host is a typed zero-mutation refusal on every verb.

Both host adapters are exercised from this Linux runner through the platform seam the backend
resolves on, with fake runners standing in for `launchctl` / `systemctl`: no host is mutated.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser

# Both OS adapters are imported EAGERLY, here, on purpose. Several tests below patch
# ``sys.platform`` to ``darwin`` on this Linux runner; if the launchd module were first imported
# under that patch, stdlib ``urllib.request`` would follow it and try the macOS-only ``_scproxy``,
# failing the test for a reason that has nothing to do with the contract under test. Importing
# before any patch keeps the platform seam a *behaviour* switch, not an import-time one.
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    supervisor_launchd as sl,
    supervisor_service_backend as sb,
    supervisor_systemd as ss,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (
    DEFAULT_OS_TICK_INTERVAL_SECONDS,
    DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
)


def _run(argv) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


class _HostCase(unittest.TestCase):
    """An isolated mozyo home; every host call goes through a fake runner."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = str(Path(tmp.name))

    def _status(self, platform: str) -> dict:
        def _fake_run(argv, *a, **k):
            # `systemctl --user show --property=Version` must succeed or the Linux adapter reports
            # an unreachable user manager; everything else is an empty, successful read.
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(sys, "platform", platform), patch(
            "subprocess.run", _fake_run
        ):
            rc, out = _run(
                ["workflow", "supervisor", "--service-status", "--home", self.home, "--json"]
            )
        self.assertEqual(rc, 0, platform)
        return json.loads(out)


class OneRegistrationPerHostTest(_HostCase):
    """Acceptance 1 / 2: one owned registration per host, and it runs the bounded sweep."""

    def test_each_host_owns_exactly_one_service(self) -> None:
        for platform, backend in (("darwin", sb.BACKEND_LAUNCHD), ("linux", sb.BACKEND_SYSTEMD)):
            payload = self._status(platform)
            self.assertEqual(payload["backend"], backend, platform)
            self.assertEqual(len(payload["agents"]), 1, platform)
            self.assertEqual(len(payload["definitions"]), 1, platform)

    def test_the_owned_registration_runs_run_once_on_both_hosts(self) -> None:
        self.assertEqual(sl.SUPERVISOR_AGENT.argv_tail, ("workflow", "supervisor", "--run-once"))
        self.assertEqual(ss.SUPERVISOR_UNIT.argv_tail, ("workflow", "supervisor", "--run-once"))
        for platform in ("darwin", "linux"):
            payload = self._status(platform)
            self.assertEqual(payload["definitions"][0]["command"][-1], "--run-once", platform)

    def test_no_host_registers_a_drain_or_watch_timer(self) -> None:
        # Acceptance 3: they stay MANUAL / event-driven entry points. A registered drain unit is
        # what #15192 retired; a registered `--watch` would be a resident process by another name.
        #
        # Asserted against what is REGISTERED — the owned argv tails and the owned definition roster
        # — not against a substring of the whole payload. A blob grep also matches prose that
        # correctly *explains* the retirement, so it would fail on an accurate note while still
        # passing on a real regression that used a different spelling.
        self.assertNotIn("--drain-only", sl.SUPERVISOR_AGENT.argv_tail)
        self.assertNotIn("--drain-only", ss.SUPERVISOR_UNIT.argv_tail)
        self.assertNotIn("--watch", sl.SUPERVISOR_AGENT.argv_tail)
        self.assertNotIn("--watch", ss.SUPERVISOR_UNIT.argv_tail)
        for platform in ("darwin", "linux"):
            payload = self._status(platform)
            registered = [d["command"][-1] for d in payload["definitions"]]
            self.assertEqual(registered, ["--run-once"], platform)
            # The retired drain key may exist for compatibility, but must never claim registration.
            self.assertFalse(payload["drain_definition"]["registered"], platform)

    def test_the_manual_entry_points_are_still_reachable(self) -> None:
        # Retiring the drain REGISTRATION must not retire the drain ACTION.
        parser = build_parser()
        for flag in ("--drain-only", "--watch"):
            args = parser.parse_args(["workflow", "supervisor", flag, "--home", self.home])
            self.assertTrue(getattr(args, flag.lstrip("-").replace("-", "_")), flag)

    def test_no_host_declares_keep_alive_or_a_resident_loop(self) -> None:
        for platform in ("darwin", "linux"):
            payload = self._status(platform)
            self.assertFalse(payload["definitions"][0]["keep_alive"], platform)
            self.assertFalse(payload["agents"][0]["keep_alive_present"], platform)


class OneIntervalSurfaceTest(_HostCase):
    """Acceptance 6 / 7: one cadence knob, one measured default, provider cadence untouched."""

    def test_both_adapters_default_to_the_same_portable_tick(self) -> None:
        self.assertEqual(sl.SUPERVISOR_AGENT.default_interval_seconds, DEFAULT_OS_TICK_INTERVAL_SECONDS)
        self.assertEqual(ss.DEFAULT_TICK_INTERVAL_SECONDS, DEFAULT_OS_TICK_INTERVAL_SECONDS)

    def test_the_portable_default_is_the_measured_value(self) -> None:
        # 180s, decided on measured tick cost and recovery latency (#15192), NOT inherited from the
        # retired drain agent's 60s. Pinned so a silent revert fails here.
        self.assertEqual(DEFAULT_OS_TICK_INTERVAL_SECONDS, 180)

    def test_the_tick_is_strictly_finer_than_the_unchanged_provider_cadence(self) -> None:
        self.assertEqual(DEFAULT_RECONCILIATION_INTERVAL_SECONDS, 300)
        self.assertLess(DEFAULT_OS_TICK_INTERVAL_SECONDS, DEFAULT_RECONCILIATION_INTERVAL_SECONDS)

    def test_status_surfaces_the_provider_cadence_on_both_hosts(self) -> None:
        # So an operator can see the OS tick is not a Redmine poll.
        for platform in ("darwin", "linux"):
            payload = self._status(platform)
            self.assertEqual(
                payload["agents"][0]["provider_reconcile_interval_seconds"],
                DEFAULT_RECONCILIATION_INTERVAL_SECONDS,
                platform,
            )

    def test_the_tick_interval_flag_reaches_both_adapters(self) -> None:
        for platform, adapter in (("darwin", sl), ("linux", ss)):
            seen = {}

            def _fake_install(**kwargs):
                seen.update(kwargs)
                return {"action": "install", "performed": True, "reason": "", "label": "L"}

            with patch.object(sys, "platform", platform), patch.object(
                adapter, "install", _fake_install
            ):
                sb.install(interval_seconds=90)
            self.assertEqual(seen.get("interval_seconds"), 90, platform)


class CommonStatusVocabularyTest(_HostCase):
    """Acceptance 4: 実行内容 / 次回起動 / 直近の終了結果 mean the same thing on both hosts."""

    #: The keys the acceptance requires status to answer, in the same words, on every host.
    CONTRACT_KEYS = (
        "installed", "loaded", "pid", "scheduled_interval_seconds", "home_pin",
        "executable_matches", "keep_alive_present", "no_environment_block",
        "credential_readiness", "installed_command", "next_elapse", "next_elapse_basis",
        "last_result", "last_exit_status", "last_exit_at",
        "provider_reconcile_interval_seconds",
    )

    def test_both_hosts_answer_the_same_status_keys(self) -> None:
        for platform in ("darwin", "linux"):
            row = self._status(platform)["agents"][0]
            for key in self.CONTRACT_KEYS:
                self.assertIn(key, row, f"{platform}:{key}")

    def test_the_shared_status_tokens_are_identical_on_both_adapters(self) -> None:
        # Drift guards, not redundancy: neither OS adapter imports the other, and callers read a
        # BACKEND-normalized row (which may come from either) while comparing against one adapter's
        # constants — e.g. the offline-rollout readback checks `credential_readiness` / `home_pin`
        # using the launchd names. Identical literals are what makes that correct, so pin them.
        self.assertEqual(sl.NEXT_ELAPSE_UNKNOWN, ss.NEXT_ELAPSE_UNKNOWN)
        self.assertEqual(sl.CREDENTIAL_READY, ss.CREDENTIAL_READY)
        self.assertEqual(sl.CREDENTIAL_MISSING, ss.CREDENTIAL_MISSING)
        self.assertEqual(sl.CREDENTIAL_INCOMPLETE, ss.CREDENTIAL_INCOMPLETE)
        self.assertEqual(sl.CREDENTIAL_UNSAFE, ss.CREDENTIAL_UNSAFE)
        self.assertEqual(sl.HOME_PIN_OK, ss.HOME_PIN_OK)
        self.assertEqual(sl.HOME_PIN_NOT_INSTALLED, ss.HOME_PIN_NOT_INSTALLED)

    def test_status_emits_no_credential_on_either_host(self) -> None:
        for platform in ("darwin", "linux"):
            blob = json.dumps(self._status(platform)).lower()
            self.assertNotIn("api_key", blob, platform)
            self.assertNotIn("x-redmine-api-key", blob, platform)


class CredentialContractParityTest(unittest.TestCase):
    """Review j#102151 Finding 4: `install` must mean the same thing on both hosts.

    macOS used to refuse the install outright on a non-ready Redmine credential while Linux
    installed and projected the readiness. That is not an internals difference — it is the
    operator-visible answer to "can I install this?", which is precisely what #15192 unifies. The
    macOS gate was inherited from #13683, when a supervisor tick meant nothing but a Redmine
    reconciliation; since #14150 a tick does real local work with no provider at all.
    """

    #: Every non-ready state, so the parity is asserted across the whole matrix, not one sample.
    NON_READY = ("missing", "incomplete", "unsafe")

    def test_neither_adapter_gates_install_on_credential_readiness(self) -> None:
        # Asserted structurally against BOTH adapters: no install path may turn a readiness token
        # into a refusal. A reintroduced gate on either host re-splits the contract.
        import inspect

        for adapter in (sl, ss):
            source = inspect.getsource(adapter.install)
            self.assertNotIn("_CREDENTIAL_REFUSAL_REASON", source, adapter.__name__)
            self.assertNotIn("!= CREDENTIAL_READY", source, adapter.__name__)

    def test_the_readiness_vocabulary_is_identical_and_complete_on_both(self) -> None:
        for token in self.NON_READY:
            self.assertIn(token, (sl.CREDENTIAL_MISSING, sl.CREDENTIAL_INCOMPLETE, sl.CREDENTIAL_UNSAFE))
            self.assertIn(token, (ss.CREDENTIAL_MISSING, ss.CREDENTIAL_INCOMPLETE, ss.CREDENTIAL_UNSAFE))
        self.assertEqual(sl.CREDENTIAL_READY, ss.CREDENTIAL_READY)

    def test_no_adapter_reintroduces_a_credential_refusal_token(self) -> None:
        # The old tokens named the gate. If one comes back as a module constant, the gate came back.
        for adapter in (sl, ss):
            for retired in ("redmine_credential_missing", "redmine_credential_incomplete",
                            "redmine_credential_unsafe"):
                self.assertNotIn(
                    retired,
                    {v for v in vars(adapter).values() if isinstance(v, str)},
                    f"{adapter.__name__}:{retired}",
                )


class OfflineRolloutSeamTest(_HostCase):
    """Review j#102151 Finding 2: the capture side and the plan side must agree on the roster size.

    The original defect was a *seam* defect, not a logic one: each half was internally consistent —
    the snapshot read the backend's one-row roster, the plan required the retired two-label set — so
    both halves' own tests passed while every real post-migration capture was refused. These tests
    therefore run the REAL producer into the REAL validator instead of hand-writing a roster, which
    is the only shape that would have caught it.
    """

    def _observed_labels(self, platform: str) -> list:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_offline_rollout_snapshot import (  # noqa: E501
            _supervisor_snapshots,
            read_supervisor_status,
        )

        def _fake_run(argv, *a, **k):
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(sys, "platform", platform), patch("subprocess.run", _fake_run):
            snapshots = _supervisor_snapshots(Path(self.home), read_supervisor_status)
        return [s.label for s in snapshots]

    def test_the_captured_roster_satisfies_the_plan_contract_on_both_hosts(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
            OWNED_SUPERVISOR_LABELS,
        )

        for platform in ("darwin", "linux"):
            labels = self._observed_labels(platform)
            self.assertEqual(len(labels), 1, platform)
            # The exact predicate the plan validator applies. Asserting the SET equality (not just
            # the count) is what ties the two halves together.
            self.assertEqual(set(labels), set(OWNED_SUPERVISOR_LABELS), platform)

    def test_the_capture_side_names_the_same_authority_the_plan_side_enforces(self) -> None:
        # Deliberately NOT a re-implementation of the plan's predicate here: a test that copies the
        # rule cannot detect the rule drifting. The producer's output is compared against the
        # validator's own constant, and the validator's ACCEPTANCE of that roster is pinned where
        # the real `build_offline_rollout_plan` runs against a full valid capture —
        # `test_offline_rollout_plan.test_a_single_owned_supervisor_capture_plans_successfully`.
        # Together the two cover producer -> authority -> validator with no duplicated logic.
        from mozyo_bridge.e_110_execution_platform.f_160_state_store_managed_events.domain.offline_rollout_plan import (  # noqa: E501
            OWNED_SUPERVISOR_LABELS,
        )

        self.assertEqual(len(OWNED_SUPERVISOR_LABELS), 1)
        self.assertNotIn("org.mozyo-bridge.callback-supervisor.drain", OWNED_SUPERVISOR_LABELS)


class UnsupportedHostTest(_HostCase):
    """Acceptance 9: a host with no owned scheduler adapter is a typed zero-mutation refusal."""

    def test_every_mutating_verb_refuses_with_a_typed_token(self) -> None:
        for verb in ("--install", "--restart", "--uninstall"):
            with patch.object(sys, "platform", "win32"):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
            payload = json.loads(out)
            self.assertEqual(rc, 1, verb)
            self.assertFalse(payload["performed"], verb)
            self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED, verb)
            self.assertEqual(payload["reason"], sb.REASON_NO_BACKEND, verb)
            self.assertEqual(payload["agents"], [], verb)

    def test_an_unsupported_host_owns_nothing_and_declares_nothing(self) -> None:
        payload = self._status("win32")
        self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED)
        self.assertEqual(payload["agents"], [])
        self.assertEqual(payload["definitions"], [])


if __name__ == "__main__":
    unittest.main()
