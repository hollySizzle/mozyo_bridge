"""Regression pins for Redmine #13748 — managed-launch launcher capability skew.

Cause / fix commit ``d925b90`` (preflight added) hardened by review R1 j#77425 (a
bare exit-0 is not proof of capability).

The #13637 managed launch execs every provider THROUGH ``<launcher> herdr agent-attest
... -- <provider>``. Before #13748, :func:`resolve_attest_launcher` only proved the
launcher was an *executable*, so an installed launcher lagging unreleased source
(measured: installed ``mozyo-bridge 0.10.0`` answers ``herdr agent-attest --help`` with
argparse exit 2 while the source tree exits 0) let every wrapped pane exit before the
provider started — ``sublane create`` returned a live locator that then vanished.

These pins characterize the two failure modes
:func:`preflight_attest_launcher_capability` must reject at the launcher
:func:`resolve_attest_launcher` selects, so the vanishing lane can never silently
return:

- the **exit-2 skew** — the installed launcher lacks the subcommand; and
- the **exit-0 false positive** (review R1) — a success-exit non-launcher (e.g.
  ``/usr/bin/true``) ignores the probe args and exits 0 *without* the subcommand, so
  exit 0 alone is not proof; the real launch, running the SAME launcher as the
  wrapper's ``argv[0]``, would still exit before ``exec``ing the provider. A positive
  verdict additionally requires the ``--assigned-name`` contract marker in the output.

Both are pinned across the two launcher resolution paths (explicit
``MOZYO_BRIDGE_LAUNCHER`` override and trusted-PATH fallback), and the error is asserted
to carry launcher path + required command + recovery action (acceptance 5).
Characterization only; the fix lives in
``src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/application/``
(``herdr_pane_lifecycle.py`` + ``herdr_launch_argv.py``).
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    resolve_attest_launcher,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (
    build_attest_capability_contract_line,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (
    preflight_attest_launcher_capability,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
    HerdrSessionStartError,
)

# A launcher whose `herdr agent-attest --help` prints the wrapper contract marker AND (Redmine
# #13847) advertises the required attestation-store schema, then exits 0 — a source-capable /
# released mozyo-bridge whose attestation store matches this runtime's. The subcommand marker
# alone is no longer sufficient (#13847 adds the schema check); a truly-capable launcher
# advertises both.
_CAPABLE = (
    "#!/bin/sh\n"
    "echo 'usage: mozyo-bridge herdr agent-attest [-h] --assigned-name NAME'\n"
    "echo 'capability contract (Redmine #13847):'\n"
    f"echo '{build_attest_capability_contract_line(HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION)}'\n"
    "exit 0\n"
)
# An installed launcher whose CLI lacks the subcommand (argparse invalid choice / exit 2).
_SKEW = "#!/bin/sh\necho \"invalid choice: 'agent-attest'\" >&2\nexit 2\n"
# A success-exit non-launcher: exits 0 for any args, emits no marker (the R1 `/usr/bin/true`).
_SUCCESS_NO_MARKER = "#!/bin/sh\nexit 0\n"

_TIMEOUT = 10.0


class Issue13748LauncherCapabilityPreflight(unittest.TestCase):
    def _script(self, directory, name, body):
        path = Path(directory) / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(path)

    # --- fix surface: preflight_attest_launcher_capability -------------------

    def test_capable_launcher_passes(self):
        with tempfile.TemporaryDirectory() as d:
            launcher = self._script(d, "mozyo-bridge", _CAPABLE)
            # Returns the parsed observation since Redmine #13882 (it feeds the store
            # join without re-running the probe); "passes" still means "does not raise".
            observation = preflight_attest_launcher_capability(
                launcher, subprocess.run, _TIMEOUT, {}
            )
            self.assertTrue(observation.subcommand_marker_present)

    def test_exit2_skew_launcher_fails_closed(self):
        # The original #13748 defect: installed launcher lacks the subcommand (exit 2).
        with tempfile.TemporaryDirectory() as d:
            launcher = self._script(d, "mozyo-bridge", _SKEW)
            with self.assertRaises(HerdrSessionStartError) as ctx:
                preflight_attest_launcher_capability(launcher, subprocess.run, _TIMEOUT, {})
            msg = str(ctx.exception)
            self.assertIn(launcher, msg)
            self.assertIn("herdr agent-attest", msg)
            self.assertIn("MOZYO_BRIDGE_LAUNCHER", msg)

    def test_success_exit_non_launcher_fails_closed(self):
        # Review R1: exit 0 without the `--assigned-name` marker must NOT be trusted.
        with tempfile.TemporaryDirectory() as d:
            launcher = self._script(d, "mozyo-bridge", _SUCCESS_NO_MARKER)
            with self.assertRaises(HerdrSessionStartError) as ctx:
                preflight_attest_launcher_capability(launcher, subprocess.run, _TIMEOUT, {})
            msg = str(ctx.exception)
            self.assertIn("--assigned-name", msg)
            self.assertIn(launcher, msg)

    def test_system_true_is_rejected(self):
        # The exact reviewer reproduction command: /usr/bin/true.
        true = shutil.which("true") or "/usr/bin/true"
        with self.assertRaises(HerdrSessionStartError):
            preflight_attest_launcher_capability(true, subprocess.run, _TIMEOUT, {})

    # --- both launcher resolution paths (acceptance 4) -----------------------

    def _resolve_and_preflight(self, env):
        launcher = resolve_attest_launcher(env)
        # The launcher DID resolve as an executable — the pre-#13748 gate — so only the
        # capability preflight can reject it.
        self.assertTrue(launcher)
        preflight_attest_launcher_capability(launcher, subprocess.run, _TIMEOUT, env)
        return launcher

    def test_explicit_override_incapable_fails_closed(self):
        for body, label in ((_SKEW, "exit2"), (_SUCCESS_NO_MARKER, "success-no-marker")):
            with self.subTest(kind=label), tempfile.TemporaryDirectory() as d:
                launcher = self._script(d, "any-launcher", body)
                with self.assertRaises(HerdrSessionStartError):
                    self._resolve_and_preflight({"MOZYO_BRIDGE_LAUNCHER": launcher})

    def test_path_fallback_incapable_fails_closed(self):
        for body, label in ((_SKEW, "exit2"), (_SUCCESS_NO_MARKER, "success-no-marker")):
            with self.subTest(kind=label), tempfile.TemporaryDirectory() as d:
                # `resolve_attest_launcher` PATH fallback looks for a `mozyo-bridge` binary.
                self._script(d, "mozyo-bridge", body)
                with self.assertRaises(HerdrSessionStartError):
                    self._resolve_and_preflight({"PATH": d})

    def test_path_fallback_capable_resolves_and_passes(self):
        with tempfile.TemporaryDirectory() as d:
            self._script(d, "mozyo-bridge", _CAPABLE)
            launcher = self._resolve_and_preflight({"PATH": d})
            self.assertTrue(launcher.endswith("mozyo-bridge"))


if __name__ == "__main__":
    unittest.main()
