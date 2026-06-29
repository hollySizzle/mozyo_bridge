"""Fake-port specification for the status session-read + command-handler
boundary (Redmine #12831 / #12830 / #12825 / #12785).

#12785 migrated the status present/missing agent-window logic off the
``commands.*`` monkeypatch seam: the existence / window-enumeration /
pane-capture reads, once only exercisable by patching
``mozyo_bridge.application.commands.session_exists`` /
``commands.list_session_windows`` / ``commands.run_tmux`` and scraping stdout
(see ``test_mozyo_bridge`` ``test_cmd_status_*``), are injected through a fake
:class:`StatusSessionPort` so :class:`ResolveSessionStatusUseCase` is unit
tested with no patch and no real tmux.

#12825 extends that to the command handler. :class:`StatusCommandHandler`
composes the session-read use case, the cockpit-membership projection (over a
fake :class:`StatusCockpitMembershipPort`), and the pure
:func:`render_status_report` and returns a typed :class:`StatusReport`, so the
rendering / cockpit-projection behavior the broad ``test_cmd_status_*``
integration tests assert on by scraping stdout is now driven by fakes and
asserted on a returned string. The integration tests stay as end-to-end
characterization; deeper migration of those sites remains residual to #12638.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.commands_status import (  # noqa: E402
    CockpitMembershipIdentity,
    CockpitMembershipProjection,
    LiveStatusCockpitMembershipReads,
    LiveStatusDoctorContinuation,
    ResolveSessionStatusUseCase,
    SessionStatusView,
    StatusCockpitMembershipPort,
    StatusCockpitMembershipReads,
    StatusCommandHandler,
    StatusCommandRequest,
    StatusContinuationResult,
    StatusDoctorContinuation,
    StatusQuery,
    StatusReport,
    match_cockpit_membership,
    render_status_report,
)
from mozyo_bridge.application.status_session_port import (  # noqa: E402
    LiveStatusSession,
    StatusSessionPort,
)


class _FakeStatusSession:
    """Fake :class:`StatusSessionPort`: the three reads are scripted.

    Counts ``capture_panes`` calls so the spec can pin the behavior-preserving
    invariant that ``list-panes`` runs only when agent windows are present.
    """

    def __init__(self, *, exists, windows=(), capture=(False, "")):
        self._exists = exists
        self._windows = list(windows)
        self._capture = capture
        self.capture_calls = 0

    def session_exists(self, session):
        return self._exists

    def list_windows(self, session):
        return list(self._windows)

    def capture_panes(self, session):
        self.capture_calls += 1
        return self._capture


class StatusSessionPortContractTest(unittest.TestCase):
    def test_fake_and_live_satisfy_port(self) -> None:
        self.assertIsInstance(_FakeStatusSession(exists=False), StatusSessionPort)
        self.assertIsInstance(LiveStatusSession(), StatusSessionPort)


class ResolveSessionStatusUseCaseTest(unittest.TestCase):
    def test_missing_session_reports_absent_without_window_read(self) -> None:
        fake = _FakeStatusSession(exists=False)
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertFalse(view.present)
        self.assertFalse(view.has_agent_windows)
        # A missing session never reaches pane capture.
        self.assertEqual(0, fake.capture_calls)

    def test_agent_windows_capture_panes_and_compute_missing(self) -> None:
        fake = _FakeStatusSession(
            exists=True,
            windows=["claude"],
            capture=(True, "0\tclaude\t%1\t1\tclaude\t/repo\n"),
        )
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertTrue(view.present)
        self.assertTrue(view.has_agent_windows)
        self.assertEqual(("claude",), view.agent_windows)
        self.assertTrue(view.panes_ok)
        self.assertEqual("0\tclaude\t%1\t1\tclaude\t/repo\n", view.panes_text)
        # codex has no window -> reported missing (sorted set of agent labels).
        self.assertEqual(("codex",), view.missing_agents)
        self.assertEqual(1, fake.capture_calls)

    def test_window_order_is_preserved_and_no_agent_missing(self) -> None:
        fake = _FakeStatusSession(
            exists=True,
            windows=["codex", "shell", "claude"],
            capture=(True, "rows"),
        )
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        # Non-agent windows are dropped; agent windows keep tmux window order.
        self.assertEqual(("codex", "claude"), view.agent_windows)
        self.assertEqual((), view.missing_agents)

    def test_present_session_without_agent_windows_skips_pane_capture(self) -> None:
        fake = _FakeStatusSession(exists=True, windows=["zsh"])
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertTrue(view.present)
        self.assertFalse(view.has_agent_windows)
        self.assertEqual((), view.agent_windows)
        self.assertEqual((), view.missing_agents)
        # Behavior-preserving: no list-panes read when there are no agent windows.
        self.assertEqual(0, fake.capture_calls)

    def test_failed_pane_capture_keeps_header_renderable(self) -> None:
        fake = _FakeStatusSession(
            exists=True, windows=["claude", "codex"], capture=(False, "")
        )
        view = ResolveSessionStatusUseCase(fake).resolve(StatusQuery(session="s"))
        self.assertTrue(view.has_agent_windows)
        self.assertFalse(view.panes_ok)
        self.assertEqual("", view.panes_text)
        self.assertEqual((), view.missing_agents)
        self.assertEqual(1, fake.capture_calls)


class _FakeMembership:
    """Minimal stand-in for ``WorkspaceMembership`` (only the rendered fields)."""

    def __init__(
        self,
        *,
        member,
        label="my_project",
        window="1",
        codex_pane="%2",
        claude_pane="%1",
        geometry_status="ok",
    ):
        self.member = member
        self.label = label
        self.window = window
        self.codex_pane = codex_pane
        self.claude_pane = claude_pane
        self.geometry_status = geometry_status


class _FakeMembershipPort:
    """Fake :class:`StatusCockpitMembershipPort`: a scripted projection result."""

    def __init__(self, membership):
        self._membership = membership

    def resolve(self):
        return self._membership


class RenderStatusReportTest(unittest.TestCase):
    """The pure renderer reproduces the procedural ``cmd_status`` stdout block."""

    def test_present_with_agent_table_and_missing_note(self) -> None:
        view = SessionStatusView(
            session="my_project",
            present=True,
            agent_windows=("claude",),
            has_agent_windows=True,
            panes_ok=True,
            panes_text="0\tclaude\t%1\t1\tclaude\t/repo\n",
            missing_agents=("codex",),
        )
        text = render_status_report(view, None)
        self.assertTrue(text.startswith("session: my_project\n"))
        self.assertIn("WINDOW\tNAME\tTARGET\tACTIVE\tPROCESS\tCWD\n", text)
        # panes_text is emitted raw (no doubled newline from the old end="" print).
        self.assertIn("0\tclaude\t%1\t1\tclaude\t/repo\n", text)
        self.assertNotIn("/repo\n\n  codex window missing", text)
        self.assertIn("  codex window missing; run `mozyo`", text)
        # No cockpit projection -> only the trailing blank line closes the block.
        self.assertNotIn("cockpit:", text)
        self.assertTrue(text.endswith("\n"))

    def test_present_without_agent_windows_emits_hint(self) -> None:
        view = SessionStatusView(session="agents", present=True, has_agent_windows=False)
        text = render_status_report(view, None)
        self.assertIn("no agent windows in this session", text)
        self.assertIn("mozyo-bridge init claude|codex", text)
        self.assertNotIn("WINDOW\tNAME", text)

    def test_missing_session(self) -> None:
        view = SessionStatusView(session="ghost", present=False)
        self.assertEqual("session: ghost (missing)\n\n", render_status_report(view, None))

    def test_cockpit_member_line(self) -> None:
        view = SessionStatusView(session="my_project", present=True, has_agent_windows=False)
        text = render_status_report(view, _FakeMembership(member=True))
        self.assertIn("cockpit: workspace 'my_project' IS loaded in cockpit", text)
        self.assertIn("codex=%2 claude=%1, geometry=ok", text)
        self.assertIn("display/liveness projection, not Redmine", text)

    def test_cockpit_non_member_line(self) -> None:
        view = SessionStatusView(session="my_project", present=True, has_agent_windows=False)
        text = render_status_report(view, _FakeMembership(member=False))
        self.assertIn("is NOT loaded in cockpit", text)
        self.assertIn("not cockpit membership", text)


class StatusCommandHandlerTest(unittest.TestCase):
    """The handler turns a typed request into a typed report over fakes."""

    def test_handle_composes_session_and_membership_into_report(self) -> None:
        sessions = _FakeStatusSession(
            exists=True,
            windows=["claude", "codex"],
            capture=(True, "0\tclaude\t%1\t1\tclaude\t/repo\n"),
        )
        handler = StatusCommandHandler(
            sessions=sessions,
            membership=_FakeMembershipPort(_FakeMembership(member=True)),
        )
        report = handler.handle(StatusCommandRequest(session="my_project"))
        self.assertIsInstance(report, StatusReport)
        self.assertIn("session: my_project\n", report.report_text)
        self.assertIn("0\tclaude\t%1\t1\tclaude\t/repo\n", report.report_text)
        self.assertIn("IS loaded in cockpit", report.report_text)
        # No agent is missing (both windows present) -> no missing note.
        self.assertNotIn("window missing", report.report_text)

    def test_handle_without_membership_port_omits_cockpit_lines(self) -> None:
        sessions = _FakeStatusSession(exists=True, windows=["zsh"])
        report = StatusCommandHandler(sessions=sessions).handle(
            StatusCommandRequest(session="agents")
        )
        self.assertIn("no agent windows in this session", report.report_text)
        self.assertNotIn("cockpit:", report.report_text)

    def test_handle_missing_session_with_absent_membership(self) -> None:
        sessions = _FakeStatusSession(exists=False)
        report = StatusCommandHandler(
            sessions=sessions,
            membership=_FakeMembershipPort(_FakeMembership(member=False)),
        ).handle(StatusCommandRequest(session="ghost"))
        self.assertIn("session: ghost (missing)\n", report.report_text)
        self.assertIn("is NOT loaded in cockpit", report.report_text)
        # A missing session never triggers a pane capture.
        self.assertEqual(0, sessions.capture_calls)


class StatusCockpitMembershipPortContractTest(unittest.TestCase):
    def test_fake_membership_port_satisfies_protocol(self) -> None:
        self.assertIsInstance(_FakeMembershipPort(None), StatusCockpitMembershipPort)


# --- #12830 cockpit-membership projection boundary -------------------------
#
# #12830 decomposed the procedural ``commands._status_repo_cockpit_membership``
# into a typed boundary in ``commands_status.py``: a value object
# (``CockpitMembershipIdentity``), a pure domain policy
# (``match_cockpit_membership``), a reads port
# (``StatusCockpitMembershipReads``), and the tolerant
# ``CockpitMembershipProjection`` use case. The specs below drive that boundary
# with a fake reads object and fake workspace records, so the projection's
# match / absent-fallback / tolerance behavior is asserted without patching the
# ``commands.*`` cockpit helpers (``resolve_canonical_session`` /
# ``_resolve_workspace_lane`` / ``_collect_cockpit_membership`` /
# ``_resolve_registry_facts``) the procedural body forced tests to monkeypatch.


class _FakeWorkspace:
    """Minimal stand-in for a loaded-cockpit ``WorkspaceMembership`` record.

    Only the fields the pure match policy keys on (``workspace_id`` /
    ``lane_id``) plus a ``member`` marker for assertions.
    """

    def __init__(self, *, workspace_id, lane_id, member=True):
        self.workspace_id = workspace_id
        self.lane_id = lane_id
        self.member = member


class _FakeMembershipReads:
    """Fake :class:`StatusCockpitMembershipReads`: scripted reads + call counts.

    Replaces the ``commands.*`` cockpit-helper monkeypatch the procedural
    projection required: ``resolve_identity`` / ``collect_workspaces`` /
    ``absent_membership`` are scripted, and each can be told to raise so the
    projection's tolerance is exercised. ``absent_calls`` pins that a match hit
    never builds the absent record (the original's "no registry read on a hit").
    """

    def __init__(
        self,
        *,
        identity,
        workspaces=(),
        absent=None,
        raise_on=None,
    ):
        self._identity = identity
        self._workspaces = list(workspaces)
        self._absent = absent
        self._raise_on = raise_on or set()
        self.absent_calls = 0
        self.absent_identity = None

    def _maybe_raise(self, name):
        if name in self._raise_on:
            raise RuntimeError(f"boom: {name}")

    def resolve_identity(self, repo):
        self._maybe_raise("resolve_identity")
        return self._identity

    def collect_workspaces(self):
        self._maybe_raise("collect_workspaces")
        return tuple(self._workspaces)

    def absent_membership(self, identity):
        self.absent_calls += 1
        self.absent_identity = identity
        self._maybe_raise("absent_membership")
        return self._absent


def _identity(*, workspace_id="wsA", target_lane="default"):
    return CockpitMembershipIdentity(
        repo_root="/repo/alpha",
        workspace_id=workspace_id,
        target_lane=target_lane,
        lane_label=None,
        fallback_label="alpha",
    )


class MatchCockpitMembershipPolicyTest(unittest.TestCase):
    """The pure match policy selects by workspace id + normalized lane."""

    def test_selects_workspace_by_id_and_normalized_lane(self) -> None:
        match = _FakeWorkspace(workspace_id="wsA", lane_id="default")
        other = _FakeWorkspace(workspace_id="wsB", lane_id="default", member=False)
        result = match_cockpit_membership([other, match], _identity(workspace_id="wsA"))
        self.assertIs(match, result)

    def test_empty_lane_id_normalizes_to_default_and_matches(self) -> None:
        # ``normalize_lane('')`` collapses to the ``default`` lane, so a record
        # carrying a blank lane matches a repo on the default lane.
        match = _FakeWorkspace(workspace_id="wsA", lane_id="")
        result = match_cockpit_membership([match], _identity(target_lane="default"))
        self.assertIs(match, result)

    def test_lane_mismatch_yields_no_match(self) -> None:
        rec = _FakeWorkspace(workspace_id="wsA", lane_id="feature-x")
        self.assertIsNone(
            match_cockpit_membership([rec], _identity(target_lane="default"))
        )

    def test_workspace_id_mismatch_yields_no_match(self) -> None:
        rec = _FakeWorkspace(workspace_id="wsZ", lane_id="default")
        self.assertIsNone(
            match_cockpit_membership([rec], _identity(workspace_id="wsA"))
        )

    def test_no_workspaces_yields_no_match(self) -> None:
        self.assertIsNone(match_cockpit_membership([], _identity()))


class CockpitMembershipProjectionTest(unittest.TestCase):
    """The tolerant use case composes the pure policy over a fake reads port."""

    def test_match_hit_returns_record_without_building_absent(self) -> None:
        hit = _FakeWorkspace(workspace_id="wsA", lane_id="default")
        reads = _FakeMembershipReads(
            identity=_identity(workspace_id="wsA"),
            workspaces=[hit],
            absent=_FakeMembership(member=False),
        )
        result = CockpitMembershipProjection(reads).resolve("/repo/alpha")
        self.assertIs(hit, result)
        # Behavior-preserving: a match never asks the port for the absent record.
        self.assertEqual(0, reads.absent_calls)

    def test_no_match_falls_back_to_absent_record(self) -> None:
        absent = _FakeMembership(member=False)
        identity = _identity(workspace_id="wsZ")
        reads = _FakeMembershipReads(
            identity=identity,
            workspaces=[_FakeWorkspace(workspace_id="wsA", lane_id="default")],
            absent=absent,
        )
        result = CockpitMembershipProjection(reads).resolve("/repo/zeta")
        self.assertIs(absent, result)
        self.assertEqual(1, reads.absent_calls)
        # The absent record is built from the resolved identity, not re-read.
        self.assertIs(identity, reads.absent_identity)

    def test_identity_read_failure_degrades_to_none(self) -> None:
        reads = _FakeMembershipReads(
            identity=_identity(), raise_on={"resolve_identity"}
        )
        self.assertIsNone(CockpitMembershipProjection(reads).resolve("/repo/alpha"))

    def test_collect_failure_degrades_to_none(self) -> None:
        reads = _FakeMembershipReads(
            identity=_identity(), raise_on={"collect_workspaces"}
        )
        self.assertIsNone(CockpitMembershipProjection(reads).resolve("/repo/alpha"))

    def test_absent_build_failure_degrades_to_none(self) -> None:
        reads = _FakeMembershipReads(
            identity=_identity(workspace_id="wsZ"),
            workspaces=[],
            raise_on={"absent_membership"},
        )
        self.assertIsNone(CockpitMembershipProjection(reads).resolve("/repo/zeta"))


class StatusCockpitMembershipReadsContractTest(unittest.TestCase):
    def test_fake_and_live_reads_satisfy_port(self) -> None:
        self.assertIsInstance(
            _FakeMembershipReads(identity=_identity()), StatusCockpitMembershipReads
        )
        self.assertIsInstance(
            LiveStatusCockpitMembershipReads(), StatusCockpitMembershipReads
        )


# --- #12831 status -> doctor tail continuation boundary --------------------
#
# #12831 isolated the procedural ``cmd_status`` -> ``cmd_doctor(args)`` tail
# delegation (the status command's exit-code continuation) behind a typed
# boundary in ``commands_status.py``: a value object
# (``StatusContinuationResult``), a port (``StatusDoctorContinuation``) with a
# live adapter routing to ``commands.cmd_doctor`` at call time, and a
# ``StatusCommandHandler.continue_with_doctor`` collaborator. The specs below
# drive that continuation with a fake, so the status command's deferral of its
# exit code to the doctor tail is asserted WITHOUT monkeypatching the
# ``commands.*`` doctor helpers (``commands.cmd_doctor`` / ``commands.run_doctor``
# / ``commands.format_doctor_text``) the procedural tail forced tests to patch.


class _FakeDoctorContinuation:
    """Fake :class:`StatusDoctorContinuation`: a scripted exit code + call count.

    Replaces the ``commands.*`` doctor-helper monkeypatch the procedural
    ``return cmd_doctor(args)`` tail required: ``run`` returns a scripted
    :class:`StatusContinuationResult` and counts its invocations, so a handler
    test can pin that rendering never runs the continuation and that
    ``continue_with_doctor`` defers to the port exactly once.
    """

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.run_calls = 0

    def run(self) -> StatusContinuationResult:
        self.run_calls += 1
        return StatusContinuationResult(exit_code=self._exit_code)


class StatusDoctorContinuationContractTest(unittest.TestCase):
    def test_fake_and_live_continuation_satisfy_port(self) -> None:
        self.assertIsInstance(_FakeDoctorContinuation(0), StatusDoctorContinuation)
        self.assertIsInstance(
            LiveStatusDoctorContinuation(object()), StatusDoctorContinuation
        )


class StatusContinuationTest(unittest.TestCase):
    """The handler defers its exit code to the doctor-tail continuation."""

    def test_continue_with_doctor_returns_port_result_without_patching_commands(
        self,
    ) -> None:
        sessions = _FakeStatusSession(exists=True, windows=["zsh"])
        continuation = _FakeDoctorContinuation(exit_code=1)
        handler = StatusCommandHandler(sessions=sessions, continuation=continuation)
        result = handler.continue_with_doctor()
        self.assertIsInstance(result, StatusContinuationResult)
        self.assertEqual(1, result.exit_code)
        self.assertEqual(1, continuation.run_calls)

    def test_handle_render_never_runs_the_continuation(self) -> None:
        # Rendering the status block is side-effect free: it must not run the
        # doctor tail (the adapter prints the report before the continuation).
        sessions = _FakeStatusSession(exists=True, windows=["zsh"])
        continuation = _FakeDoctorContinuation(exit_code=0)
        handler = StatusCommandHandler(sessions=sessions, continuation=continuation)
        handler.handle(StatusCommandRequest(session="agents"))
        self.assertEqual(0, continuation.run_calls)

    def test_continue_with_doctor_without_continuation_raises(self) -> None:
        # Render-only handler tests construct the handler with no continuation;
        # asking for the doctor tail then is a programming error, not a silent 0.
        handler = StatusCommandHandler(sessions=_FakeStatusSession(exists=False))
        with self.assertRaises(RuntimeError):
            handler.continue_with_doctor()


class LiveStatusDoctorContinuationTest(unittest.TestCase):
    """The live adapter routes to ``commands.cmd_doctor`` at call time."""

    def test_run_routes_to_commands_cmd_doctor_and_wraps_exit_code(self) -> None:
        # Call-time routing is what keeps the existing ``test_cmd_status_*``
        # integration tests (which patch ``commands.run_doctor`` /
        # ``commands.format_doctor_text``) driving the live doctor through this
        # continuation. Here we patch the seam one level up — ``commands.cmd_doctor``
        # — to confirm the adapter forwards the namespace and wraps the int.
        from unittest.mock import patch

        sentinel_args = object()
        with patch(
            "mozyo_bridge.application.commands.cmd_doctor", return_value=7
        ) as cmd_doctor:
            result = LiveStatusDoctorContinuation(sentinel_args).run()
        self.assertEqual(StatusContinuationResult(exit_code=7), result)
        cmd_doctor.assert_called_once_with(sentinel_args)


if __name__ == "__main__":
    unittest.main()
