"""Regression pins for Redmine #13847 — launcher attestation-schema capability contract.

The #13748 launcher preflight only proved a probed launcher *carries* the ``herdr
agent-attest`` subcommand (the ``--assigned-name`` marker). It could not see the #13847
failure: the source runtime's startup self-attestation store is schema v2
(``replacement_action_id``), but a managed launch may be wrapped through an *older
installed* launcher whose attestation store is v1. Both carry ``agent-attest`` +
``--assigned-name`` — so the subcommand-marker probe passes — yet the v1 launcher,
injected with the source runtime's shared ``MOZYO_BRIDGE_HOME``, opens the v2 store, hits
the exact-version write guard, silently drops the attestation, and ``exec``s the provider
anyway: the pair boots **live but unattested / stale** (the live evidence — gateway
``unattested``, worker ``stale_named_slot``), with no public recovery.

#13847 adds an attestation-**schema** capability contract to the preflight, decided
purely from the launcher's advertised schema (probe / decision separation):

- the source ``agent-attest --help`` advertises its store schema as a stable, unwrapped
  capability token (the ``RawDescriptionHelpFormatter`` epilog);
- a launcher that advertises no schema (any pre-#13847 build, incl. the v1 installed
  launcher) fails closed ``schema_contract_absent``;
- a launcher advertising a non-matching exact schema fails closed
  ``schema_version_mismatch`` (the shared store's write guard is an exact match);
- ``sublane create/start`` surfaces the typed ``launcher_runtime_incompatible`` blocker,
  zero-actuation before any process launch.

These pins are characterization for
``.../f_130_terminal_runtime_provider/application/herdr_launcher_capability.py`` (pure
decision) + ``herdr_pane_lifecycle.py`` (probe adapter) + the sublane actuator's typed
blocker.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.core.state.herdr_identity_attestation import (
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launcher_capability import (  # noqa: E501
    ATTEST_CAPABILITY_CONTRACT_PREFIX,
    LAUNCHER_CAPABILITY_OK,
    LAUNCHER_SCHEMA_CONTRACT_ABSENT,
    LAUNCHER_SCHEMA_VERSION_MISMATCH,
    LAUNCHER_SUBCOMMAND_ABSENT,
    LauncherCapabilityObservation,
    build_attest_capability_contract_line,
    decide_launcher_capability,
    parse_launcher_capability_output,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    ATTEST_CAPABILITY_MARKER,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pane_lifecycle import (  # noqa: E501
    HerdrLauncherIncompatibleError,
    preflight_attest_launcher_capability,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator import (  # noqa: E501
    SublaneActuateUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_actuation import (  # noqa: E501
    ACTUATE_BLOCKED,
    REASON_LAUNCHER_INCOMPATIBLE,
    REASON_PANE_CREATE_FAILED,
    SublaneLauncherIncompatibleError,
)

# The sanctioned fake actuator port + request builder (the #12973 style).
from tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_sublane_actuator import (  # noqa: E501
    FakeActuatorOps,
    _req,
)

_V = HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION


def _capable_help() -> str:
    """A v2-capable launcher's `agent-attest --help`: subcommand marker + schema token."""
    return (
        "usage: mozyo-bridge herdr agent-attest [-h] --assigned-name NAME\n"
        "capability contract (Redmine #13847):\n"
        + build_attest_capability_contract_line(_V)
    )


def _runner(stdout: str, rc: int = 0):
    def run(argv, capture_output=None, text=None, timeout=None, env=None, **kw):
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")

    return run


class PureDecisionMatrix(unittest.TestCase):
    def test_capable_v2_launcher_is_ok(self):
        obs = parse_launcher_capability_output(_capable_help())
        self.assertEqual(obs.advertised_schema_version, _V)
        self.assertTrue(obs.subcommand_marker_present)
        v = decide_launcher_capability(obs, required_schema_version=_V)
        self.assertTrue(v.ok)
        self.assertEqual(v.reason, LAUNCHER_CAPABILITY_OK)

    def test_v1_launcher_subcommand_but_no_schema_fails_closed(self):
        # The exact #13847 root cause: subcommand marker present, no advertised schema.
        v1 = "usage: mozyo-bridge herdr agent-attest [-h] --assigned-name NAME\n"
        obs = parse_launcher_capability_output(v1)
        self.assertTrue(obs.subcommand_marker_present)
        self.assertIsNone(obs.advertised_schema_version)
        v = decide_launcher_capability(obs, required_schema_version=_V)
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, LAUNCHER_SCHEMA_CONTRACT_ABSENT)

    def test_non_launcher_no_marker_is_subcommand_absent(self):
        v = decide_launcher_capability(
            parse_launcher_capability_output(""), required_schema_version=_V
        )
        self.assertFalse(v.ok)
        self.assertEqual(v.reason, LAUNCHER_SUBCOMMAND_ABSENT)

    def test_newer_or_older_exact_schema_mismatch_fails_closed(self):
        for other in (_V + 1, _V + 7):
            with self.subTest(advertised=other):
                obs = LauncherCapabilityObservation(True, other)
                v = decide_launcher_capability(obs, required_schema_version=_V)
                self.assertFalse(v.ok)
                self.assertEqual(v.reason, LAUNCHER_SCHEMA_VERSION_MISMATCH)

    def test_contract_line_is_in_lockstep_with_store_schema(self):
        # The advertised number is derived from the store schema constant, never a literal.
        line = build_attest_capability_contract_line(_V)
        self.assertTrue(line.startswith(ATTEST_CAPABILITY_CONTRACT_PREFIX))
        self.assertEqual(parse_launcher_capability_output(line).advertised_schema_version, _V)

    def test_guard_bite_subcommand_only_launcher_must_be_rejected(self):
        # Adversarial: if the schema check were dropped and only the subcommand marker were
        # required (the pre-#13847 behavior), a v1 launcher would pass. It must not.
        obs = LauncherCapabilityObservation(subcommand_marker_present=True, advertised_schema_version=None)
        self.assertFalse(decide_launcher_capability(obs, required_schema_version=_V).ok)


class HelpTokenWrapRobustness(unittest.TestCase):
    """The token must survive `--help` rendering at any terminal width (the wrap guard).

    Argparse's default help formatter reflows the epilog and breaks a long token on
    hyphens AND on width — an earlier hyphenated token form was split mid-token, so the
    source's OWN launcher (no skew) read as ``schema_contract_absent``. The token is now
    hyphen/whitespace-free AND rendered by a RawDescriptionHelpFormatter.
    """

    def _real_help(self, columns: str) -> str:
        env = dict(os.environ)
        env["COLUMNS"] = columns
        env["PYTHONPATH"] = str(_SRC)
        out = subprocess.run(
            [sys.executable, "-m", "mozyo_bridge", "herdr", "agent-attest", "--help"],
            capture_output=True,
            text=True,
            env=env,
        )
        return out.stdout + out.stderr

    def test_source_launcher_parses_compatible_at_narrow_and_wide_widths(self):
        for cols in ("40", "80", "200"):
            with self.subTest(columns=cols):
                obs = parse_launcher_capability_output(self._real_help(cols))
                self.assertEqual(
                    obs.advertised_schema_version,
                    _V,
                    "the source launcher's --help must advertise its schema intact "
                    f"at COLUMNS={cols} (no mid-token wrap)",
                )
                self.assertTrue(
                    decide_launcher_capability(obs, required_schema_version=_V).ok
                )

    def test_token_is_hyphen_and_whitespace_free(self):
        token = build_attest_capability_contract_line(_V)
        self.assertNotIn("-", token)
        self.assertNotIn(" ", token)


class PreflightAdapter(unittest.TestCase):
    def test_capable_launcher_passes(self):
        self.assertIsNone(
            preflight_attest_launcher_capability(
                "/abs/launcher", _runner(_capable_help()), 5.0, {}
            )
        )

    def test_v1_launcher_raises_typed_incompatible(self):
        with self.assertRaises(HerdrLauncherIncompatibleError) as ctx:
            preflight_attest_launcher_capability(
                "/abs/launcher",
                _runner("usage: agent-attest --assigned-name NAME\n"),
                5.0,
                {},
            )
        self.assertEqual(ctx.exception.reason, LAUNCHER_SCHEMA_CONTRACT_ABSENT)
        # The typed error is still a HerdrSessionStartError so existing fail-closed callers
        # keep aborting.
        self.assertIsInstance(ctx.exception, HerdrSessionStartError)

    def test_mechanical_failure_stays_plain_not_typed(self):
        # A non-zero exit is a mechanical failure, not a capability verdict — it must NOT be
        # the typed launcher-incompatible error (whose recovery is a schema upgrade).
        with self.assertRaises(HerdrSessionStartError) as ctx:
            preflight_attest_launcher_capability("/abs/launcher", _runner("", rc=2), 5.0, {})
        self.assertNotIsInstance(ctx.exception, HerdrLauncherIncompatibleError)

    def test_probe_run_failure_stays_plain(self):
        def boom(*a, **k):
            raise OSError("cannot exec")

        with self.assertRaises(HerdrSessionStartError) as ctx:
            preflight_attest_launcher_capability("/abs/launcher", boom, 5.0, {})
        self.assertNotIsInstance(ctx.exception, HerdrLauncherIncompatibleError)


class SublaneCreateSurfacesTypedBlocker(unittest.TestCase):
    """`sublane create/start` reports `launcher_runtime_incompatible`, zero-actuation."""

    def test_launcher_incompatible_blocks_distinctly_and_never_dispatches(self):
        ops = FakeActuatorOps(
            git=True,
            lanes=[None],
            append_error=SublaneLauncherIncompatibleError(
                "launcher incompatible", reason=LAUNCHER_SCHEMA_CONTRACT_ABSENT
            ),
        )
        outcome = SublaneActuateUseCase(ops).run(_req(), execute=True)
        self.assertEqual(outcome.status, ACTUATE_BLOCKED)
        self.assertIn(REASON_LAUNCHER_INCOMPATIBLE, outcome.blocked_reasons)
        # The underlying capability verdict reason is carried for the journal.
        self.assertIn(LAUNCHER_SCHEMA_CONTRACT_ABSENT, outcome.blocked_reasons)
        # Distinct from a generic pane-create failure (different recovery).
        self.assertNotIn(REASON_PANE_CREATE_FAILED, outcome.blocked_reasons)
        # Zero dispatch — nothing actuated past the failed launch.
        self.assertNotIn("dispatch", ops._names())


if __name__ == "__main__":
    unittest.main()
