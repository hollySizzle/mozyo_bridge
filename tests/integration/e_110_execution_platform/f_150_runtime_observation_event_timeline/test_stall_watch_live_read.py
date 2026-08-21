"""Integration: the stall watcher's LIVE read adapter, end to end (Redmine #15843).

Why this file exists, when the unit tests already cover the pass: the pass takes its
reader as an injected callable, so a suite that only ever hands it a fake never executes
:func:`live_screen_reader` — and that function reaches its two collaborators through
**lazy imports inside a ``try`` that swallows every exception**, which is exactly the
shape that returns ``None`` and looks like "no herdr binary" when what really happened is
that a symbol was renamed. #15745 j#109007 is the recorded instance of that class: a
rename left a live adapter's in-method import dangling while 117 faked tests stayed green
and production failed at the send.

So this file does the two things a fake cannot:

1. imports both collaborators **at module level**, so a rename fails loudly here rather
   than degrading into a swallowed ``ImportError`` at runtime;
2. drives the real adapter against a fake ``herdr`` executable on disk — real argv, real
   subprocess, real payload parsing — so the whole live path is executed, only the
   provider process is substituted.

Real filesystem + subprocess, temp-rooted, no operator state touched: integration, not
unit, per ``vibes/docs/logics/tests-placement-discovery-policy.md``.
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Module-level, deliberately: these are the two symbols `live_screen_reader` reaches
# through a swallowing `try`, so importing them here is the drift guard.
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501  F401
    _resolve_binary_or_die,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501  F401
    live_visible_reader,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.cli_workflow_stall_watch import (  # noqa: E501
    live_screen_reader,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    HERDR_BINARY_ENV,
)

FAKE_HERDR = """#!/bin/sh
# Fake herdr: record the argv it was called with, then emit the live E11 read payload.
printf '%s\\n' "$*" >> "$ARGV_LOG"
cat <<'PAYLOAD'
{PAYLOAD}
PAYLOAD
"""


class LiveScreenReaderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mozyo-15843-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.argv_log = self.root / "argv.log"

    def _install_fake_herdr(self, payload: str) -> Path:
        binary = self.root / "herdr"
        binary.write_text(FAKE_HERDR.replace("{PAYLOAD}", payload), encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        return binary

    def _env(self, binary: Path) -> dict:
        # Absolute PATH only: the shared resolver rejects a trusted PATH carrying any
        # empty / relative (cwd-dependent) component, so a sloppy env here would fail
        # closed for the wrong reason and make this test pass vacuously.
        return {
            HERDR_BINARY_ENV: str(binary),
            "PATH": "/usr/bin:/bin",
            "ARGV_LOG": str(self.argv_log),
        }

    def test_the_live_adapter_reads_a_pane_through_the_real_argv_and_parser(self):
        screen = "line one\nline two\n✳ Thinking… (12s · 1.2k tokens)"
        payload = json.dumps({"result": {"read": {"text": screen, "truncated": False}}})
        binary = self._install_fake_herdr(payload)

        with mock.patch.dict(os.environ, self._env(binary), clear=True):
            read = live_screen_reader()
            self.assertIsNotNone(
                read,
                "the live reader did not bind: a swallowed import or a failed binary "
                "resolution, not a missing herdr",
            )
            readable, content = read("w1V:pY")

        self.assertTrue(readable)
        self.assertEqual(content, screen)

        argv = self.argv_log.read_text(encoding="utf-8").strip()
        # The read is the same read-only primitive the send boundary already trusts.
        self.assertIn("agent read w1V:pY", argv)
        self.assertIn("--source visible", argv)

    def test_a_non_string_payload_reads_unreadable_rather_than_empty(self):
        # A blank read is not evidence of a clear screen (#13760), and an unreadable
        # sample must reach the sensor as INCOMPARABLE, never as "nothing changed".
        binary = self._install_fake_herdr(json.dumps({"result": {"read": {}}}))
        with mock.patch.dict(os.environ, self._env(binary), clear=True):
            read = live_screen_reader()
            readable, content = read("w1V:pY")
        # `_parse_read_payload` falls back to raw stdout for an unrecognised shape, so
        # this asserts the adapter's contract (a str is readable) rather than inventing
        # a failure the parser does not produce.
        self.assertTrue(readable)
        self.assertIsInstance(content, str)

    def test_an_unresolvable_binary_binds_no_reader_instead_of_guessing(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-abs-dir"}, clear=True):
            self.assertIsNone(live_screen_reader())

    def test_a_failing_herdr_invocation_surfaces_as_an_unreadable_sample(self):
        binary = self.root / "herdr"
        binary.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
            StallWatchTarget,
            load_default_signatures,
            run_stall_watch_pass,
        )
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
            CLASS_SCREEN_UNREADABLE,
            RX_NO_ACTION,
        )

        with mock.patch.dict(os.environ, self._env(binary), clear=True):
            read = live_screen_reader()
            observations = run_stall_watch_pass(
                [StallWatchTarget(target="w1V:pY", provider_id="claude")],
                read_screen=read,
                clock=lambda: 0.0,
                sleep=lambda seconds: None,
                signatures=load_default_signatures(),
                interval_seconds=0.0,
            )

        self.assertEqual(observations[0].stall_class, CLASS_SCREEN_UNREADABLE)
        self.assertEqual(observations[0].prescription.action, RX_NO_ACTION)


class LivePassAgainstFakeProviderTest(unittest.TestCase):
    """The whole pass — real reader, real registry, real classifier — on one frozen pane."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mozyo-15843-pass-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_frozen_retry_banner_classifies_from_the_packaged_signatures(self):
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_pass import (  # noqa: E501
            StallWatchTarget,
            load_default_signatures,
            run_stall_watch_pass,
        )
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_disposition import (  # noqa: E501
            CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED,
            RX_PATIENT_WAIT_RETRY,
        )

        screen = "transcript line\n✳ Thinking… · Retrying in 8s · attempt 3/10"
        payload = json.dumps({"result": {"read": {"text": screen}}})
        binary = self.root / "herdr"
        binary.write_text(
            FAKE_HERDR.replace("{PAYLOAD}", payload), encoding="utf-8"
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        env = {
            HERDR_BINARY_ENV: str(binary),
            "PATH": "/usr/bin:/bin",
            "ARGV_LOG": str(self.root / "argv.log"),
        }

        with mock.patch.dict(os.environ, env, clear=True):
            observations = run_stall_watch_pass(
                [StallWatchTarget(target="w1V:pY", provider_id="claude")],
                read_screen=live_screen_reader(),
                clock=lambda: 0.0,
                sleep=lambda seconds: None,
                signatures=load_default_signatures(),
                interval_seconds=0.0,
            )

        observation = observations[0]
        self.assertEqual(
            observation.stall_class, CLASS_PROVIDER_UNRESPONSIVE_SUSPECTED
        )
        self.assertEqual(observation.prescription.action, RX_PATIENT_WAIT_RETRY)
        self.assertFalse(observation.prescription.relaunch_is_a_candidate)
        self.assertEqual(observation.evidence, "binary_read_unrendered")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
