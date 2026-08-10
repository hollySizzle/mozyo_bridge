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
import plistlib
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


class ProbeStateProjectionTest(unittest.TestCase):
    """Review j#102200 finding r3f2: status must distinguish a verified state from an unreadable one.

    Both hosts collapsed a failed read into the same shape a successful "not running" read produces,
    so an operator could not tell "confirmed stopped" from "I could not look". The projection now
    carries a fixed-vocabulary ``probe_state`` — and it must carry the SAME vocabulary on both, or
    the common CLI contract is split again by the very key that was added to unify it.
    """

    def test_the_probe_vocabulary_is_identical_on_both_adapters(self) -> None:
        # A drift guard, not an import: neither OS adapter imports the other.
        self.assertEqual(sl.PROBE_LOADED, ss.PROBE_LOADED)
        self.assertEqual(sl.PROBE_CONFIRMED_ABSENT, ss.PROBE_CONFIRMED_ABSENT)
        self.assertEqual(sl.PROBE_UNREADABLE, ss.PROBE_UNREADABLE)

    def test_macos_status_separates_confirmed_absence_from_an_unreadable_read(self) -> None:
        owned = sl.SUPERVISOR_AGENT.label
        denied = _launchd_status(_fake_result(113, stderr="Operation not permitted"))
        absent = _launchd_status(
            _fake_result(113, stderr=f'Could not find service "{owned}" in domain for gui')
        )
        # The exact defect: these two used to be byte-identical dictionaries.
        self.assertNotEqual(denied, absent)
        self.assertEqual(denied["probe_state"], sl.PROBE_UNREADABLE)
        self.assertEqual(absent["probe_state"], sl.PROBE_CONFIRMED_ABSENT)
        # ...and neither claims the service is running.
        self.assertFalse(denied["loaded"])
        self.assertFalse(absent["loaded"])

    def test_linux_status_separates_an_unreadable_manager_from_a_read_unit(self) -> None:
        readable = _systemd_status(manager_available=True)
        unreadable = _systemd_status(manager_available=False)
        self.assertNotEqual(readable, unreadable)
        self.assertEqual(unreadable["probe_state"], ss.PROBE_UNREADABLE)
        self.assertIn(
            readable["probe_state"], (ss.PROBE_LOADED, ss.PROBE_CONFIRMED_ABSENT)
        )

    def test_a_partial_linux_read_is_unreadable_not_a_confirmed_state(self) -> None:
        # Review j#102235 finding r4f2. Reading SOME property is not reading the SCHEDULE state: a
        # timer answer carrying only `UnitFileState` was reported as `confirmed_absent`, asserting a
        # fact nothing in that response established.
        for timer_output, why in (
            ("UnitFileState=enabled\n", "no ActiveState at all"),
            ("ActiveState=\nUnitFileState=enabled\n", "ActiveState present but empty"),
        ):
            status = _systemd_status(manager_available=True, timer_output=timer_output)
            self.assertEqual(status["probe_state"], ss.PROBE_UNREADABLE, why)

    def test_every_active_state_is_classified_by_a_closed_vocabulary(self) -> None:
        # Review j#102309 finding r5f2. "active versus everything else" is an OPEN negation: it
        # asserted confirmed absence for `reloading` (which systemd defines as active), for both
        # transition states, and for any value this code has never heard of. Absence is claimed only
        # for the two values that mean it; everything unrecognized is unreadable.
        expected = {
            "active": ss.PROBE_LOADED,
            "reloading": ss.PROBE_LOADED,          # active, reloading its configuration
            "inactive": ss.PROBE_CONFIRMED_ABSENT,
            "failed": ss.PROBE_CONFIRMED_ABSENT,
            "activating": ss.PROBE_UNREADABLE,     # mid-transition: not a confirmed state
            "deactivating": ss.PROBE_UNREADABLE,
            "maintenance": ss.PROBE_UNREADABLE,    # documented elsewhere, unknown to this code
            "some-future-state": ss.PROBE_UNREADABLE,
        }
        for state, want in expected.items():
            status = _systemd_status(
                manager_available=True, timer_output=f"ActiveState={state}\n"
            )
            self.assertEqual(status["probe_state"], want, state)

    def test_the_closed_vocabulary_is_matched_case_sensitively(self) -> None:
        # Review j#102327 finding r6f2. The value was lower-cased before the lookup, which reopened
        # the vocabulary the table above closes: `INACTIVE` reported a confirmed absence and
        # `ACTIVE` a confirmed run, though systemd's D-Bus interface enumerates `ActiveState` as
        # lowercase literals and neither spelling is a token this code was told the meaning of.
        for state in ("ACTIVE", "Active", "RELOADING", "INACTIVE", "Inactive", "FAILED"):
            status = _systemd_status(
                manager_available=True, timer_output=f"ActiveState={state}\n"
            )
            self.assertEqual(status["probe_state"], ss.PROBE_UNREADABLE, state)
    def test_the_closed_vocabulary_is_matched_without_trimming(self) -> None:
        # Review j#102378 finding r7f2 — and a correction to this file. The sibling test above was
        # added in R6 asserting that `ActiveState=  active  ` reads as LOADED, on the reasoning that
        # the padding was framing the `key=value` parse had introduced. That reasoning was wrong:
        # `splitlines` has already removed the terminator, so everything after the first `=` is the
        # manager's answer, and systemd enumerates its states as bare lowercase literals. A padded
        # value is therefore a value this code has not been told the meaning of — the unknown case.
        # A green regression test is not evidence its expectation was right.
        for state, why in (
            ("  active  ", "spaces around a known running state"),
            (" inactive ", "spaces around a known absent state"),
            ("\tfailed", "a tab before a known absent state"),
            ("active ", "one trailing space"),
        ):
            status = _systemd_status(
                manager_available=True, timer_output=f"ActiveState={state}\n"
            )
            self.assertEqual(status["probe_state"], ss.PROBE_UNREADABLE, why)
        # The complement: the exact tokens still classify.
        self.assertEqual(
            _systemd_status(
                manager_available=True, timer_output="ActiveState=active\n"
            )["probe_state"],
            ss.PROBE_LOADED,
        )

    def test_neither_adapter_folds_case_to_reach_a_confirmed_state(self) -> None:
        # The cross-adapter shape of r6f1 / r6f2: one is an identity (a launchd label), the other a
        # state token, and folding either one turned an unrecognized input into a confirmed fact —
        # on macOS into an absence that authorizes deleting a registration.
        owned = sl.LEGACY_DRAIN_AGENT.label
        macos = _launchd_status(
            _fake_result(113, stderr=f'Could not find service "{owned.upper()}" in domain')
        )
        self.assertEqual(macos["probe_state"], sl.PROBE_UNREADABLE)
        linux = _systemd_status(manager_available=True, timer_output="ActiveState=INACTIVE\n")
        self.assertEqual(linux["probe_state"], ss.PROBE_UNREADABLE)

    def test_loaded_and_probe_state_never_disagree(self) -> None:
        # One state machine, one answer: `loaded` is derived from the same classification, so the
        # projection cannot say "not running" while the state token says otherwise (and vice versa).
        for state in ("active", "reloading", "activating", "deactivating", "inactive",
                      "failed", "some-future-state", ""):
            status = _systemd_status(
                manager_available=True, timer_output=f"ActiveState={state}\n"
            )
            self.assertEqual(
                status["loaded"], status["probe_state"] == ss.PROBE_LOADED, state
            )

    def test_the_projection_leaks_no_raw_manager_text(self) -> None:
        # `probe_state` is a fixed token, never the launchctl / systemctl message it came from.
        status = _launchd_status(_fake_result(113, stderr="Operation not permitted"))
        self.assertNotIn("not permitted", json.dumps(status).lower())
        self.assertIn(
            status["probe_state"],
            (sl.PROBE_LOADED, sl.PROBE_CONFIRMED_ABSENT, sl.PROBE_UNREADABLE),
        )


def _fake_result(returncode: int, stdout: str = "", stderr: str = ""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def _launchd_status(print_result) -> dict:
    """A macOS status projection with an owned plist installed and ``print`` scripted."""
    os_home = Path(tempfile.mkdtemp())
    mozyo_home = Path(tempfile.mkdtemp())
    target = sl.plist_path(os_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once",
        "--home", str(sl.resolve_mozyo_home(mozyo_home)),
    ]
    target.write_bytes(sl.render_plist(argv, interval_seconds=180, os_home=os_home))

    def runner(command):
        return print_result if list(command)[1] == "print" else _fake_result(0)

    with patch.object(sl, "_running_on_darwin", return_value=True):
        return sl.service_status(
            os_home=os_home, mozyo_home=mozyo_home, runner=runner,
            which=lambda _n: "/opt/bin/mozyo-bridge",
        )


def _systemd_status(*, manager_available: bool, timer_output: str = "ActiveState=inactive\n") -> dict:
    """A Linux status projection where the user manager is reachable, or is not."""
    os_home = Path(tempfile.mkdtemp())
    mozyo_home = Path(tempfile.mkdtemp())

    def runner(command):
        argv = list(command)
        if argv[2] == "show" and any(a.startswith("--property=Version") for a in argv):
            return _fake_result(0 if manager_available else 1)
        if argv[2] == "show":
            return _fake_result(0 if manager_available else 1, timer_output)
        return _fake_result(0)

    with patch.object(sys, "platform", "linux"):
        return ss.service_status(
            os_home=os_home, mozyo_home=mozyo_home, runner=runner,
            which=lambda _n: "/opt/bin/mozyo-bridge",
        )


class LegacyMigrationAuthorityTest(unittest.TestCase):
    """The retired-agent migration's authority, seen through the operator-facing surfaces.

    Owner delegation j#102452 / gateway disposition j#102458: only a succeeding `bootout` may
    authorize the unlink, and a non-zero one is a typed refusal that keeps the plist. Asserted here
    at the status/envelope level, where an operator would actually observe it.
    """

    def test_status_still_reports_a_retained_legacy_registration(self) -> None:
        # The migration refusing must not make the leftover invisible: it is the operator's cue.
        os_home = Path(tempfile.mkdtemp())
        mozyo_home = Path(tempfile.mkdtemp())
        target = sl.plist_path(os_home, agent=sl.LEGACY_DRAIN_AGENT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            plistlib.dumps({"Label": sl.LEGACY_DRAIN_AGENT.label, "ProgramArguments": ["/x"]})
        )
        with patch.object(sl, "_running_on_darwin", return_value=True):
            status = sl.service_status(
                os_home=os_home, mozyo_home=mozyo_home,
                runner=lambda _c: _fake_result(1), which=lambda _n: "/opt/bin/mozyo-bridge",
            )
        self.assertEqual(status["legacy_drain"], sl.LEGACY_DRAIN_OWNED)

    def test_the_refusal_token_is_typed_and_secret_free(self) -> None:
        self.assertEqual(sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE, "legacy_drain_state_unreadable")
        for noun in ("/users/", "key", "token", "password"):
            self.assertNotIn(noun, sl.REASON_LEGACY_DRAIN_STATE_UNREADABLE.lower())

    def test_every_destructive_refusal_token_is_typed_and_secret_free(self) -> None:
        # Review j#102496 added three tokens to the destructive surface. Same contract as the rest:
        # a fixed lowercase identifier, no host path, no credential noun, no manager wording.
        for token in (
            sl.REASON_PLIST_FOREIGN_LABEL,
            sl.REASON_PLIST_UNREADABLE,
            sl.REASON_BOOTOUT_FAILED,
        ):
            with self.subTest(token=token):
                self.assertRegex(token, r"^[a-z0-9_]+$")
                for noun in ("/users/", "key", "token", "password", "boot-out", "denied"):
                    self.assertNotIn(noun, token.lower())


class DestructiveVerbIdentityFenceTest(unittest.TestCase):
    """Review j#102496 r12f1-r12f3, asserted at the surface an operator observes.

    One rule now covers both LaunchAgents this adapter can touch: a file is mutated only when its own
    ``Label`` says it is ours, checked again at the moment of the mutation, and the owned plist is
    unlinked only after a bootout that actually succeeded.
    """

    def _home(self):
        os_home = Path(tempfile.mkdtemp())
        mozyo_home = Path(tempfile.mkdtemp())
        return os_home, mozyo_home

    def _write(self, target: Path, label: str) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": ["/x"]}))
        return target

    def test_a_stranger_s_plist_at_our_path_survives_install_and_uninstall(self) -> None:
        for verb in ("install", "uninstall"):
            with self.subTest(verb=verb):
                os_home, mozyo_home = self._home()
                foreign = self._write(sl.plist_path(os_home), "com.example.foreign")
                before = foreign.read_bytes()
                with patch.object(sl, "_running_on_darwin", return_value=True):
                    result = (
                        sl.install(
                            os_home=os_home, mozyo_home=mozyo_home,
                            runner=lambda _c: _fake_result(0),
                            which=lambda _n: "/opt/bin/mozyo-bridge",
                        )
                        if verb == "install"
                        else sl.uninstall(os_home=os_home, runner=lambda _c: _fake_result(0))
                    )
                self.assertFalse(result["performed"])
                self.assertEqual(sl.REASON_PLIST_FOREIGN_LABEL, result["reason"])
                self.assertEqual(before, foreign.read_bytes())

    def test_uninstall_keeps_the_owned_plist_when_the_bootout_did_not_succeed(self) -> None:
        os_home, _ = self._home()
        owned = self._write(sl.plist_path(os_home), sl.SUPERVISOR_LAUNCHD_LABEL)
        with patch.object(sl, "_running_on_darwin", return_value=True):
            result = sl.uninstall(os_home=os_home, runner=lambda _c: _fake_result(1))
        self.assertFalse(result["performed"])
        self.assertEqual(sl.REASON_BOOTOUT_FAILED, result["reason"])
        self.assertTrue(owned.exists())

    def test_the_classification_is_the_same_one_for_both_agents(self) -> None:
        # Not two parallel implementations that can drift apart — one function, two callers.
        os_home, _ = self._home()
        for agent in (sl.SUPERVISOR_AGENT, sl.LEGACY_DRAIN_AGENT):
            with self.subTest(agent=agent.label):
                target = sl.plist_path(os_home, agent=agent)
                self.assertEqual(
                    sl.PLIST_ABSENT, sl.classify_agent_plist(os_home, agent=agent)
                )
                self._write(target, agent.label)
                self.assertEqual(sl.PLIST_OWNED, sl.classify_agent_plist(os_home, agent=agent))
                self._write(target, "com.example.other")
                self.assertEqual(sl.PLIST_FOREIGN, sl.classify_agent_plist(os_home, agent=agent))
                target.write_bytes(b"not a plist")
                self.assertEqual(sl.PLIST_UNREADABLE, sl.classify_agent_plist(os_home, agent=agent))


class RestartRefusalParityTest(unittest.TestCase):
    """Review j#102398 finding r9f2: one verb, one meaning, through the common envelope.

    The backend declares `install` / `restart` / `uninstall` / `service_status` identical across
    hosts, so a refusal must mean the same thing on both. macOS collapsed its three-valued probe to a
    bool and answered `service_not_loaded` for BOTH a permission-denied read and a confirmed absence,
    dropping `probe_state` — while Linux had already separated them. Asserted at the **backend
    envelope**, not the adapter, because that is the surface the operator and the CLI actually see.
    """

    #: The refusal vocabulary is a shared contract and names no OS-specific manager noun.
    def test_both_adapters_publish_the_same_refusal_tokens(self) -> None:
        self.assertEqual(sl.REASON_SERVICE_NOT_LOADED, ss.REASON_SERVICE_NOT_LOADED)
        self.assertEqual(sl.REASON_SERVICE_STATE_UNREADABLE, ss.REASON_SERVICE_STATE_UNREADABLE)
        for token in (sl.REASON_SERVICE_STATE_UNREADABLE, sl.REASON_SERVICE_NOT_LOADED):
            for os_noun in ("timer", "launchagent", "plist", "unit", "launchd", "systemd"):
                self.assertNotIn(os_noun, token.lower(), token)

    def test_the_restart_envelope_separates_absent_from_unreadable_on_both_hosts(self) -> None:
        matrix = {
            "darwin": {
                "absent": _fake_result(
                    113,
                    stderr=f'Could not find service "{sl.SUPERVISOR_AGENT.label}" in domain',
                ),
                "unreadable": _fake_result(1, stderr="Operation not permitted"),
            },
            "linux": {
                "absent": "ActiveState=inactive\n",
                "unreadable": "ActiveState=inactive\nActiveState=active\n",
            },
        }
        for platform, cases in matrix.items():
            absent = _restart_envelope(platform, cases["absent"])
            unreadable = _restart_envelope(platform, cases["unreadable"])
            # Both refuse with zero mutation...
            self.assertFalse(absent["performed"], platform)
            self.assertFalse(unreadable["performed"], platform)
            # ...and say WHICH fact refused, in the shared vocabulary.
            self.assertEqual(absent["agents"][0]["reason"], sl.REASON_SERVICE_NOT_LOADED, platform)
            self.assertEqual(
                unreadable["agents"][0]["reason"], sl.REASON_SERVICE_STATE_UNREADABLE, platform
            )
            self.assertEqual(
                absent["agents"][0]["probe_state"], sl.PROBE_CONFIRMED_ABSENT, platform
            )
            self.assertEqual(
                unreadable["agents"][0]["probe_state"], sl.PROBE_UNREADABLE, platform
            )


def _restart_envelope(platform: str, scripted) -> dict:
    """A backend-normalized `restart` envelope for ``platform`` with the probe read scripted."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
        supervisor_service_backend,
    )

    os_home = Path(tempfile.mkdtemp())
    mozyo_home = Path(tempfile.mkdtemp())
    if platform == "darwin":
        target = sl.plist_path(os_home)
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once",
            "--home", str(sl.resolve_mozyo_home(mozyo_home)),
        ]
        target.write_bytes(sl.render_plist(argv, interval_seconds=180, os_home=os_home))

        def runner(command):
            return scripted if list(command)[1] == "print" else _fake_result(0)

        with patch.object(sys, "platform", "darwin"), patch.object(
            sl, "_running_on_darwin", return_value=True
        ):
            return supervisor_service_backend.restart(
                mozyo_home=mozyo_home, os_home=os_home, runner=runner,
                which=lambda _n: "/opt/bin/mozyo-bridge",
            )

    def runner(command):
        argv = list(command)
        if argv[2] == "show" and any(a.startswith("--property=Version") for a in argv):
            return _fake_result(0)
        if argv[2] == "show":
            return _fake_result(0, scripted)
        return _fake_result(0)

    with patch.object(sys, "platform", "linux"):
        ss.install(
            os_home=os_home, mozyo_home=mozyo_home, runner=runner,
            which=lambda _n: "/opt/bin/mozyo-bridge",
        )
        return supervisor_service_backend.restart(
            mozyo_home=mozyo_home, os_home=os_home, runner=runner,
            which=lambda _n: "/opt/bin/mozyo-bridge",
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
