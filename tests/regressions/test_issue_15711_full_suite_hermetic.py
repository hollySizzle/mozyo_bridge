"""Regression pin: the full suite must not couple to the ambient home or a real remote (Redmine #15711).

Fixed defect (observed on the raw full suite, #15709 j#108148 side observation): a
deterministic failure harness — a fake ambient ``MOZYO_BRIDGE_HOME``, a fake ``HOME``
(so the ``~/.mozyo_bridge`` fallback also lands on harness ground), and a PATH-shimmed
``ssh`` that records and refuses — attributed, per test, every ambient write and every
outbound ssh attempt:

- ``test_canonical_gate_record_e2e`` enqueued a supervisor wake hint into the ambient
  home (``supervisor-wake.sqlite``); ``test_workflow_glance_cli`` /
  ``test_issue_14242_active_live_zero_retire`` / ``test_issue_14813...`` /
  ``test_mcp_read_plan_tools`` created the ambient ``state.sqlite`` (no-arg store
  construction or glance source wiring); ``test_issue_15190...`` took the
  ``session_start_gate`` lease inside the ambient home; ``test_cli_herdr_unit_board``
  wrote the metadata sync lock there.
- ``test_cli_herdr_unit_board``'s show/watch tests read the ambient
  ``unit-board-sources.yaml``: with the operator's real remote sources declared, the
  multi-source branch bypassed the mocked ``_runtime`` and spawned a REAL
  ``ssh <target> mozyo-bridge herdr unit-board show`` per remote source — and the watch
  loop (mocked ``time.sleep``) spun that spawn unboundedly (229k recorded attempts in
  one harness run).
- ``test_redmine_note_transport``'s fail-closed tests cleared the environment, so the
  credential resolver read the operator's real home credential file and could proceed
  to a live network PUT.
- ``test_herdr_forward_send`` set ``MOZYO_BRIDGE_HOME`` and popped it on cleanup instead
  of restoring it, un-pinning every later test in the process.
- ``test_test_disk_pressure`` imported ``mozyo_bridge`` without the corpus self-insert
  header, so standalone discovery verified whatever installed runtime happened to be on
  the interpreter path.

The fix pins each offending TestCase to its own temp home
(``tests/support/process_home_pin.py``), passes an explicit ``home=`` where the test
itself clears the environment, and adds the missing self-insert header. This file pins
the recurrence with the same harness the diagnosis used.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from tests.support.nested_python import hermetic_python_env

#: Every module the #15711 harness attributed an ambient write, an outbound ssh
#: attempt, a credential-surface read, or an env-restore leak to. Each must stay green
#: and silent under the adversarial environment below, in any run shape.
FAMILY_MODULES = (
    "tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_canonical_gate_record_e2e",
    "tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_workflow_glance_cli",
    "tests.regressions.test_issue_14242_active_live_zero_retire",
    "tests.regressions.test_issue_14813_active_roster_capacity_partition",
    "tests.regressions.test_issue_15190_v1_replacement_alias_boundary",
    "tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_herdr_forward_send",
    "tests.unit.e_110_execution_platform.f_180_llm_mcp_operation_entry.test_mcp_read_plan_tools",
    "tests.unit.e_140_adapter_provider.f_120_redmine_adapter.test_redmine_note_transport",
    "tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_cli_herdr_unit_board",
)

#: An abstract, unroutable ssh destination: the shim refuses before any resolution, and
#: even without the shim nothing listens behind this reserved-for-tests name shape.
_TRAP_SSH_TARGET = "issue-15711-trap.invalid"

#: A syntactically valid base URL whose connection is refused locally (discard port on
#: loopback) — an ambient credential read that escapes to the network fails fast and
#: forever without leaving the host. Values are abstract placeholders, not secrets.
_TRAP_CREDENTIALS = (
    "redmine:\n"
    "  url: http://127.0.0.1:9\n"
    "  api_key: issue-15711-regression-trap\n"
)

_SSH_SHIM = """#!/bin/sh
# Redmine #15711 regression shim: record the attempt, refuse the connection.
printf 'ssh %s\\n' "$*" >> "$MOZYO_15711_SSH_LOG"
exit 255
"""


def _adversarial_environment(base: Path) -> tuple[dict[str, str], Path, Path, set[str]]:
    """Build the fake-ambient / fake-fallback / fence-root / ssh-shim world.

    Three harness-owned homes cover every resolution shape a test can take:

    - ``MOZYO_BRIDGE_HOME`` (explicit ambient) — an unpinned test resolves here;
    - ``$HOME/.mozyo_bridge`` (tilde fallback) — a test that pops the env var but
      keeps ``HOME`` resolves here;
    - the resolver fence root (``MOZYO_BRIDGE_TEST_HOME_FENCE``, #14757) — a test
      that CLEARS the environment resolves here, because with ``HOME`` gone the
      tilde expansion falls through to the passwd database (the operator's real
      home) and the fence substitutes its root. The real default home rides in
      ``MOZYO_BRIDGE_TEST_HOME_DENY``, so an explicit resolution onto it refuses
      instead of touching shared state.

    Every home carries the same traps: a remote ssh source (any sources read that
    escapes to the multi-source branch spawns the recording shim, never a host)
    and a credential file whose base URL is refused locally (any ambient
    credential read that escapes toward the network fails as ``transport_error``
    instead of the expected fail-closed reason).
    """
    ambient = base / "ambient-home"
    fallback_root = base / "fake-home"
    fallback = fallback_root / ".mozyo_bridge"
    fence_root = base / "fence-home"
    shim_bin = base / "bin"
    ssh_log = base / "ssh-attempts.log"
    for directory in (ambient, fallback, fence_root, shim_bin):
        directory.mkdir(parents=True)
    for home in (ambient, fallback_root, fallback, fence_root):
        home.chmod(0o700)
    sources = (
        "version: 1\n"
        "sources:\n"
        "  - host_id: trap\n"
        "    kind: ssh\n"
        f"    ssh_target: {_TRAP_SSH_TARGET}\n"
        "    label: issue-15711 trap\n"
    )
    for home in (ambient, fallback, fence_root):
        (home / "unit-board-sources.yaml").write_text(sources, encoding="utf-8")
        credential_file = home / "redmine-credentials.yaml"
        credential_file.write_text(_TRAP_CREDENTIALS, encoding="utf-8")
        credential_file.chmod(0o600)
    shim = shim_bin / "ssh"
    shim.write_text(_SSH_SHIM, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    env = hermetic_python_env()
    env["MOZYO_BRIDGE_HOME"] = str(ambient)
    env["HOME"] = str(fallback_root)
    env["MOZYO_BRIDGE_TEST_HOME_FENCE"] = str(fence_root)
    env["MOZYO_BRIDGE_TEST_HOME_DENY"] = str(
        Path("~/.mozyo_bridge").expanduser().resolve()
    )
    env["MOZYO_15711_SSH_LOG"] = str(ssh_log)
    env["PATH"] = f"{shim_bin}{os.pathsep}{env.get('PATH', '')}"
    planted = (
        {p.name for p in ambient.iterdir()}
        | {f"fallback:{p.name}" for p in fallback.iterdir()}
        | {f"fence:{p.name}" for p in fence_root.iterdir()}
    )
    return env, ambient, ssh_log, planted


class FullSuiteAmbientCouplingTest(unittest.TestCase):
    def test_family_is_green_and_silent_under_the_adversarial_environment(self) -> None:
        base = Path(tempfile.mkdtemp())
        env, ambient, ssh_log, planted = _adversarial_environment(base)
        fallback = Path(env["HOME"]) / ".mozyo_bridge"
        fence_root = Path(env["MOZYO_BRIDGE_TEST_HOME_FENCE"])

        proc = subprocess.run(
            [sys.executable, "-m", "unittest", *FAMILY_MODULES],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "a family verdict coupled to the ambient home / remote sources:\n"
            + proc.stderr[-4000:],
        )
        # No ambient write into any watched home: the pinned per-test homes own every
        # store the family touches.
        after = (
            {p.name for p in ambient.iterdir()}
            | {f"fallback:{p.name}" for p in fallback.iterdir()}
            | {f"fence:{p.name}" for p in fence_root.iterdir()}
        )
        self.assertEqual(after, planted)
        # And not one outbound ssh spawn: the multi-source branch must never be entered
        # from a test, so the recording shim stays silent.
        self.assertFalse(
            ssh_log.exists() and ssh_log.read_text(encoding="utf-8").strip(),
            "a test spawned ssh toward a configured remote source",
        )

    def test_forward_send_cleanup_restores_the_callers_home_pin(self) -> None:
        # The defect was restore-vs-pop: after the module ran, MOZYO_BRIDGE_HOME was
        # ABSENT, un-pinning the remainder of the process. Run the module in a nested
        # interpreter with a sentinel pin and require the sentinel to survive.
        base = Path(tempfile.mkdtemp())
        env, _ambient, _ssh_log, _planted = _adversarial_environment(base)
        sentinel = env["MOZYO_BRIDGE_HOME"]
        bootstrap = (
            "import os, sys, unittest\n"
            "sys.path.insert(0, os.getcwd())\n"
            "module = 'tests.unit.e_110_execution_platform."
            "f_140_delegated_coordinator_nested_handoff.test_herdr_forward_send'\n"
            "program = unittest.main(module=None, argv=['x', module], exit=False,\n"
            "                        verbosity=0)\n"
            "ok = program.result.wasSuccessful()\n"
            "kept = os.environ.get('MOZYO_BRIDGE_HOME') == os.environ['MOZYO_15711_PIN']\n"
            "sys.exit(0 if (ok and kept) else 1)\n"
        )
        env["MOZYO_15711_PIN"] = sentinel
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "test_herdr_forward_send failed or left MOZYO_BRIDGE_HOME un-restored:\n"
            + proc.stderr[-2000:],
        )

    def test_disk_pressure_module_resolves_its_own_runtime(self) -> None:
        # Scrub every sys.path entry that already offers a mozyo_bridge package (an
        # installed runtime), keeping the rest of the interpreter (yaml etc.) intact.
        # The only way the module can then import mozyo_bridge is the corpus
        # self-insert header; without it, standalone discovery silently verified
        # whatever installed runtime the interpreter happened to see.
        # Import the module exactly as `discover -s tests` does — top_level_dir is the
        # tests dir, so the module name carries no `tests.` prefix and the package
        # __init__ bootstrap (which also inserts src/) never runs.
        bootstrap = (
            "import pathlib, sys, unittest\n"
            "sys.path[:] = ['tests'] + [p for p in sys.path\n"
            "               if p and not (pathlib.Path(p) / 'mozyo_bridge').exists()]\n"
            "module = ('unit.e_150_quality_architecture."
            "f_150_ci_verification.test_test_disk_pressure')\n"
            "program = unittest.main(module=None, argv=['x', module], exit=False,\n"
            "                        verbosity=0)\n"
            "sys.exit(0 if program.result.wasSuccessful() else 1)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", bootstrap],
            cwd=ROOT,
            env=hermetic_python_env(),
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "test_test_disk_pressure does not resolve the repo runtime by itself:\n"
            + proc.stderr[-2000:],
        )


if __name__ == "__main__":
    unittest.main()
