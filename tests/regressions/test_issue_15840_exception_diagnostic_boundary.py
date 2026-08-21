"""Regression pins for the #15840 exception-diagnostic boundary (parent US #15839).

Measured on 2026-08-20: ``src/`` imports ``logging`` zero times, and of 769 broad
``except Exception`` / ``except BaseException`` handlers, 578 (75%) discard the exception
entirely — 124 of those in mutating paths. The sharpest instance is the retire application's
terminal handler, whose own comment says ``an exception may be after a side effect`` while the
result it returns carries no trace of what was raised.

Measured cost (#15789 j#109134): an investigation returned ``uncertain`` /
``retire_application_error``, the cause could not be read off the result, and recovering it
required instrumenting a throwaway harness and re-running. It turned out to be a
``git worktree add`` refusal whose message was the load-bearing evidence for that issue's
entire fix.

What is pinned here, in one file per the R3-c same-issue grouping rule:

1. a CLOSED-VOCABULARY failure kind reaches the caller -- never a string the exception
   carries. Review j#109671 reproduced both ways the first attempt (``type(exc).__name__``)
   was wrong, and those two counter-examples are pinned directly below;
2. **the message, the traceback and any path do NOT** — the boundary in
   ``vibes/docs/logics/exception-diagnostic-sink-boundary.md`` allows raw only in a host-local
   sink and forbids copying it into a durable record, and this value flows into CLI JSON that
   gets pasted into Redmine journals;
3. the reason stays a prefix of the pre-#15840 token, so no consumer is renamed out from under;
4. the typed refusal vocabulary is untouched — a deterministic refusal is still a deterministic
   refusal, and only the genuinely-unexpected path gained a failure kind;
5. the terminal handler stays TOTAL: it must return an ``uncertain`` result even when reading
   anything off the raised object would itself raise.

Point 2 is the load-bearing pin. It fails loudly if a later slice wires the host-local sink and
lets the raw text leak into the returned value on the way.

Boundary: no repository writes, no lane mutation, no herdr / tmux contact. The exception is
injected by patching one collaborator to raise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = _TESTS_ROOT.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402,E501
    retire_admissibility,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.diagnostic_sink_fence import (  # noqa: E402,E501
    FENCE_MISSING_COMPONENT,
    FENCE_SYMLINK_COMPONENT,
    open_sink_directory,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.diagnostic_sink_location import (  # noqa: E402,E501
    SINK_FORBIDDEN_ROOT_NOT_ABSOLUTE,
    SINK_INSIDE_FORBIDDEN_ROOT,
    SINK_IS_FORBIDDEN_ROOT,
    SINK_NOT_ABSOLUTE,
    SINK_NO_CANDIDATE,
    SINK_NO_FORBIDDEN_ROOTS,
    resolve_diagnostic_sink_root,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E402,E501
    _DURABLE_FAILURE_KINDS,
    REASON_APPLICATION_ERROR,
    REASON_APPLICATION_ERROR_SEPARATOR,
    REASON_EXCEPTION_UNCLASSIFIED,
    RETIRE_RESULT_UNCERTAIN,
    RetireApplicationRequest,
    RetireAssertions,
    run_retire_application,
)

#: A message shaped like the one that actually cost the time: it embeds an absolute path.
#: ``lane_metadata`` declares such a path host-local private state that must never reach a
#: durable Redmine record, so it is exactly what must NOT come back in the result.
_SECRET_PATH = "/private/tmp/lane_wt_should_never_leak"
_RAISED_MESSAGE = (
    f"fatal: '{_SECRET_PATH}' is a missing but already registered worktree; "
    "use 'add -f' to override, or 'prune'"
)


class _LeakyFailure(RuntimeError):
    """An exception whose message carries a path, as real subprocess failures do."""


def _request(repo_root: Path) -> RetireApplicationRequest:
    return RetireApplicationRequest(
        repo_root=repo_root,
        issue="15840",
        lane_label="issue_15840_probe",
        assertions=RetireAssertions(),
    )


def _run_with_raised(exc: BaseException):
    """Drive the real application facade with one collaborator raising ``exc``."""
    with mock.patch.object(
        retire_admissibility, "resolve_retire_evidence_target", side_effect=exc
    ):
        return run_retire_application(_request(Path(__file__).resolve().parents[2]))


class TheUnexpectedExceptionNowNamesItsType(unittest.TestCase):
    def test_the_result_is_still_uncertain_not_a_deterministic_refusal(self):
        """The #15066 contract holds: exceptions never masquerade as a typed refusal."""
        result = _run_with_raised(_LeakyFailure(_RAISED_MESSAGE))
        self.assertEqual(result.state, RETIRE_RESULT_UNCERTAIN)
        self.assertTrue(result.uncertain)
        self.assertFalse(result.mutated)

    def test_a_recognised_failure_kind_reaches_the_caller(self):
        """A subprocess failure is named -- the distinction this diagnostic exists for."""
        result = _run_with_raised(subprocess.CalledProcessError(1, "git"))
        self.assertEqual(
            result.reason,
            REASON_APPLICATION_ERROR
            + REASON_APPLICATION_ERROR_SEPARATOR
            + "called_process_error",
        )

    def test_a_different_failure_kind_is_distinguishable(self):
        """The whole point: subprocess failure vs logic bug must be told apart."""
        first = _run_with_raised(subprocess.CalledProcessError(1, "git"))
        second = _run_with_raised(TypeError("unrelated logic bug"))
        self.assertNotEqual(first.reason, second.reason)
        self.assertTrue(second.reason.endswith("type_error"), second.reason)

    def test_an_unrecognised_type_degrades_to_the_fixed_literal(self):
        result = _run_with_raised(_LeakyFailure(_RAISED_MESSAGE))
        self.assertEqual(
            result.reason,
            REASON_APPLICATION_ERROR
            + REASON_APPLICATION_ERROR_SEPARATOR
            + REASON_EXCEPTION_UNCLASSIFIED,
        )

    def test_the_reason_stays_a_prefix_of_the_pre_change_token(self):
        """No consumer is renamed out from under: the old token is still the prefix."""
        result = _run_with_raised(_LeakyFailure(_RAISED_MESSAGE))
        self.assertTrue(
            result.reason.startswith(REASON_APPLICATION_ERROR),
            result.reason,
        )


class TheRawTextNeverCrossesTheBoundary(unittest.TestCase):
    """The load-bearing pin. Raw belongs in a host-local sink, never in this value.

    This value reaches CLI JSON and from there Redmine journals. A later slice will wire the
    host-local sink; these assertions fail loudly if the raw text is allowed to ride along.
    """

    def test_the_exception_message_is_not_returned(self):
        result = _run_with_raised(_LeakyFailure(_RAISED_MESSAGE))
        self.assertNotIn(_RAISED_MESSAGE, result.reason)
        self.assertNotIn("missing but already registered", result.reason)

    def test_no_path_from_the_message_is_returned(self):
        result = _run_with_raised(_LeakyFailure(_RAISED_MESSAGE))
        self.assertNotIn(_SECRET_PATH, result.reason)
        self.assertNotIn(_SECRET_PATH, json.dumps(result.as_payload(), ensure_ascii=False))

    def test_the_whole_serialized_payload_is_free_of_the_raw_text(self):
        """`as_payload` is what the CLI prints; check the serialized form, not just a field."""
        result = _run_with_raised(_LeakyFailure(_RAISED_MESSAGE))
        serialized = json.dumps(result.as_payload(), ensure_ascii=False)
        for forbidden in (_SECRET_PATH, _RAISED_MESSAGE, "add -f", "prune"):
            self.assertNotIn(forbidden, serialized, forbidden)

    def test_no_traceback_frame_is_returned(self):
        """A traceback names source paths of the host; none of it may cross."""
        try:
            raise _LeakyFailure(_RAISED_MESSAGE)
        except _LeakyFailure as exc:
            captured = exc
            frames = traceback.format_exception(
                type(exc), exc, exc.__traceback__
            )
        result = _run_with_raised(captured)
        serialized = json.dumps(result.as_payload(), ensure_ascii=False)
        self.assertNotIn("Traceback", result.reason)
        self.assertNotIn("Traceback", serialized)
        self.assertNotIn(__file__, serialized)
        self.assertTrue(frames, "precondition: a traceback was actually formatted")

    def test_only_a_token_from_the_closed_table_is_appended(self):
        """The replacement for a pin that did not hold (review j#109671).

        The first version asserted ``isidentifier()`` and called a class name a type-level
        guarantee. It is not: ``type()`` takes an arbitrary string, so an identifier-shaped
        secret passed that pin. The property that actually holds is membership of a closed
        table written in this repository's own source.
        """
        allowed = {token for _cls, token in _DURABLE_FAILURE_KINDS}
        allowed.add(REASON_EXCEPTION_UNCLASSIFIED)
        for raised in (
            subprocess.CalledProcessError(1, "git"),
            FileNotFoundError(_SECRET_PATH),
            TypeError("x"),
            _LeakyFailure(_RAISED_MESSAGE),
        ):
            appended = _run_with_raised(raised).reason[
                len(REASON_APPLICATION_ERROR + REASON_APPLICATION_ERROR_SEPARATOR) :
            ]
            self.assertIn(appended, allowed, appended)


class TheReviewCounterExamplesStayClosed(unittest.TestCase):
    """The two counter-examples from review j#109671, pinned as tests rather than prose.

    The first implementation appended ``type(exc).__name__`` and justified it as a type-level
    guarantee. Both halves of that were false, and both are reproduced here so the claim can
    never be re-adopted silently.
    """

    def test_an_identifier_shaped_secret_in_a_class_name_does_not_leak(self):
        """`finding_unsafeexceptiontype`: `type()` takes an ARBITRARY string as the name.

        The superseded pin asserted `isidentifier()`, which this class name satisfies — so the
        old regression accepted exactly the leak it claimed to prevent.
        """
        secret = "SECRET_TOKEN_VALUE_123"
        leaking = type(secret, (RuntimeError,), {})
        self.assertTrue(secret.isidentifier(), "precondition: the secret is identifier-shaped")

        result = _run_with_raised(leaking("boom"))
        self.assertNotIn(secret, result.reason)
        self.assertNotIn(
            secret, json.dumps(result.as_payload(), ensure_ascii=False)
        )
        self.assertTrue(result.reason.endswith(REASON_EXCEPTION_UNCLASSIFIED), result.reason)

    def test_a_metaclass_whose_name_raises_still_yields_an_uncertain_result(self):
        """`finding_terminalhandlerescape`: the handler is the last line and must be total.

        Reading `__name__` inside it let a hostile metaclass throw straight past the caller,
        breaking the #15066 contract that unexpected failures arrive as a typed result.
        """

        class _RaisingName(type):
            @property
            def __name__(cls):  # noqa: D105 - the whole point is that it raises
                raise RuntimeError("metaclass __name__ raised")

        hostile = _RaisingName("Hostile", (RuntimeError,), {})
        with self.assertRaises(RuntimeError):
            _ = hostile.__name__  # precondition: reading the name really does raise

        result = _run_with_raised(hostile("boom"))
        self.assertEqual(result.state, RETIRE_RESULT_UNCERTAIN)
        self.assertTrue(result.uncertain)
        self.assertTrue(result.reason.endswith(REASON_EXCEPTION_UNCLASSIFIED), result.reason)


    def test_a_metaclass_whose_mro_raises_still_yields_an_uncertain_result(self):
        """The classifier's own guard, exercised rather than assumed.

        A first mutation attempt failed to reach this guard — the hostile-``__name__`` class
        above never touches ``__mro__`` — so it was pinned separately instead of being left
        as untested defensive code.
        """

        class _RaisingMro(type):
            @property
            def __mro__(cls):  # noqa: D105 - the whole point is that it raises
                raise RuntimeError("mro raised")

        hostile = _RaisingMro("HostileMro", (RuntimeError,), {})
        with self.assertRaises(RuntimeError):
            _ = hostile.__mro__  # precondition: reading the mro really does raise

        result = _run_with_raised(hostile("boom"))
        self.assertEqual(result.state, RETIRE_RESULT_UNCERTAIN)
        self.assertTrue(result.reason.endswith(REASON_EXCEPTION_UNCLASSIFIED), result.reason)

    def test_an_mro_that_raises_while_iterating_still_yields_an_uncertain_result(self):
        """The second guard: the walk itself, not only obtaining the sequence."""

        class _BadSequence:
            def __iter__(self):
                raise RuntimeError("mro iteration raised")

        class _BadIterMro(type):
            @property
            def __mro__(cls):  # noqa: D105
                return _BadSequence()

        hostile = _BadIterMro("HostileIter", (RuntimeError,), {})
        result = _run_with_raised(hostile("boom"))
        self.assertEqual(result.state, RETIRE_RESULT_UNCERTAIN)
        self.assertTrue(result.reason.endswith(REASON_EXCEPTION_UNCLASSIFIED), result.reason)


class TheDeterministicRefusalsAreUntouched(unittest.TestCase):
    """The typed refusal vocabulary is out of this issue's scope and must not move."""

    def test_a_non_applicable_flag_still_returns_its_own_typed_reason(self):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_retire_application import (  # noqa: E501
            REASON_WORKTREE_ABSENT_NOT_APPLICABLE,
            RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
            RETIRE_RESULT_BLOCKED,
        )

        request = RetireApplicationRequest(
            repo_root=Path(__file__).resolve().parents[2],
            issue="15840",
            lane_label="issue_15840_probe",
            assertions=RetireAssertions(),
            intent=RETIRE_INTENT_ACTIVE_UNBOUND_LIVE_ZERO,
            worktree_absent=True,
        )
        result = run_retire_application(request)
        self.assertEqual(result.state, RETIRE_RESULT_BLOCKED)
        self.assertEqual(result.reason, REASON_WORKTREE_ABSENT_NOT_APPLICABLE)
        self.assertNotIn(REASON_APPLICATION_ERROR, result.reason)


class TheSinkRootRefusesEveryForbiddenSurface(unittest.TestCase):
    """Review j#109680 ``finding_xdgforbiddenoverlap``.

    The design first argued the XDG sink was outside the guarded home because
    ``ambient_homes()`` returns only two paths. That holds for the DEFAULT ``XDG_STATE_HOME``
    only — it is environment input, and the review reproduced it pointing straight into a
    guarded home. Enumerating forbidden surfaces in a table does nothing unless something
    refuses to resolve into them, so the refusal is pinned here per case.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.guarded = self.root / "guarded_home"
        self.repo = self.root / "repo"
        for path in (self.guarded, self.repo):
            path.mkdir()
        self.forbidden = (self.guarded, self.repo)

    def _resolve(self, candidate, forbidden=None):
        return resolve_diagnostic_sink_root(
            candidate,
            forbidden_roots=self.forbidden if forbidden is None else forbidden,
        )

    def test_a_candidate_outside_every_forbidden_root_is_admitted(self):
        """The control: the rule must not refuse everything."""
        outside = self.root / "state" / "mozyo-bridge" / "diagnostics"
        result = self._resolve(outside)
        self.assertTrue(result.admissible, result.detail)
        self.assertEqual(result.root, outside.resolve())

    def test_the_guarded_home_itself_is_refused(self):
        result = self._resolve(self.guarded)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_IS_FORBIDDEN_ROOT)

    def test_a_descendant_of_the_guarded_home_is_refused(self):
        """The exact shape the review reproduced: XDG_STATE_HOME pointed at the guarded home."""
        result = self._resolve(self.guarded / "mozyo-bridge" / "diagnostics")
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_INSIDE_FORBIDDEN_ROOT)

    def test_the_repo_root_itself_is_refused(self):
        result = self._resolve(self.repo)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_IS_FORBIDDEN_ROOT)

    def test_a_descendant_of_the_repo_is_refused(self):
        """Raw diagnostics inside a checkout get committed and shared."""
        result = self._resolve(self.repo / ".mozyo-bridge" / "diagnostics")
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_INSIDE_FORBIDDEN_ROOT)

    def test_a_symlink_into_a_forbidden_root_is_refused(self):
        """String comparison would pass this. Canonicalization is why it does not."""
        link = self.root / "looks_safe"
        link.symlink_to(self.guarded, target_is_directory=True)
        result = self._resolve(link / "diagnostics")
        self.assertFalse(result.admissible, result.as_payload())
        self.assertEqual(result.reason, SINK_INSIDE_FORBIDDEN_ROOT)

    def test_an_empty_forbidden_set_refuses_rather_than_admitting_everything(self):
        """If the wiring is ever forgotten, the failure direction must be refusal."""
        result = self._resolve(self.root / "state", forbidden=())
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_NO_FORBIDDEN_ROOTS)

    def test_a_relative_candidate_is_refused(self):
        result = self._resolve(Path("relative/diagnostics"))
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_NOT_ABSOLUTE)

    def test_no_candidate_is_refused(self):
        result = self._resolve(None)
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_NO_CANDIDATE)

    def test_the_documented_default_lands_outside_a_realistically_placed_home(self):
        """The default spelling still works — the rule constrains, it does not forbid."""
        home = self.root / "home"
        state = home / ".local" / "state" / "mozyo-bridge" / "diagnostics"
        guarded = home / ".mozyo_bridge"
        guarded.mkdir(parents=True)
        result = resolve_diagnostic_sink_root(state, forbidden_roots=(guarded, self.repo))
        self.assertTrue(result.admissible, result.detail)


class ARelativeForbiddenRootIsRefusedRatherThanMisresolved(unittest.TestCase):
    """Review j#109685 ``finding_relativeforbiddenroot``.

    The candidate's absoluteness was required from the start; the roots' was not. That
    asymmetry was fail-OPEN: ``Path("repo").resolve()`` anchors on the process working
    directory, so a caller passing a relative repo root got a root that was not the one it
    meant, and candidates under the real repo were admitted.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.actual_repo = self.root / "actual-repo"
        (self.actual_repo / "sub").mkdir(parents=True)
        self.elsewhere = self.root / "cwd"
        self.elsewhere.mkdir()

    def test_a_relative_root_does_not_admit_a_candidate_under_the_intended_root(self):
        previous = Path.cwd()
        os.chdir(self.elsewhere)
        self.addCleanup(os.chdir, previous)

        result = resolve_diagnostic_sink_root(
            self.actual_repo / "sub" / "diagnostics",
            forbidden_roots=(Path("actual-repo"),),
        )
        self.assertFalse(result.admissible, result.as_payload())
        self.assertEqual(result.reason, SINK_FORBIDDEN_ROOT_NOT_ABSOLUTE)

    def test_the_same_root_spelled_absolutely_still_refuses_by_descent(self):
        """The refusal above is about the spelling, not about losing the real rule."""
        result = resolve_diagnostic_sink_root(
            self.actual_repo / "sub" / "diagnostics",
            forbidden_roots=(self.actual_repo,),
        )
        self.assertFalse(result.admissible)
        self.assertEqual(result.reason, SINK_INSIDE_FORBIDDEN_ROOT)


class TheAtUseFenceSurvivesSubstitutionAfterAdmission(unittest.TestCase):
    """Review j#109685 ``finding_staleadmissionrace``.

    ``resolve_diagnostic_sink_root`` is a decision about a path string at one instant.
    ``Path.resolve()`` is non-strict, so a candidate under an ancestor that does not exist yet
    is admitted — and replacing that ancestor with a symlink afterwards moves the admitted root
    inside a forbidden one. The earlier symlink pin only covered links present at decision time.

    The fence is the other half: it walks components with ``dir_fd``-relative opens and
    ``O_NOFOLLOW``, so a symlink planted at ANY time is a refusal rather than a redirection.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.guarded = self.root / "guarded"
        self.guarded.mkdir()
        self.ancestor = self.root / "state"
        self.sink = self.ancestor / "mozyo-bridge" / "diagnostics"

    def _close(self, result):
        if result.dir_fd is not None:
            os.close(result.dir_fd)

    def test_admission_succeeds_before_the_substitution(self):
        """Precondition: the race is real, i.e. the preflight really does admit this."""
        admitted = resolve_diagnostic_sink_root(
            self.sink, forbidden_roots=(self.guarded,)
        )
        self.assertTrue(admitted.admissible, admitted.as_payload())

    def test_the_fence_refuses_a_symlink_planted_after_admission(self):
        resolve_diagnostic_sink_root(self.sink, forbidden_roots=(self.guarded,))
        self.ancestor.symlink_to(self.guarded, target_is_directory=True)

        result = open_sink_directory(self.sink, create=True)
        self.addCleanup(self._close, result)
        self.assertFalse(result.ok, result.as_payload())
        self.assertEqual(result.reason, FENCE_SYMLINK_COMPONENT)

    def test_nothing_is_written_under_the_forbidden_root(self):
        """The property that actually matters: zero write, not merely a refusal token."""
        resolve_diagnostic_sink_root(self.sink, forbidden_roots=(self.guarded,))
        self.ancestor.symlink_to(self.guarded, target_is_directory=True)

        result = open_sink_directory(self.sink, create=True)
        self.addCleanup(self._close, result)
        self.assertEqual(
            list(self.guarded.rglob("*")), [], "the fence created something inside the guard"
        )

    def test_the_fence_does_not_leak_a_descriptor_per_refusal(self):
        """Refusal is the common case on a diagnostic path.

        Leaking one descriptor per refusal would exhaust the process exactly when things are
        already going wrong. Found while pinning the counter-example, not by it.
        """
        self.ancestor.symlink_to(self.guarded, target_is_directory=True)
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.is_dir():
            self.skipTest("descriptor introspection unavailable on this platform")

        before = len(os.listdir(fd_dir))
        for _ in range(64):
            result = open_sink_directory(self.sink, create=True)
            self.assertFalse(result.ok)
        self.assertLessEqual(len(os.listdir(fd_dir)) - before, 1)

    def test_the_fence_creates_the_sink_when_the_path_is_clean(self):
        """Control: the fence constrains, it does not forbid. Directories are 0700."""
        result = open_sink_directory(self.sink, create=True)
        self.addCleanup(self._close, result)
        self.assertTrue(result.ok, result.as_payload())
        self.assertTrue(self.sink.is_dir())
        self.assertEqual(self.ancestor.stat().st_mode & 0o777, 0o700)

    def test_the_fence_refuses_a_missing_component_when_not_creating(self):
        result = open_sink_directory(self.sink, create=False)
        self.addCleanup(self._close, result)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, FENCE_MISSING_COMPONENT)

    def test_the_fence_reports_a_symlink_as_a_symlink(self):
        """Linux returns ENOTDIR (not ELOOP) for O_NOFOLLOW|O_DIRECTORY on a symlink.

        The refusal is correct either way; reporting it as "not a directory" would send the
        next reader after the wrong problem, so the reason is disambiguated.
        """
        self.ancestor.symlink_to(self.guarded, target_is_directory=True)
        result = open_sink_directory(self.sink, create=True)
        self.addCleanup(self._close, result)
        self.assertEqual(result.reason, FENCE_SYMLINK_COMPONENT)

    def test_the_fence_payload_carries_no_path(self):
        """The fence's own diagnostic obeys Decision 3 as well."""
        self.ancestor.symlink_to(self.guarded, target_is_directory=True)
        result = open_sink_directory(self.sink, create=True)
        self.addCleanup(self._close, result)
        serialized = json.dumps(result.as_payload(), ensure_ascii=False)
        self.assertNotIn(str(self.guarded), serialized)
        self.assertNotIn(str(self.sink), serialized)


class TheBoundaryIsWrittenDown(unittest.TestCase):
    """The decision must be readable, not only encoded in one call site (#15840 順序 1)."""

    def test_the_cataloged_boundary_doc_exists_and_states_both_constraints(self):
        doc = (
            Path(__file__).resolve().parents[2]
            / "vibes/docs/logics/exception-diagnostic-sink-boundary.md"
        )
        self.assertTrue(doc.is_file(), doc)
        text = doc.read_text(encoding="utf-8")
        # Constraint A: the sink cannot live inside the guarded home.
        self.assertIn("shared-home guard", text)
        # Constraint B: raw is host-local only; never copied into a durable record.
        self.assertIn("never copy it into a durable Redmine record", text)
        # `finding_sinklocationundefined`: an exclusion is not a selection. A concrete sink
        # location, its permissions, its retention and the forbidden surfaces must be named.
        self.assertIn("XDG_STATE_HOME", text)
        self.assertIn("0700", text)
        self.assertIn("0600", text)
        self.assertIn("retention", text.lower())
        for forbidden_surface in ("repo / worktree", "stderr"):
            self.assertIn(forbidden_surface, text, forbidden_surface)
        # `finding_xdgforbiddenoverlap`: the location must be enforced, not merely listed.
        self.assertIn("resolve_diagnostic_sink_root", text)
        self.assertIn("diagnostic_sink_location.py", text)
        # `finding_relativeforbiddenroot` / `finding_staleadmissionrace`: the contract must say
        # that forbidden roots are absolute, that admission is advisory, and that a write goes
        # through the at-use fence.
        self.assertIn("forbidden_root_not_absolute", text)
        self.assertIn("advisory preflight", text)
        self.assertIn("open_sink_directory", text)

    def test_the_normative_field_table_never_re_admits_the_class_name(self):
        """`finding_doccontractdrift`: Decision 3 forbade the class name while Decision 4's
        field table still listed it as durable-allowed, and Decision 5 still called it "safe".

        That table is the contract a later sink slice follows, so the stale wording would have
        reintroduced the leak review j#109671 had already demonstrated. Pinned as a targeted
        regression so the normative text cannot drift back.
        """
        doc = (
            Path(__file__).resolve().parents[2]
            / "vibes/docs/logics/exception-diagnostic-sink-boundary.md"
        )
        text = doc.read_text(encoding="utf-8")
        table_rows = [
            line for line in text.splitlines()
            if line.startswith("|") and line.rstrip().endswith("| 可 |")
        ]
        self.assertTrue(table_rows, "precondition: the field table has durable-allowed rows")
        for row in table_rows:
            self.assertNotIn("__name__", row, row)
        self.assertIn("exception_kind", text)
        # The superseded phrasing survives only inside the correction note that quotes it.
        # Anything OUTSIDE a blockquote is normative text and must not carry it.
        normative = [
            line for line in text.splitlines() if not line.lstrip().startswith(">")
        ]
        for line in normative:
            self.assertNotIn("安全な class 名のみ", line, line)

    def test_the_doc_is_registered_in_the_catalog(self):
        catalog = (
            Path(__file__).resolve().parents[2] / ".mozyo-bridge/docs/catalog.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("logic-exception-diagnostic-sink-boundary", catalog)
        self.assertIn(
            "vibes/docs/logics/exception-diagnostic-sink-boundary.md", catalog
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
