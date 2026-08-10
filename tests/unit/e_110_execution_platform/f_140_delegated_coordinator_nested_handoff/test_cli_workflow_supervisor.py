"""`workflow supervisor` CLI tests (Redmine #13683 Phase B1; #15183 backend dispatch).

Pins the facade: run-once / status over a hermetic temp home, and the service lifecycle command
contract — service-status is a redacted projection + secret-free definition (exit 0), while
install / restart / uninstall drive the owned scheduler pair and fail-closed (exit non-zero, zero
mutation) when the host cannot run it.

Redmine #15183: the same four verbs now dispatch by platform — the macOS LaunchAgent pair on darwin,
the systemd **user** service+timer pair on Linux, a typed refusal anywhere else — so every service
test below pins the backend it means to exercise instead of inheriting the runner's OS. Real
``launchctl`` / ``systemctl`` are never invoked here (patched), and both the OS user home and
``XDG_CONFIG_HOME`` are isolated so a projection never reads or writes the host's real
``~/Library/LaunchAgents`` or ``~/.config/systemd/user`` (Redmine #14103).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (
    supervisor_launchd as sl,
    supervisor_service_backend as sb,
    supervisor_systemd as ss,
)


def _fake_run(argv, *a, **k):
    return type("R", (), {"returncode": 113, "stdout": "", "stderr": "not found"})()


def _run(argv) -> tuple[int, str]:
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


def _parse(argv):
    """Parse OUTSIDE any platform patch, then run inside it.

    Building the parser imports the whole CLI tree, and some of those imports branch on
    ``sys.platform`` (a win32 patch sends ``multiprocessing`` looking for ``_winapi``). Parsing
    first keeps a platform-patched test from depending on which other test imported the tree first.
    """
    return build_parser().parse_args(argv)


def _invoke(args) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return int(rc or 0), buf.getvalue()


class _ServiceCliCase(unittest.TestCase):
    """Base: a hermetic mozyo home + OS user home, with the host scheduler roots isolated."""

    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))
        # A hermetic OS user home for the owned scheduler artifacts. --home is the mozyo home and
        # by design never relocates them, so their root is isolated via Path.home() — and, for the
        # systemd adapter, by pointing XDG_CONFIG_HOME at the same temp root (its unit directory
        # honours that variable exactly as the user manager does).
        self.os_home = Path(tempfile.mkdtemp())

    @contextlib.contextmanager
    def _isolated_host(self, platform: str, *, run=_fake_run):
        """Pin the dispatched backend AND isolate every host root it would touch."""
        # Patch `subprocess.run` itself rather than one adapter module's binding: both adapters use
        # the same module object, and the macOS side now spawns from a sibling leaf module, so a
        # per-module patch would silently stop covering it.
        with patch.object(sys, "platform", platform), patch.object(
            subprocess, "run", side_effect=run
        ), patch("pathlib.Path.home", return_value=self.os_home), patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.os_home / ".config")}, clear=False
        ):
            yield

    def _service_status(self, platform: str, *, run=_fake_run) -> tuple[int, str]:
        with self._isolated_host(platform, run=run):
            return _run(
                ["workflow", "supervisor", "--service-status", "--home", self.home, "--json"]
            )


class CliServiceStatusLaunchdTest(_ServiceCliCase):
    """The darwin dispatch: the ONE owned LaunchAgent answers ``--service-status`` (#15192)."""

    def test_service_status_reports_projection_and_definition_exit_zero(self) -> None:
        rc, out = self._service_status("darwin")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["backend"], sb.BACKEND_LAUNCHD)
        # Redmine #15192: ONE owned agent, so the roster is one row — the same shape as Linux.
        agents = payload["agents"]
        self.assertEqual(len(agents), 1)
        (supervisor,) = agents
        self.assertFalse(supervisor["installed"])
        self.assertFalse(supervisor["loaded"])
        self.assertEqual(payload["phase"], "B1")
        self.assertFalse(supervisor["keep_alive_present"])
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")
        self.assertFalse(payload["definition"]["keep_alive"])
        self.assertEqual(supervisor["label"], sl.SUPERVISOR_AGENT.label)
        # The owned roster matches the owned agents 1:1 — no drain service is advertised.
        self.assertEqual(len(payload["definitions"]), 1)
        self.assertEqual(payload["definitions"][0]["command"][-1], "--run-once")
        # Secret-free. Nothing is installed here, so no argv (and no home path) is projected.
        self.assertNotIn("api_key", out.lower())
        self.assertNotIn(self.home, out)

    def test_service_status_reports_installed_when_owned_plist_present(self) -> None:
        # Positive verdict held deterministic by the same OS-home seam: an owned
        # plist under the isolated home is reported installed, proving the
        # projection reflects the controlled home rather than being always-false.
        target = sl.plist_path(self.os_home)  # the single owned agent
        target.parent.mkdir(parents=True, exist_ok=True)
        argv = ["/opt/bin/mozyo-bridge", "workflow", "supervisor", "--run-once", "--home", self.home]
        target.write_bytes(sl.render_plist(argv, interval_seconds=300, os_home=self.os_home))
        rc, out = self._service_status("darwin")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["agents"]), 1)
        (supervisor,) = payload["agents"]
        self.assertTrue(supervisor["installed"])  # the owned plist under the isolated home
        self.assertTrue(supervisor["plist_exists"])
        self.assertEqual(supervisor["installed_command"], argv)
        # No retired drain registration exists on this host, so none is reported as pending.
        self.assertEqual(supervisor["legacy_drain"], sl.LEGACY_DRAIN_ABSENT)

    def test_mutating_verbs_fail_closed_zero_mutation_when_launchd_refuses(self) -> None:
        with patch.object(sl, "_running_on_darwin", return_value=False), patch.object(
            sys, "platform", "darwin"
        ), patch.object(
            subprocess, "run", side_effect=AssertionError("launchctl must not run")
        ):
            for verb in ("--install", "--restart", "--uninstall"):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
                payload = json.loads(out)
                self.assertEqual(rc, 1, verb)
                self.assertFalse(payload["performed"], verb)
                self.assertEqual(payload["reason"], sl.REASON_UNSUPPORTED_PLATFORM, verb)


class CliServiceStatusSystemdTest(_ServiceCliCase):
    """The Linux dispatch (Redmine #15183): ONE owned systemd user service + timer, same verbs."""

    def test_service_status_reports_the_systemd_projection_exit_zero(self) -> None:
        rc, out = self._service_status("linux")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["backend"], sb.BACKEND_SYSTEMD)
        # ONE owned service on Linux -- not the macOS two-row roster.
        self.assertEqual(len(payload["agents"]), 1)
        row = payload["agents"][0]
        self.assertFalse(row["installed"])
        self.assertFalse(row["loaded"])
        self.assertEqual(row["service_unit"], ss.SERVICE_UNIT_NAME)
        self.assertEqual(row["timer_unit"], ss.TIMER_UNIT_NAME)
        # The declarative definition stays the secret-free bounded command on both backends.
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")
        self.assertNotIn("api_key", out.lower())
        self.assertNotIn(self.home, out)

    def test_service_status_shows_next_run_and_last_exit_result(self) -> None:
        # The acceptance contract asks status to show 次回起動 / 直近の終了結果 without secrets.
        rc, out = self._service_status("linux")
        self.assertEqual(rc, 0)
        row = json.loads(out)["agents"][0]
        for key in ("next_elapse", "next_elapse_basis", "last_trigger",
                    "last_result", "last_exit_status", "last_exit_at"):
            self.assertIn(key, row)

    def test_service_status_reports_installed_when_the_owned_units_are_present(self) -> None:
        unit = ss.SUPERVISOR_UNIT
        path = ss.service_unit_path(self.os_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ss.render_service_unit(
                ["/opt/bin/mozyo-bridge", *unit.argv_tail, "--home", self.home]
            ),
            encoding="utf-8",
        )
        ss.timer_unit_path(self.os_home).write_text(
            ss.render_timer_unit(interval_seconds=unit.default_interval_seconds), encoding="utf-8"
        )
        rc, out = self._service_status("linux")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["agents"][0]["installed"])
        self.assertEqual(
            payload["agents"][0]["scheduled_interval_seconds"], unit.default_interval_seconds
        )
        self.assertEqual(payload["agents"][0]["installed_command"][-1], self.home)

    def test_mutating_verbs_fail_closed_zero_mutation_with_no_user_manager(self) -> None:
        # A container with no user bus is explicitly unsupported, never a silent degrade to
        # "installed but never scheduled".
        def _no_manager(argv, *a, **k):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no bus"})()

        for verb in ("--install", "--restart", "--uninstall"):
            with self._isolated_host("linux", run=_no_manager):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
            payload = json.loads(out)
            self.assertEqual(rc, 1, verb)
            self.assertFalse(payload["performed"], verb)
            self.assertEqual(payload["reason"], ss.REASON_USER_MANAGER_UNAVAILABLE, verb)
            self.assertEqual(payload["backend"], sb.BACKEND_SYSTEMD, verb)
        self.assertFalse(ss.unit_dir(self.os_home).exists())


class CliServiceStatusTextPathTest(_ServiceCliCase):
    """The human-readable path must not drop what the JSON payload carries (review j#102053 F5)."""

    def _text_status(self, platform: str) -> str:
        with self._isolated_host(platform):
            _rc, out = _run(["workflow", "supervisor", "--service-status", "--home", self.home])
        return out

    def test_text_status_labels_the_next_elapse_basis(self) -> None:
        # A monotonic figure is measured since boot, so an unlabelled `next_elapse: 4w 1d 5h` reads
        # as "in four weeks". The basis must ride with the value in text mode too.
        out = self._text_status("linux")
        self.assertIn("next_elapse:", out)
        self.assertIn("basis:", out)

    def test_text_status_shows_the_last_trigger_wall_clock(self) -> None:
        self.assertIn("last_trigger:", self._text_status("linux"))

    def test_text_status_shows_the_last_exit_result(self) -> None:
        self.assertIn("last_result:", self._text_status("linux"))

    def test_the_basis_travels_with_a_real_monotonic_value(self) -> None:
        # Drive the renderer with a real monotonic shape rather than an empty host projection.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_supervisor import (  # noqa: E501
            _service_status_lines,
        )

        host = {
            "label": "L", "installed": True, "loaded": True, "pid": None,
            "scheduled_interval_seconds": 60, "home_pin": "ok", "executable_matches": True,
            "keep_alive_present": False, "credential_readiness": "missing", "timer_enabled": True,
            "next_elapse": "4w 1d 5h 2min 6.063752s", "next_elapse_basis": "monotonic",
            "last_trigger": "Sun 2026-08-09 22:50:24 JST", "last_result": "success",
            "last_exit_status": 0, "last_exit_at": "Sun 2026-08-09 22:50:25 JST",
            "provider_reconcile_interval_seconds": 300, "installed_command": ["/x", "--run-once"],
        }
        text = "\n".join(_service_status_lines(host, 0))
        self.assertIn("next_elapse: 4w 1d 5h 2min 6.063752s (basis: monotonic)", text)
        self.assertIn("last_trigger: Sun 2026-08-09 22:50:24 JST", text)


class CliServiceDefinitionRosterTest(_ServiceCliCase):
    """Declarative definitions must describe what the backend actually owns (review j#102053 F6)."""

    def _json_status(self, platform: str) -> dict:
        args = _parse(
            ["workflow", "supervisor", "--service-status", "--home", self.home, "--json"]
        )
        with self._isolated_host(platform):
            _rc, out = _invoke(args)
        return json.loads(out)

    def test_linux_status_declares_no_drain_service_it_does_not_own(self) -> None:
        payload = self._json_status("linux")
        self.assertEqual(payload["backend"], sb.BACKEND_SYSTEMD)
        self.assertEqual(len(payload["agents"]), 1)
        # The Linux host installs ONE `--run-once` timer, so advertising a `--drain-only`
        # definition told the reader a service exists that does not. The key itself survives for
        # compatibility (j#102151 Finding 3), but only as a retired marker that makes no such claim
        # — what F6 forbade is the CLAIM, not the key, and `registered: False` is not a claim.
        self.assertFalse(payload["drain_definition"]["registered"])
        self.assertNotIn("command", payload["drain_definition"])
        self.assertEqual(len(payload["definitions"]), 1)
        self.assertEqual(payload["definitions"][0]["command"][-1], "--run-once")

    def test_the_definition_roster_matches_the_agent_roster(self) -> None:
        # EVERY backend, including the unsupported host. Covering only the two supported platforms
        # is what let the unsupported path ship with agents=0 beside definitions=1 — the invariant
        # this key exists to state (review j#102069 Finding 8).
        for platform in ("linux", "darwin", "win32"):
            payload = self._json_status(platform)
            self.assertEqual(
                len(payload["definitions"]), len(payload["agents"]),
                f"{platform}: definitions must be 1:1 with owned services",
            )

    def test_an_unsupported_host_owns_no_service_and_declares_none(self) -> None:
        payload = self._json_status("win32")
        self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED)
        self.assertEqual(payload["agents"], [])
        self.assertEqual(payload["definitions"], [])
        # Nothing is owned here, and the retired marker says exactly that too.
        self.assertFalse(payload["drain_definition"]["registered"])
        # The would-be primary definition stays available to readers that predate the roster; it is
        # not a claim that anything is installed.
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")

    def test_no_host_declares_a_drain_service_it_does_not_own(self) -> None:
        # #15192: neither host registers a `--drain-only` service any more, so neither declares one
        # in the owned roster. Emitting a definition for a service nobody owns is the defect review
        # j#102053 Finding 6 removed for Linux; the rule now has no host left to exempt.
        for platform in ("darwin", "linux"):
            payload = self._json_status(platform)
            self.assertEqual(len(payload["definitions"]), 1, platform)
            self.assertEqual(payload["definitions"][0]["command"][-1], "--run-once", platform)
            self.assertNotIn(
                "--drain-only", [d["command"][-1] for d in payload["definitions"]], platform
            )

    def test_the_drain_definition_key_survives_as_an_honest_retired_marker(self) -> None:
        # Compatibility (j#102151 Finding 3) without re-asserting the removed claim: the key stays
        # for readers that index it, but says it is retired and registered nowhere rather than
        # describing a service no host owns.
        for platform in ("darwin", "linux"):
            drain = self._json_status(platform)["drain_definition"]
            self.assertTrue(drain["retired"], platform)
            self.assertFalse(drain["registered"], platform)
            self.assertNotIn("command", drain, platform)


class CliDeprecatedIntervalFlagsTest(_ServiceCliCase):
    """j#102151 Finding 3: a minor feature keeps the previous release's parser surface working."""

    def _status_with(self, *extra) -> dict:
        with self._isolated_host("linux"):
            rc, out = _run(
                ["workflow", "supervisor", "--service-status", "--home", self.home, "--json", *extra]
            )
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_the_previous_release_flags_still_parse(self) -> None:
        # The regression this closes: both flags exited 2 with `unrecognized arguments`.
        payload = self._status_with("--drain-interval", "60", "--reconciliation-interval", "240")
        self.assertEqual(len(payload["deprecations"]), 2)

    def test_reconciliation_interval_is_folded_onto_the_one_cadence_knob(self) -> None:
        # It set the definition's interval on the previous release, so ignoring it would silently
        # change what an existing invocation configures.
        payload = self._status_with("--reconciliation-interval", "240")
        self.assertEqual(payload["definition"]["reconciliation_interval_seconds"], 240)
        self.assertEqual(payload["agents"][0]["scheduled_interval_seconds"], 240)

    def test_an_explicit_tick_interval_wins_over_the_deprecated_synonym(self) -> None:
        payload = self._status_with("--reconciliation-interval", "240", "--tick-interval", "90")
        self.assertEqual(payload["definition"]["reconciliation_interval_seconds"], 90)

    def test_drain_interval_is_accepted_but_inert_and_says_so(self) -> None:
        payload = self._status_with("--drain-interval", "60")
        self.assertEqual(payload["definition"]["reconciliation_interval_seconds"], 180)
        self.assertIn("ignored", " ".join(payload["deprecations"]))

    def test_no_deprecation_noise_when_the_flags_are_not_used(self) -> None:
        self.assertNotIn("deprecations", self._status_with())


class CliServiceHelpContractTest(unittest.TestCase):
    """CLI help is a distributed contract: it must not advertise retired conditions (F6)."""

    def _supervisor_help(self) -> str:
        """Help text with argparse's line wrapping collapsed.

        argparse re-wraps every help string to the terminal width, so a phrase can be split across
        lines at any point. Asserting on the raw output tests the wrap position, not the contract.
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                build_parser().parse_args(["workflow", "supervisor", "--help"])
            except SystemExit:
                pass
        return " ".join(buf.getvalue().split())

    def test_help_does_not_claim_a_linux_atomic_pair(self) -> None:
        text = self._supervisor_help()
        # The retired conditions must not be stated as host-common facts.
        self.assertNotIn("service+timer pair", text)
        self.assertNotIn("Atomic-or-nothing;", text)

    def test_help_states_one_registration_per_host(self) -> None:
        text = self._supervisor_help()
        self.assertIn("ONE macOS LaunchAgent, or ONE Linux systemd user service + timer", text)
        self.assertIn("every --tick-interval seconds", text)
        # The retired two-agent macOS shape must not be advertised anywhere in help.
        self.assertNotIn("LaunchAgent pair", text)

    def test_help_states_the_non_gating_install_as_a_host_common_fact(self) -> None:
        # Previously this was stated as a Linux-only property. Since j#102151 Finding 4 it is the
        # contract on both hosts, and help must not re-split it.
        text = self._supervisor_help()
        self.assertIn("An unconfigured Redmine blocks the install on neither host", text)
        self.assertIn("readiness is reported, not gated", text)
        self.assertNotIn("does not block installing the Linux timer", text)

    def test_help_states_the_credential_contract_as_host_common(self) -> None:
        # j#102151 Finding 4 removed the macOS-only credential gate, so help must no longer describe
        # one — an install-time difference is operator-visible, which is what #15192 unifies.
        text = self._supervisor_help()
        self.assertIn("On NEITHER host does an unconfigured Redmine block the install", text)
        self.assertIn("readiness is reported, not gated", text)
        self.assertNotIn("macOS refuses on a non-ready credential", text)


class CliServiceUnsupportedHostTest(_ServiceCliCase):
    """A host with no owned scheduler adapter is a typed refusal, never a silent no-op."""

    def test_mutating_verbs_refuse_with_a_typed_backend_token(self) -> None:
        for verb in ("--install", "--restart", "--uninstall"):
            with patch.object(sys, "platform", "win32"):
                rc, out = _run(["workflow", "supervisor", verb, "--home", self.home, "--json"])
            payload = json.loads(out)
            self.assertEqual(rc, 1, verb)
            self.assertFalse(payload["performed"], verb)
            self.assertEqual(payload["reason"], sb.REASON_NO_BACKEND, verb)
            self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED, verb)

    def test_service_status_still_exits_zero_and_mutates_nothing(self) -> None:
        with patch.object(sys, "platform", "win32"):
            rc, out = _run(
                ["workflow", "supervisor", "--service-status", "--home", self.home, "--json"]
            )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["backend"], sb.BACKEND_UNSUPPORTED)
        self.assertEqual(payload["agents"], [])
        self.assertFalse(payload["platform_supported"])
        # The secret-free declarative definitions are still projected.
        self.assertEqual(payload["definition"]["command"][-1], "--run-once")


class CliWorkflowSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = str(Path(tempfile.mkdtemp()))

    def test_status_over_empty_home_exits_zero(self) -> None:
        rc, out = _run(["workflow", "supervisor", "--status", "--home", self.home, "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["workspaces_total"], 0)
        self.assertEqual(payload["leases_held"], 0)

    def test_run_once_over_empty_home_supervises_nothing(self) -> None:
        rc, out = _run(
            ["workflow", "supervisor", "--run-once", "--home", self.home,
             "--holder", "superTest", "--json"]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "bounded_reconciliation")
        self.assertEqual(payload["workspaces_total"], 0)

    def test_run_once_with_wake_selects_local_wake_mode(self) -> None:
        rc, out = _run(
            ["workflow", "supervisor", "--run-once", "--home", self.home,
             "--wake", "wsA:13683", "--json"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["mode"], "local_wake")

    def test_no_action_is_rejected(self) -> None:
        parser = build_parser()
        # argparse's required mutually-exclusive group rejects a bare `workflow supervisor`.
        with self.assertRaises(SystemExit):
            parser.parse_args(["workflow", "supervisor"])


if __name__ == "__main__":
    unittest.main()
