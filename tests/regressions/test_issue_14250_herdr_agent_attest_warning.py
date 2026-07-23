"""Redmine #14250 — the agent-attest fail-closed tests must observe the CLI, not the host.

``CmdAgentAttestArgv0DecouplingTest.test_nonexistent_alias_fails_closed`` asserted
equality on the WHOLE ``contextlib.redirect_stderr`` buffer. That buffer is the whole
process ``sys.stderr`` for the duration of the block, so any interpreter warning firing
during the call under test (``warnings.warn`` renders through ``sys.stderr``, and the
default filter emits it once per location — so which test pays for it depends on suite
order) became part of the comparison. Green alone, green pairwise, red in about one full
suite run in four.

R1 fixed that by keeping only stderr lines with a CLI prefix. **R1-F1 (review j#86284,
verdict j#86286) showed that was wrong**: ``die`` does not forbid newlines in its message,
so a diagnostic that grows a continuation renders prefix-less physical lines. The filter
dropped them, and a real change to the fail-closed diagnostic stayed green — the very
contract this issue promises not to loosen.

R2 stops inferring *who wrote it* from *what it looks like*: ``die`` / ``warn`` are wrapped
and the exact message (newlines included) and exit code the CLI passed are recorded at the
call, then delegated to the real implementation. These tests pin, in both directions:

- host noise never changes the verdict (the original defect);
- the recorded contract is exact — continuation, rewording, extra, missing, duplicated, or
  a changed exit code is red (F1, plus the R1 form demonstrated green on the same record);
- a diagnostic that is recorded but never reaches stderr, or reaches it as different text,
  is red;
- all of the above stay red under injected noise.

Nothing here touches the fail-closed semantics, the provider launch, or a live herdr.
"""

from __future__ import annotations

import sys
import unittest
import warnings
from unittest.mock import patch

import mozyo_bridge.shared.errors as errors_mod
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_agent_attest as attest_mod,
)

from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_agent_attest import (  # noqa: E501
    ARGV0_ALIAS_UNBOUND_MESSAGE,
    assert_cli_diagnostics,
    observed_cli_diagnostics,
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

EXPECTED = [("error", ARGV0_ALIAS_UNBOUND_MESSAGE, 2)]


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


def _run_case(case_class, method, noise=None) -> unittest.TestResult:
    """Run one real test method, optionally with ``noise`` emitted mid-command.

    The seam is the alias-binding predicate the command calls between entering the capture
    and writing its diagnostic — exactly where a stray interpreter warning landed in
    production runs. The predicate's own behaviour is delegated to unchanged, so the
    scenario under test is the real one.
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
    tail = "" if result.wasSuccessful() else _first_failure_text(result).splitlines()[-1]
    return f"failures={len(result.failures)} errors={len(result.errors)}: {tail}"


def _record_of(diagnostics, noise=None):
    """A GENUINE record: the real ``die`` / ``warn``, through the real observation seam.

    ``diagnostics`` is what a CLI would report — ``(kind, message, code)``. This is how a
    changed contract is simulated: R2 records what the CLI *passed to* ``die``, and the
    command passes a literal, so the mutation has to be applied at the call. Everything
    else (the wrapper, the real ``die``, the rendering onto stderr) is unmodified.
    """
    with observed_cli_diagnostics() as record:
        if noise is not None:
            noise()
        for kind, message, code in diagnostics:
            if kind == "error":
                try:
                    errors_mod.die(message, code)
                except SystemExit:
                    pass
            else:
                errors_mod.warn(message)
    return record


class AgentAttestStderrHermeticityTest(unittest.TestCase):
    """The original defect: host stderr must not reach the verdict."""

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

    def test_noise_does_not_disturb_an_otherwise_exact_record(self) -> None:
        for shape, noise in NOISE_SHAPES:
            with self.subTest(shape=shape):
                record = _record_of(EXPECTED, noise=noise)
                assert_cli_diagnostics(self, record, EXPECTED)  # must not raise


class ContinuationLineIsPartOfTheContractTest(unittest.TestCase):
    """R1-F1 (review j#86284): the defect that the R1 prefix filter let through.

    ``die`` renders ``print(f"error: {message}")``, so a message containing a newline
    produces prefix-less physical lines. R1 dropped them; R2 records the message itself.
    """

    def test_appended_continuation_is_red(self) -> None:
        record = _record_of([("error", ARGV0_ALIAS_UNBOUND_MESSAGE + "\nAND ALSO", 2)])
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, record, EXPECTED)

    def test_continuation_that_reverses_the_meaning_is_red(self) -> None:
        reversed_msg = ARGV0_ALIAS_UNBOUND_MESSAGE + "\n(launching anyway)"
        record = _record_of([("error", reversed_msg, 2)])
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, record, EXPECTED)

    def test_the_r1_prefix_filter_is_green_on_the_same_record(self) -> None:
        # Negative control for the fix itself: on the record above, the R1 form passes.
        # That is what F1 reported, and it is why the classifier was replaced rather than
        # patched — this test fails the day someone reintroduces prefix classification.
        record = _record_of([("error", ARGV0_ALIAS_UNBOUND_MESSAGE + "\nAND ALSO", 2)])
        r1_lines = [
            line
            for line in record.stderr.getvalue().splitlines()
            if line.startswith(("error: ", "warning: "))
        ]
        self.assertEqual(r1_lines, ["error: " + ARGV0_ALIAS_UNBOUND_MESSAGE])

    def test_appended_continuation_is_red_under_noise_too(self) -> None:
        for shape, noise in NOISE_SHAPES:
            with self.subTest(shape=shape):
                record = _record_of(
                    [("error", ARGV0_ALIAS_UNBOUND_MESSAGE + "\nAND ALSO", 2)],
                    noise=noise,
                )
                with self.assertRaises(AssertionError):
                    assert_cli_diagnostics(self, record, EXPECTED)


class RecordedContractIsExactTest(unittest.TestCase):
    """Every other way the CLI's reported contract can drift is red."""

    def test_exact_match_is_green(self) -> None:
        assert_cli_diagnostics(self, _record_of(EXPECTED), EXPECTED)

    def test_reworded_message_is_red(self) -> None:
        record = _record_of([("error", "MOZYO_PROVIDER_ARGV0 did not verify", 2)])
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, record, EXPECTED)

    def test_extra_diagnostic_is_red(self) -> None:
        record = _record_of(
            [("error", ARGV0_ALIAS_UNBOUND_MESSAGE, 2), ("warning", "launching anyway", None)]
        )
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, record, EXPECTED)

    def test_silent_failure_is_red(self) -> None:
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, _record_of([]), EXPECTED)

    def test_duplicated_diagnostic_is_red(self) -> None:
        record = _record_of(
            [("error", ARGV0_ALIAS_UNBOUND_MESSAGE, 2)] * 2
        )
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, record, EXPECTED)

    def test_changed_exit_code_is_red(self) -> None:
        # Same words, different fail-closed outcome: still a contract change.
        record = _record_of([("error", ARGV0_ALIAS_UNBOUND_MESSAGE, 3)])
        with self.assertRaises(AssertionError):
            assert_cli_diagnostics(self, record, EXPECTED)


class RecordedDiagnosticMustReachStderrTest(unittest.TestCase):
    """A recorded diagnostic that never lands, or lands as different text, is red.

    Driven end-to-end through the real command: ``die`` is replaced with one that renders
    something other than what it was handed, so the record and the sink disagree.
    """

    def _run_with_rendered(self, lines, noise=None) -> unittest.TestResult:
        def _diverging_die(message, code=2):
            for line in lines:
                print(line, file=sys.stderr)
            raise SystemExit(code)

        with patch.object(errors_mod, "die", _diverging_die):
            return _run_case(_unit_case_class(), FLAKY_METHOD, noise=noise)

    def test_nothing_rendered_is_red(self) -> None:
        result = self._run_with_rendered([])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1, _verdict(result))

    def test_different_text_rendered_is_red(self) -> None:
        result = self._run_with_rendered(["error: something else entirely"])
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1, _verdict(result))

    def test_extra_rendered_cli_line_is_red(self) -> None:
        result = self._run_with_rendered(
            ["error: " + ARGV0_ALIAS_UNBOUND_MESSAGE, "warning: launching anyway"]
        )
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1, _verdict(result))

    def test_divergence_is_red_under_noise_too(self) -> None:
        # The capture may drop host noise, never a contract violation hiding behind it.
        for shape, noise in NOISE_SHAPES:
            with self.subTest(shape=shape):
                result = self._run_with_rendered(
                    ["error: something else entirely"], noise=noise
                )
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(len(result.failures), 1, _verdict(result))


class DieRenderingContractTest(unittest.TestCase):
    """The other half of the chain: how ``die`` / ``warn`` render what they are handed.

    R2 pins the CLI's contract as *the message it passed*, which only determines the
    bytes on stderr if the renderer is itself pinned — and nothing in the suite pinned it.
    Without these, a renderer that appended a line would be invisible to every CLI test.
    Together with the exact recorded message, this fixes the whole observable.

    Capture is deliberately whole-buffer here: the block contains one call to a two-line
    function with no collaborators, and the warnings channel is silenced for it, so there
    is nothing else that could write.
    """

    def _render(self, emit):
        import contextlib
        import io

        stderr = io.StringIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with contextlib.redirect_stderr(stderr):
                emit()
        return stderr.getvalue()

    def test_die_renders_exactly_one_prefixed_block_and_exits_2(self) -> None:
        captured = {}

        def _emit():
            try:
                errors_mod.die("some message")
            except SystemExit as exc:
                captured["code"] = exc.code

        self.assertEqual(self._render(_emit), "error: some message\n")
        self.assertEqual(captured["code"], 2)

    def test_die_renders_a_multiline_message_inline(self) -> None:
        # Exactly why the R1 prefix filter was unsound: the continuation is a physical
        # line with no prefix, indistinguishable by shape from host noise.
        def _emit():
            try:
                errors_mod.die("first half\nsecond half")
            except SystemExit:
                pass

        self.assertEqual(self._render(_emit), "error: first half\nsecond half\n")

    def test_die_honours_a_non_default_code(self) -> None:
        captured = {}

        def _emit():
            try:
                errors_mod.die("some message", 3)
            except SystemExit as exc:
                captured["code"] = exc.code

        self._render(_emit)
        self.assertEqual(captured["code"], 3)

    def test_warn_renders_one_prefixed_line_and_does_not_exit(self) -> None:
        self.assertEqual(
            self._render(lambda: errors_mod.warn("some notice")), "warning: some notice\n"
        )


class StrictEqualityMutantIsRedUnderNoiseTest(unittest.TestCase):
    """The original defect's negative control: the injection is real.

    The pre-#14250 assertion is restored on top of the real class and driven through the
    same scenarios, so the green results above cannot be vacuous.
    """

    def _mutant_class(self):
        base = _unit_case_class()
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (  # noqa: E501
            MOZYO_PROVIDER_ARGV0_ENV,
        )
        import contextlib
        import io

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
                self.assertEqual(
                    stderr.getvalue(), "error: " + ARGV0_ALIAS_UNBOUND_MESSAGE + "\n"
                )

        return _StrictEqualityMutant

    def test_old_form_is_green_without_noise(self) -> None:
        # Without the injection the old form passes — so the redness below is caused by
        # the noise, not by the mutant class being broken in some other way.
        result = _run_case(self._mutant_class(), FLAKY_METHOD)
        self.assertTrue(result.wasSuccessful(), _verdict(result))

    def test_old_form_is_red_under_each_noise_shape(self) -> None:
        for shape, noise in NOISE_SHAPES:
            with self.subTest(shape=shape):
                result = _run_case(self._mutant_class(), FLAKY_METHOD, noise)
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(len(result.failures), 1, _verdict(result))
                self.assertEqual(result.errors, [])
                # Red for the right reason: the noise itself is in the compared value.
                self.assertIn(NOISE_MARKER, _first_failure_text(result))


if __name__ == "__main__":
    unittest.main()
