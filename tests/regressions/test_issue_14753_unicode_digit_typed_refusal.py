"""Redmine #14753: a Unicode digit must not reach ``int()`` and break a typed refusal.

``str.isdigit()`` reads like "this is a number ``int()`` can parse". It is not, in two
independent ways, and every surface in this file was guarding a conversion with it:

- **the crash gap** — characters that are ``isdigit()`` and that ``int()`` REFUSES (``²`` and
  friends). The guard passes, the conversion raises, and a raw ``ValueError`` comes out of a
  function whose entire contract is to return a typed refusal;
- **the misread gap** — characters that are ``isdigit()`` and that ``int()`` accepts but that the
  source system never wrote (``１`` full-width, ``٣`` Arabic-Indic). No exception; the value is
  silently read as a number nobody named.

#14694 j#94249 / j#94257 and its approved review j#94271 fixed one instance of this on the
hibernate-evidence marker parser and named the sibling surfaces. This file pins the sibling
surfaces themselves, each at its own public entry point and each against its own refusal
vocabulary — the point is not that some exception is avoided but that the surface's *documented*
answer is the one that comes back:

- ``coordinator_proxy_fence.journal_ordinal`` promised ``None``; ``reserve`` raised instead of
  returning a ``ProxyReserveResult``;
- ``coordinator_proxy._generation_ordinal`` is a fail-closed classifier; ``_lane_scoped_status``
  raised instead of naming ``ANCHOR_DECISION_INCOMPLETE``;
- ``cockpit_layout._read_uint`` was already inside ``parse_window_layout``'s
  ``(ValueError, IndexError)`` net, so the crash gap was contained — but the MISREAD gap was not,
  and ``１０x2,0,0,1`` parsed into a real 10x2 tree tmux never emitted;
- ``hibernate_basis_producer.decision_journal`` raised out of ``as_payload``;
- ``herdr_transport._apply_sgr`` is ASCII-fenced by ``_SGR_RE``, so only the WIDTH half of the
  crash gap reaches it — CPython's int-from-str cap — and it raised out of a render classifier
  whose contract is ``ambiguous_render``;
- ``redmine_context._latest_issue_payload`` raised out of a cockpit fetch that otherwise degrades
  to ``STATE_UNAVAILABLE``;
- ``launch_command._parse_mozyo_window_rows`` raised instead of taking its own non-numeric branch;
- ``supervisor_launchd._is_loaded`` raised out of ``service_status`` despite promising it never
  raises;
- ``sublane_hibernate``'s supplied revision raised out of the hibernate preflight, pre-CAS;
- ``sublane_worker_refresh_live._anchor_bound`` answered "anchor bound" for a token the ordered
  durable comparison it justifies could not convert.

Every ASCII-decimal case is asserted alongside the refusal, because a guard that refuses
everything would pass a refusal-only test while breaking every real caller. The digit sets are
DERIVED from the interpreter (:func:`unconvertible_digits` / :func:`non_ascii_convertible_digits`)
rather than written down: a literal list goes stale against a new Unicode version, and
list-shaped reasoning — "isdigit means int can read it" — is what produced the defect.
"""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import tempfile
import unittest

from mozyo_bridge.application.launch_command import _parse_mozyo_window_rows
from mozyo_bridge.core.state.coordinator_proxy_fence import (
    CoordinatorProxyFence,
    ProxyRouteKey,
    RESERVE_STALE,
    journal_ordinal,
)
from mozyo_bridge.core.state.lane_lifecycle_model import is_redmine_id
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    supervisor_launchd,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate import (  # noqa: E501
    _revision_ordinal,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh import (  # noqa: E501
    WorkerRefreshRequest,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_worker_refresh_live import (  # noqa: E501
    LiveWorkerRefreshOps,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (  # noqa: E501
    coordinator_proxy,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.hibernate_basis_producer import (  # noqa: E501
    ProducedBasis,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (  # noqa: E501
    parse_window_layout,
)
from mozyo_bridge.e_140_adapter_provider.f_110_ticket_adapter_common.domain.ticket_adapter import (  # noqa: E501
    IssueRef,
)
from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure import (
    redmine_context,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure import (  # noqa: E501
    herdr_transport,
)


def _digit_gap(convertible, sample=6):
    """Characters that are ``str.isdigit()`` and whose ``int()`` outcome is ``convertible``.

    Asked of the interpreter, never listed. ``convertible=False`` yields the crash gap (``²``);
    ``convertible=True`` restricted to non-ASCII yields the misread gap (``１`` / ``٣``). Sampled
    across the whole range so the sweep spans several digit families and still runs fast.
    """
    found = []
    for code_point in range(0x110000):
        character = chr(code_point)
        if not character.isdigit() or character.isascii():
            continue
        try:
            int(character)
        except ValueError:
            converts = False
        else:
            converts = True
        if converts is convertible:
            found.append(character)
    if not found:
        return ()
    return tuple(found[:: max(1, len(found) // sample)])[:sample]


#: ``isdigit()`` and ``int()`` REFUSES it — the token that used to raise out of a typed refusal.
UNCONVERTIBLE_DIGITS = _digit_gap(convertible=False)

#: ``isdigit()`` and ``int()`` accepts it, but no source system wrote it — the silent misread.
NON_ASCII_CONVERTIBLE_DIGITS = _digit_gap(convertible=True)

#: A digit run wider than CPython's int-from-str cap: the crash gap reached through width rather
#: than through the alphabet. ASCII, so it survives an alphabet-only fix.
WIDE_ASCII_RUN = "9" * 5000


class DigitGapDerivationTest(unittest.TestCase):
    """The sweeps below are only meaningful if the interpreter actually admits both gaps."""

    def test_both_gaps_are_non_empty(self):
        self.assertTrue(UNCONVERTIBLE_DIGITS, "no isdigit()-but-unconvertible character")
        self.assertTrue(NON_ASCII_CONVERTIBLE_DIGITS, "no non-ASCII convertible digit")

    def test_the_gaps_are_what_they_claim(self):
        for character in UNCONVERTIBLE_DIGITS:
            self.assertTrue(character.isdigit(), repr(character))
            with self.assertRaises(ValueError):
                int(character)
        for character in NON_ASCII_CONVERTIBLE_DIGITS:
            self.assertTrue(character.isdigit(), repr(character))
            self.assertFalse(character.isascii(), repr(character))
            int(character)  # must not raise: that is what makes it a MISREAD, not a crash

    def test_the_wide_run_is_the_width_half_not_the_alphabet_half(self):
        self.assertTrue(WIDE_ASCII_RUN.isascii() and WIDE_ASCII_RUN.isdigit())
        with self.assertRaises(ValueError):
            int(WIDE_ASCII_RUN)


class CoordinatorProxyFenceJournalOrdinalTest(unittest.TestCase):
    """``journal_ordinal`` promises ``None``; ``reserve`` promises a ``ProxyReserveResult``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fence = CoordinatorProxyFence(pathlib.Path(tmp.name) / "proxy.sqlite")
        self.fence.bootstrap()
        self.route = ProxyRouteKey(
            workspace_id="w", lane_id="l", role="coordinator", action="a"
        )

    def test_digit_gap_journals_are_none_not_a_valueerror(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertIsNone(journal_ordinal(token), repr(token))

    def test_reserve_refuses_with_its_own_verdict(self):
        for token in (*UNCONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            result = self.fence.reserve(self.route, issue="1", journal=token)
            self.assertFalse(result.won, repr(token))
            self.assertEqual(result.verdict, RESERVE_STALE, repr(token))

    def test_ascii_journals_still_order_numerically(self):
        self.assertEqual(journal_ordinal("94322"), 94322)
        self.assertEqual(journal_ordinal(" 94329 "), 94329)
        self.assertLess(journal_ordinal("9"), journal_ordinal("10"))
        self.assertTrue(self.fence.reserve(self.route, issue="1", journal="94322").won)


class RedmineIdShapeTest(unittest.TestCase):
    """``core/state`` holds ONE answer for what a Redmine record id may be built from."""

    def test_digit_gap_tokens_are_not_redmine_ids(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertFalse(is_redmine_id(token), repr(token))

    def test_real_ids_are(self):
        for token in ("1", "14753", "94322", "9" * 18):
            self.assertTrue(is_redmine_id(token), repr(token))

    def test_nothing_that_names_no_record_is(self):
        for token in ("", "0", "00", "-1", "1.0", "9" * 19, " 1", "1 "):
            self.assertFalse(is_redmine_id(token), repr(token))


class CoordinatorProxyGenerationOrdinalTest(unittest.TestCase):
    """A decision whose generation cannot be read authorizes nothing — it does not raise."""

    def _record(self, generation):
        return coordinator_proxy.DecisionRecord(
            journal="94322", token="t", lane="lane", lane_generation=generation
        )

    def test_digit_gap_generations_classify_as_incomplete(self):
        expected = coordinator_proxy.LaneExpectation(
            lane="lane", generation=1, decision_journal="94322"
        )
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertEqual(
                coordinator_proxy._lane_scoped_status(self._record(token), expected),
                coordinator_proxy.ANCHOR_DECISION_INCOMPLETE,
                repr(token),
            )

    def test_an_ascii_generation_still_verifies_and_still_detects_drift(self):
        expected = coordinator_proxy.LaneExpectation(
            lane="lane", generation=7, decision_journal="94322"
        )
        self.assertEqual(
            coordinator_proxy._lane_scoped_status(self._record("7"), expected),
            coordinator_proxy.ANCHOR_VERIFIED,
        )
        self.assertEqual(
            coordinator_proxy._lane_scoped_status(self._record("6"), expected),
            coordinator_proxy.ANCHOR_GENERATION_STALE,
        )


class CockpitLayoutReadUintTest(unittest.TestCase):
    """tmux writes ``%u``. A layout this parser accepts must be one tmux could have written."""

    def test_the_misread_gap_no_longer_parses_into_a_real_tree(self):
        for character in NON_ASCII_CONVERTIBLE_DIGITS:
            self.assertIsNone(
                parse_window_layout(f"{character}x2,0,0,1"), repr(character)
            )

    def test_the_crash_gap_stays_contained_as_unparseable(self):
        for token in (*UNCONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertIsNone(parse_window_layout(f"{token}x2,0,0,1"), repr(token))

    def test_a_real_tmux_layout_still_parses(self):
        root = parse_window_layout("a3cd,180x50,0,0{90x50,0,0,1,89x50,91,0,2}")
        self.assertIsNotNone(root)
        self.assertEqual((root.width, root.height), (180, 50))
        self.assertEqual([leaf.pane_id for leaf in root.leaves()], ["%1", "%2"])

    def test_a_leaf_only_layout_still_parses(self):
        root = parse_window_layout("180x50,0,0,3")
        self.assertIsNotNone(root)
        self.assertEqual(root.pane_id, "%3")


class HibernateBasisDecisionJournalTest(unittest.TestCase):
    """A value that is not a journal id is simply not a journal — it is not an exception."""

    def _basis(self, journals):
        return ProducedBasis(
            basis="b", conjuncts=(), gaps=(), evidence_journals=journals
        )

    def test_digit_gap_evidence_journals_drop_out(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            basis = self._basis({"k": token})
            self.assertEqual(basis.decision_journal, "", repr(token))
            self.assertEqual(basis.as_payload()["decision_journal"], "", repr(token))

    def test_ascii_journals_still_take_the_newest(self):
        basis = self._basis({"a": "94322", "b": "94329", "c": "94271"})
        self.assertEqual(basis.decision_journal, "94329")

    def test_one_unreadable_journal_does_not_hide_the_readable_ones(self):
        basis = self._basis({"a": "94322", "bad": UNCONVERTIBLE_DIGITS[0]})
        self.assertEqual(basis.decision_journal, "94322")


class HerdrSgrIntensityTest(unittest.TestCase):
    """``_SGR_RE`` fences the alphabet, so only the WIDTH half of the gap reaches this fold."""

    def test_an_over_wide_sgr_parameter_no_longer_raises(self):
        faint, dim_fg = herdr_transport._apply_sgr(WIDE_ASCII_RUN, False, True)
        self.assertFalse(faint)
        self.assertTrue(dim_fg, "an unrecognised code must not clear the dim axis")

    def test_an_over_wide_sgr_reaches_the_render_classifier_without_raising(self):
        observation = herdr_transport._parse_render_payload(
            '{"text":"\\u001b[' + WIDE_ASCII_RUN + 'mX"}'
        )
        self.assertIsNotNone(observation)

    def test_the_standard_foregrounds_still_clear_the_dim_axis(self):
        for code in [str(n) for n in range(30, 38)] + ["030", "0030", "39"]:
            self.assertEqual(
                herdr_transport._apply_sgr(code, False, True), (False, False), code
            )

    def test_the_dim_codes_still_set_their_axes(self):
        self.assertEqual(herdr_transport._apply_sgr("2", False, False), (True, False))
        self.assertEqual(herdr_transport._apply_sgr("90", False, False), (False, True))
        self.assertEqual(herdr_transport._apply_sgr("0", True, True), (False, False))
        self.assertEqual(herdr_transport._apply_sgr("22", True, True), (False, True))


class RedmineContextIssueIdTest(unittest.TestCase):
    """The cockpit payload's ``latest_issue.id`` contract: a number, a string, or ``None``."""

    def _payload_id(self, issue_id):
        return redmine_context._latest_issue_payload(
            IssueRef(provider="redmine", id=issue_id)
        )["id"]

    def test_digit_gap_ids_take_the_existing_non_numeric_branch(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertEqual(self._payload_id(token), token, repr(token))

    def test_a_real_id_is_still_emitted_as_a_number(self):
        self.assertEqual(self._payload_id("14753"), 14753)
        self.assertIsInstance(self._payload_id("14753"), int)

    def test_an_absent_id_is_still_none(self):
        self.assertIsNone(self._payload_id(""))


class LaunchCommandWindowIndexTest(unittest.TestCase):
    """``index`` is an int for a tmux window index and the raw string for anything else."""

    def test_digit_gap_indices_stay_strings(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            rows = _parse_mozyo_window_rows(f"{token}\tclaude\tclaude\n")
            self.assertEqual(rows[0]["index"], token, repr(token))

    def test_real_tmux_indices_are_still_ints(self):
        rows = _parse_mozyo_window_rows("0\tclaude\tclaude\n1\tcodex\tnode\n")
        self.assertEqual([row["index"] for row in rows], [0, 1])

    def test_the_pre_existing_non_numeric_branch_is_unchanged(self):
        rows = _parse_mozyo_window_rows("x\tclaude\t\n")
        self.assertEqual(rows[0]["index"], "x")
        self.assertIsNone(rows[0]["process"])


class SupervisorLaunchdPidTest(unittest.TestCase):
    """``_is_loaded`` promises it never raises; ``service_status`` returns a typed dict."""

    @staticmethod
    def _runner(stdout):
        return lambda argv: subprocess.CompletedProcess(argv, 0, stdout, "")

    def test_digit_gap_pids_read_as_none(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            loaded, pid = supervisor_launchd._is_loaded(self._runner(f"\tpid = {token}\n"))
            self.assertTrue(loaded, repr(token))
            self.assertIsNone(pid, repr(token))

    def test_service_status_stays_a_dict_not_a_traceback(self):
        for token in (*UNCONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            status = supervisor_launchd.service_status(
                runner=self._runner(f"\tpid = {token}\n")
            )
            self.assertIsInstance(status, dict, repr(token))
            self.assertIsNone(status.get("pid"), repr(token))

    def test_a_real_pid_is_still_read(self):
        loaded, pid = supervisor_launchd._is_loaded(self._runner("\tpid = 4321\n"))
        self.assertTrue(loaded)
        self.assertEqual(pid, 4321)


class SublaneHibernateRevisionTest(unittest.TestCase):
    """A supplied revision that names no row must fail closed pre-CAS, not raise."""

    def test_digit_gap_revisions_are_none(self):
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertIsNone(_revision_ordinal(token), repr(token))

    def test_a_none_revision_never_equals_a_real_one(self):
        # This is the property the preflight relies on: an unreadable supplied revision can
        # never satisfy `expected_revision == rec.revision`, so the approval fails closed.
        for token in (*UNCONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            self.assertNotEqual(_revision_ordinal(token), 0)
            self.assertNotEqual(_revision_ordinal(token), 7)

    def test_ascii_revisions_including_the_stores_zero_still_read(self):
        self.assertEqual(_revision_ordinal("0"), 0)
        self.assertEqual(_revision_ordinal("7"), 7)
        self.assertEqual(_revision_ordinal(str(2**63 - 1)), 2**63 - 1)

    def test_a_non_decimal_revision_is_none(self):
        for token in ("", "1.0", "-1", "1_0", "7a"):
            self.assertIsNone(_revision_ordinal(token), repr(token))


class WorkerRefreshAnchorBoundTest(unittest.TestCase):
    """"Anchor bound" must mean the ordered durable comparison it justifies can actually run."""

    def _ops(self, anchor_journal):
        request = WorkerRefreshRequest(
            issue="14753",
            lane="lane",
            role="worker",
            provider="claude",
            assigned_name="mzb1",
            locator="w1:p1",
            anchor_issue="14753",
            resume_anchor_journal=anchor_journal,
            resume_gate="implementation_request",
        )
        return LiveWorkerRefreshOps(repo_root=pathlib.Path("."), request=request), request

    def test_digit_gap_anchors_are_not_bound(self):
        # Pre-fix these answered True while the reader that orders against the anchor caught a
        # ValueError and reported *unobservable* — the classification and the comparison
        # disagreeing about the same token.
        for token in (*UNCONVERTIBLE_DIGITS, *NON_ASCII_CONVERTIBLE_DIGITS, WIDE_ASCII_RUN):
            ops, request = self._ops(token)
            self.assertFalse(ops._anchor_bound(request), repr(token))

    def test_a_real_anchor_journal_is_still_bound(self):
        ops, request = self._ops("94322")
        self.assertTrue(ops._anchor_bound(request))

    def test_a_non_resumable_gate_is_still_unbound(self):
        ops, request = self._ops("94322")
        request = dataclasses.replace(request, resume_gate="not_a_gate")
        self.assertFalse(ops._anchor_bound(request))


if __name__ == "__main__":
    unittest.main()
