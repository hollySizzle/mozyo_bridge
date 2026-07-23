"""Redmine #14250 — the agent-attest fail-closed tests must not observe host stderr.

``CmdAgentAttestArgv0DecouplingTest.test_nonexistent_alias_fails_closed`` asserted
equality on the WHOLE ``contextlib.redirect_stderr`` buffer. That buffer is the whole
process ``sys.stderr`` for the duration of the block, so any interpreter warning firing
during the call under test (``warnings.warn`` renders through ``sys.stderr``, and the
default filter emits it once per location — so which test pays for it depends on suite
order) became part of the comparison. Result: green alone, green pairwise, and red in
roughly one full-suite run in four, with a diff about an unrelated warning.

These tests drive the REAL unit-test methods, under injected stderr noise, through the
same seam the flake came in by — and pin both directions:

- with noise and without, the current structural assertion is GREEN (hermeticity);
- with noise, the OLD strict-equality form is RED and the noise marker is in its diff
  (negative control: the injection really reaches the buffer, so the green above is not
  vacuous), while without noise that same old form is green (the noise is the only
  discriminant);
- the structural assertion is still STRICT — a reworded, extra, or absent CLI diagnostic
  is red (the fail-closed contract was not loosened to buy the hermeticity).

Nothing here touches the fail-closed semantics, the provider launch, or a live herdr.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
import warnings
from unittest.mock import patch

import mozyo_bridge.shared.errors as errors_mod
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_agent_attest as attest_mod,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
    MOZYO_PROVIDER_ARGV0_ENV,
)

# A token no CLI diagnostic contains, so its presence in a failure diff proves the
# injected noise — not something else — is what the assertion compared against.
NOISE_MARKER = "mozyo-14250-simulated-environment-noise"

# The one that actually flaked, plus its siblings through the same shared assertion.
FLAKY_METHOD = "test_nonexistent_alias_fails_closed"
ALIAS_FAIL_CLOSED_METHODS = (
    FLAKY_METHOD,
    "test_relative_alias_fails_closed",
    "test_unrelated_absolute_alias_fails_closed",
    "test_different_target_symlink_alias_fails_closed",
)


def _warning_noise() -> None:
    """The real reported shape: an interpreter warning routed through ``sys.stderr``."""
    warnings.warn(NOISE_MARKER, UserWarning, stacklevel=2)


def _raw_stderr_noise() -> None:
    """Arbitrary host noise (wrapper banner / tool chatter), including a trailing line."""
    sys.stderr.write(f"{NOISE_MARKER} banner line 1\n{NOISE_MARKER} banner line 2\n")


NOISE_SHAPES = (("interpreter_warning", _warning_noise), ("raw_stderr", _raw_stderr_noise))


def _unit_case_class():
    """The real unit-test class, imported lazily so discovery does not re-collect it."""
    from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_agent_attest import (  # noqa: E501
        CmdAgentAttestArgv0DecouplingTest,
    )

    return CmdAgentAttestArgv0DecouplingTest


def _strict_equality_mutant_class():
    """The pre-#14250 assertion, restored on top of the real class.

    Defined inside a function so ``unittest discover`` never collects it as a test of
    its own — it exists only to be driven by the negative controls below.
    """
    base = _unit_case_class()
    from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_agent_attest import (  # noqa: E501
        ARGV0_ALIAS_UNBOUND_ERROR,
    )

    class _StrictEqualityMutant(base):  # pragma: no cover - driven explicitly
        def _assert_alias_fails_closed(self, provider_argv, alias) -> None:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                execv, execvp, leftover = self._run(
                    provider_argv, {MOZYO_PROVIDER_ARGV0_ENV: alias}
                )
            execv.assert_not_called()
            execvp.assert_not_called()
            self.assertIsNone(leftover)
            # The defect under regression: the WHOLE captured buffer is the verdict.
            self.assertEqual(stderr.getvalue(), ARGV0_ALIAS_UNBOUND_ERROR + "\n")

    return _StrictEqualityMutant


def _run_case(case_class, method, noise=None) -> unittest.TestResult:
    """Run one real test method, optionally with ``noise`` emitted mid-command.

    The seam is the alias-binding predicate the command calls between entering the
    ``redirect_stderr`` block and writing its diagnostic — exactly where a stray
    interpreter warning landed in production runs. The predicate's own behaviour is
    delegated to unchanged, so the scenario under test is the real one.
    """
    result = unittest.TestResult()
    case = case_class(method)
    if noise is None:
        case.run(result)
        return result

    original = attest_mod._argv0_alias_binds_to_exec_target

    def _noisy(argv0_alias, exec_target):
        noise()
        return original(argv0_alias, exec_target)

    with warnings.catch_warnings():
        # Default filters emit a given warning once per location; "always" makes the
        # injection deterministic no matter what ran earlier in the suite.
        warnings.simplefilter("always")
        with patch.object(attest_mod, "_argv0_alias_binds_to_exec_target", _noisy):
            case.run(result)
    return result


def _first_failure_text(result: unittest.TestResult) -> str:
    for _, text in result.failures + result.errors:
        return text
    return ""


def _verdict(result: unittest.TestResult) -> str:
    return (
        f"failures={len(result.failures)} errors={len(result.errors)}: "
        f"{_first_failure_text(result).splitlines()[-1] if not result.wasSuccessful() else ''}"
    )


class AgentAttestStderrHermeticityTest(unittest.TestCase):
    """The current assertion is indifferent to stderr the CLI did not write."""

    def test_flaky_method_is_green_without_injected_noise(self) -> None:
        # Baseline: whatever the noise cases prove, they must be measured against a
        # scenario that is green on its own.
        result = _run_case(_unit_case_class(), FLAKY_METHOD)
        self.assertTrue(result.wasSuccessful(), _verdict(result))

    def test_alias_fail_closed_methods_stay_green_under_injected_noise(self) -> None:
        for shape, noise in NOISE_SHAPES:
            for method in ALIAS_FAIL_CLOSED_METHODS:
                with self.subTest(shape=shape, method=method):
                    result = _run_case(_unit_case_class(), method, noise=noise)
                    self.assertTrue(result.wasSuccessful(), _verdict(result))

    def test_missing_provider_argv_assertion_survives_noise_too(self) -> None:
        # The sibling assertion in the same file was the same shape. Its die() is reached
        # before any alias code, so the noise is injected through die itself.
        from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_agent_attest import (  # noqa: E501
            CmdAgentAttestTest,
        )

        real_die = errors_mod.die

        def _noisy_die(message, code=2):
            _raw_stderr_noise()
            real_die(message, code)

        result = unittest.TestResult()
        with patch.object(errors_mod, "die", _noisy_die):
            CmdAgentAttestTest("test_missing_provider_argv_fails_closed").run(result)
        self.assertTrue(result.wasSuccessful(), _verdict(result))


class StrictEqualityMutantIsRedUnderNoiseTest(unittest.TestCase):
    """Negative control: the injection is real, and the old form is what it killed."""

    def test_old_form_is_green_without_noise(self) -> None:
        # Without the injection the old form passes — so the redness below is caused by
        # the noise, not by the mutant class being broken in some other way.
        result = _run_case(_strict_equality_mutant_class(), FLAKY_METHOD)
        self.assertTrue(result.wasSuccessful(), _verdict(result))

    def test_old_form_is_red_under_each_noise_shape(self) -> None:
        for shape, noise in NOISE_SHAPES:
            with self.subTest(shape=shape):
                result = _run_case(_strict_equality_mutant_class(), FLAKY_METHOD, noise)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(len(result.failures), 1, _verdict(result))
                self.assertEqual(result.errors, [])
                # Red for the right reason: the noise itself is in the compared value.
                self.assertIn(NOISE_MARKER, _first_failure_text(result))


class StructuralAssertionIsStillStrictTest(unittest.TestCase):
    """The hermeticity must not have been bought by accepting any diagnostic at all.

    Each case mutates what the CLI writes (the fail-closed contract's observable) and
    requires the CURRENT assertion to go red — including under injected noise, so the
    filter can never be the thing that swallows a contract violation.
    """

    def _run_with_cli_output(self, lines, noise=None) -> unittest.TestResult:
        def _fake_die(message, code=2):
            for line in lines:
                print(line, file=sys.stderr)
            raise SystemExit(code)

        with patch.object(errors_mod, "die", _fake_die):
            return _run_case(_unit_case_class(), FLAKY_METHOD, noise=noise)

    def test_reworded_diagnostic_is_red(self) -> None:
        result = self._run_with_cli_output(
            ["error: MOZYO_PROVIDER_ARGV0 did not verify"]
        )
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1, _verdict(result))

    def test_extra_cli_diagnostic_line_is_red(self) -> None:
        from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_agent_attest import (  # noqa: E501
            ARGV0_ALIAS_UNBOUND_ERROR,
        )

        result = self._run_with_cli_output(
            [ARGV0_ALIAS_UNBOUND_ERROR, "warning: launching anyway"]
        )
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1, _verdict(result))

    def test_silent_failure_is_red(self) -> None:
        result = self._run_with_cli_output([])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1, _verdict(result))

    def test_reworded_diagnostic_is_red_even_under_noise(self) -> None:
        # The filter drops host noise, never a contract violation hiding behind it.
        for shape, noise in NOISE_SHAPES:
            with self.subTest(shape=shape):
                result = self._run_with_cli_output(
                    ["error: MOZYO_PROVIDER_ARGV0 did not verify"], noise=noise
                )
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(len(result.failures), 1, _verdict(result))


class CliDiagnosticLinesTest(unittest.TestCase):
    """The classifier itself: what it keeps, and what it deliberately does not drop."""

    def _lines(self, text):
        from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_agent_attest import (  # noqa: E501
            cli_diagnostic_lines,
        )

        return cli_diagnostic_lines(text)

    def test_keeps_cli_prefixed_lines_in_order_and_drops_the_rest(self) -> None:
        self.assertEqual(
            self._lines(
                "/some/interpreter/path.py:12: UserWarning: noise\n"
                "  warnings.warn(...)\n"
                "error: first\n"
                "warning: second\n"
                "trailing host chatter\n"
            ),
            ["error: first", "warning: second"],
        )

    def test_empty_capture_yields_no_diagnostics(self) -> None:
        self.assertEqual(self._lines(""), [])

    def test_noise_wearing_a_cli_prefix_is_kept_not_swallowed(self) -> None:
        # The filter may only ever be stricter than an exact-buffer compare, never
        # laxer: anything shaped like a CLI diagnostic still counts against the verdict.
        self.assertEqual(
            self._lines("error: something else entirely\n"),
            ["error: something else entirely"],
        )

    def test_a_diagnostic_split_across_lines_is_not_silently_rejoined(self) -> None:
        # Only the prefixed line survives; a wrapped continuation is not treated as part
        # of the diagnostic, so a message that grew a second line shows up as a mismatch.
        self.assertEqual(
            self._lines("error: first half\ncontinued second half\n"),
            ["error: first half"],
        )


if __name__ == "__main__":
    unittest.main()
