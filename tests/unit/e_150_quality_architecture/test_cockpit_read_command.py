"""Fake-port / pure specifications for the cockpit live-read boundary (#12971).

These exercise the ``cockpit_read_command`` use case and pure projection
directly with a synthetic :class:`CockpitReadOps` — no real tmux server. They
pin:

- the pure projection (``project_columns`` / ``project_geometry`` /
  ``project_managed_window_rows`` line parses, the ``_as_int`` tolerance, and the
  ``columns_target`` / ``geometry_target`` addressing),
- the ``CockpitReadUseCase`` walk: the tolerant ``None`` / ``[]`` degradation on a
  raised read or a non-zero returncode, and the managed-window discovery that
  composes over the ``read_columns`` seam and filters to windows carrying a
  managed pane,
- the #13106 session-helper reads: the pure ``rightmost_codex_anchor`` pick /
  ``project_nonempty_lines`` parse / cleanup-note wording, and the
  ``session_present`` / ``attached_clients_result`` /
  ``source_session_cleanup_note`` use-case walks with their known/unknown and
  gone/alive distinctions.

The end-to-end behavior over the live ``commands.run_tmux`` /
``commands._read_cockpit_columns`` seams stays pinned by the cockpit
append / geometry / group-window / membership characterization tests; this file
pins the boundary in isolation.
"""

from __future__ import annotations

import argparse
import unittest

from mozyo_bridge.application.cockpit_read_command import (
    COLUMNS_FIELDS,
    CockpitReadUseCase,
    GEOMETRY_FIELDS,
    WINDOWS_FIELDS,
    _as_int,
    columns_target,
    geometry_target,
    project_columns,
    project_geometry,
    project_managed_window_rows,
    project_nonempty_lines,
    rightmost_codex_anchor,
    source_session_cleanup_note_text,
)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> argparse.Namespace:
    return argparse.Namespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeCockpitReadOps:
    """A synthetic :class:`CockpitReadOps` recording ``run_tmux`` calls.

    ``run_result`` feeds every ``run_tmux`` call (or ``run_raises`` makes it
    raise); ``columns_by_window`` feeds the ``read_columns`` seam per window id.
    """

    def __init__(
        self,
        *,
        run_result: argparse.Namespace | None = None,
        run_raises: BaseException | None = None,
        columns_by_window: dict | None = None,
        session_present: bool = True,
        session_raises: BaseException | None = None,
    ) -> None:
        self._run_result = run_result if run_result is not None else _result()
        self._run_raises = run_raises
        self._columns_by_window = columns_by_window or {}
        self._session_present = session_present
        self._session_raises = session_raises
        self.run_calls: list[tuple] = []
        self.read_columns_calls: list[tuple] = []
        self.session_exists_calls: list[str] = []

    def run_tmux(self, *args, **kwargs):
        self.run_calls.append((args, kwargs))
        if self._run_raises is not None:
            raise self._run_raises
        return self._run_result

    def read_columns(self, session, window):
        self.read_columns_calls.append((session, window))
        return self._columns_by_window.get(window)

    def session_exists(self, session):
        self.session_exists_calls.append(session)
        if self._session_raises is not None:
            raise self._session_raises
        return self._session_present


class PureProjectionTest(unittest.TestCase):
    def test_as_int_is_tolerant(self) -> None:
        self.assertEqual(41, _as_int("41"))
        self.assertEqual(0, _as_int(""))
        self.assertEqual(0, _as_int("x"))
        self.assertEqual(0, _as_int(None))  # type: ignore[arg-type]

    def test_columns_target_addressing(self) -> None:
        # A window id (`@N`) targets on its own; a name needs the session prefix;
        # None defaults to the shared cockpit window.
        self.assertEqual("@7", columns_target("mozyo-cockpit", "@7"))
        self.assertEqual("mozyo-cockpit:grp", columns_target("mozyo-cockpit", "grp"))
        self.assertEqual("mozyo-cockpit:cockpit", columns_target("mozyo-cockpit", None))

    def test_geometry_target_is_cockpit_window(self) -> None:
        self.assertEqual("s:cockpit", geometry_target("s"))

    def test_project_columns_mixed_feed(self) -> None:
        out = (
            "%1\twsA\tcodex\twkt-1\t41\t39\t\t\t\n"
            "%2\twsA\tclaude\twkt-1\t41\t13\t\t\t\n"
            "%3\twsB\tcodex\n"
            "%4\twsA\tcodex\twkt-1\t82\t39\tgiken-cloud-drive-management"
            "\tprojects/giken-cloud-drive-management\tクラウドドライブ管理\n"
        )
        cols = project_columns(out)
        self.assertEqual(4, len(cols))
        self.assertEqual(
            {
                "pane_id": "%1", "workspace_id": "wsA", "role": "codex",
                "lane_id": "wkt-1", "pane_left": 41, "pane_width": 39,
                "project_scope": "", "project_path": "", "project_label": "",
            },
            cols[0],
        )
        # Legacy 3-field pane -> lane_id "" + geometry 0 + empty project triple.
        self.assertEqual("", cols[2]["lane_id"])
        self.assertEqual(0, cols[2]["pane_left"])
        self.assertEqual("", cols[2]["project_scope"])
        # Project-scoped gateway pane -> the #12658 project triple parses.
        self.assertEqual("giken-cloud-drive-management", cols[3]["project_scope"])
        self.assertEqual("クラウドドライブ管理", cols[3]["project_label"])

    def test_project_columns_skips_blank_pane_id(self) -> None:
        # A line without a pane id (< 3 fields or empty first) is dropped.
        self.assertEqual([], project_columns("\t\t\n\n"))
        self.assertEqual([], project_columns("only\ttwo\n"))

    def test_project_managed_window_rows(self) -> None:
        out = "@1\tcockpit\t\n@5\tgroup-a\tgid-a\n\tno-id\tx\n"
        rows = project_managed_window_rows(out)
        self.assertEqual(
            [
                {"window_id": "@1", "window": "cockpit", "group_id": ""},
                {"window_id": "@5", "window": "group-a", "group_id": "gid-a"},
            ],
            rows,
        )

    def test_project_geometry_pads_short_rows(self) -> None:
        out = "%1\twsA\tcodex\twkt-1\t0\t0\t66\t34\n%1106\n"
        panes = project_geometry(out)
        self.assertEqual(2, len(panes))
        self.assertEqual(
            {
                "pane_id": "%1", "workspace_id": "wsA", "role": "codex",
                "lane_id": "wkt-1", "pane_left": 0, "pane_top": 0,
                "pane_width": 66, "pane_height": 34,
            },
            panes[0],
        )
        # A role-less half-bound pane (the #12130 case) still reports, padded.
        self.assertEqual("%1106", panes[1]["pane_id"])
        self.assertEqual("", panes[1]["role"])
        self.assertEqual(0, panes[1]["pane_height"])


class ReadColumnsUseCaseTest(unittest.TestCase):
    def test_reads_and_projects_with_the_columns_template(self) -> None:
        ops = _FakeCockpitReadOps(
            run_result=_result(stdout="%1\twsA\tcodex\n"),
        )
        cols = CockpitReadUseCase(ops).read_columns("mozyo-cockpit", "@3")
        self.assertEqual([{
            "pane_id": "%1", "workspace_id": "wsA", "role": "codex",
            "lane_id": "", "pane_left": 0, "pane_width": 0,
            "project_scope": "", "project_path": "", "project_label": "",
        }], cols)
        (args, kwargs) = ops.run_calls[0]
        self.assertEqual(("list-panes", "-t", "@3", "-F", COLUMNS_FIELDS), args)
        self.assertEqual({"check": False}, kwargs)

    def test_non_zero_returncode_degrades_to_none(self) -> None:
        ops = _FakeCockpitReadOps(run_result=_result(returncode=1))
        self.assertIsNone(CockpitReadUseCase(ops).read_columns("s"))

    def test_raised_read_degrades_to_none(self) -> None:
        ops = _FakeCockpitReadOps(run_raises=OSError("no tmux"))
        self.assertIsNone(CockpitReadUseCase(ops).read_columns("s"))
        # SystemExit is caught too (a require_tmux-style abort stays non-raising).
        ops = _FakeCockpitReadOps(run_raises=SystemExit(2))
        self.assertIsNone(CockpitReadUseCase(ops).read_columns("s"))


class ReadGeometryUseCaseTest(unittest.TestCase):
    def test_reads_geometry_with_its_template_and_target(self) -> None:
        ops = _FakeCockpitReadOps(run_result=_result(stdout="%1\twsA\tcodex\t\t0\t0\t66\t34\n"))
        panes = CockpitReadUseCase(ops).read_geometry("s")
        self.assertEqual(66, panes[0]["pane_width"])
        (args, _kwargs) = ops.run_calls[0]
        self.assertEqual(("list-panes", "-t", "s:cockpit", "-F", GEOMETRY_FIELDS), args)

    def test_geometry_degrades_to_none(self) -> None:
        self.assertIsNone(CockpitReadUseCase(_FakeCockpitReadOps(run_result=_result(returncode=1))).read_geometry("s"))
        self.assertIsNone(CockpitReadUseCase(_FakeCockpitReadOps(run_raises=OSError())).read_geometry("s"))


class ReadManagedWindowsUseCaseTest(unittest.TestCase):
    def test_composes_over_read_columns_and_filters_unmanaged(self) -> None:
        ops = _FakeCockpitReadOps(
            run_result=_result(stdout="@1\tcockpit\t\n@5\tgroup-a\tgid-a\n@9\tempty\t\n"),
            columns_by_window={
                "@1": [{"pane_id": "%1", "workspace_id": "wsA"}],
                "@5": [{"pane_id": "%2", "workspace_id": "wsB"}],
                # @9 read returns no columns -> window dropped.
                "@9": [],
            },
        )
        managed = CockpitReadUseCase(ops).read_managed_windows("mozyo-cockpit")
        self.assertEqual(["@1", "@5"], [w["window_id"] for w in managed])
        self.assertEqual("gid-a", managed[1]["group_id"])
        self.assertEqual([{"pane_id": "%1", "workspace_id": "wsA"}], managed[0]["columns"])
        # The list-windows read used the windows template; per-window reads went
        # through the read_columns seam by window id.
        (args, _kwargs) = ops.run_calls[0]
        self.assertEqual(("list-windows", "-t", "mozyo-cockpit", "-F", WINDOWS_FIELDS), args)
        self.assertEqual(
            [("mozyo-cockpit", "@1"), ("mozyo-cockpit", "@5"), ("mozyo-cockpit", "@9")],
            ops.read_columns_calls,
        )

    def test_window_without_managed_pane_is_dropped(self) -> None:
        # A window whose panes carry no @mozyo_workspace_id is omitted.
        ops = _FakeCockpitReadOps(
            run_result=_result(stdout="@1\tcockpit\t\n"),
            columns_by_window={"@1": [{"pane_id": "%1", "workspace_id": ""}]},
        )
        self.assertEqual([], CockpitReadUseCase(ops).read_managed_windows("s"))

    def test_managed_windows_degrades_to_empty_list(self) -> None:
        self.assertEqual([], CockpitReadUseCase(_FakeCockpitReadOps(run_result=_result(returncode=1))).read_managed_windows("s"))
        self.assertEqual([], CockpitReadUseCase(_FakeCockpitReadOps(run_raises=OSError())).read_managed_windows("s"))


class SessionHelperPureTest(unittest.TestCase):
    """The #13106 session-helper pure projections / wording."""

    def test_rightmost_codex_anchor_picks_by_geometry(self) -> None:
        codex = [
            {"pane_id": "%mid", "pane_left": 40, "pane_width": 40},
            {"pane_id": "%rightmost", "pane_left": 80, "pane_width": 40},
            {"pane_id": "%left", "pane_left": 0, "pane_width": 40},
        ]
        self.assertEqual("%rightmost", rightmost_codex_anchor(codex))

    def test_rightmost_tie_breaks_on_right_edge_then_pane_id(self) -> None:
        # Same left: the wider pane (larger right edge) wins.
        codex = [
            {"pane_id": "%a", "pane_left": 40, "pane_width": 20},
            {"pane_id": "%b", "pane_left": 40, "pane_width": 40},
        ]
        self.assertEqual("%b", rightmost_codex_anchor(codex))
        # Missing geometry defaults to 0: falls back to a stable pane-id order.
        codex = [{"pane_id": "%1"}, {"pane_id": "%3"}, {"pane_id": "%2"}]
        self.assertEqual("%3", rightmost_codex_anchor(codex))

    def test_rightmost_empty_is_none(self) -> None:
        self.assertIsNone(rightmost_codex_anchor([]))

    def test_project_nonempty_lines_strips_and_drops_blanks(self) -> None:
        self.assertEqual(
            ("/dev/ttys003", "/dev/ttys004"),
            project_nonempty_lines("/dev/ttys003\n\n  /dev/ttys004  \n"),
        )
        self.assertEqual((), project_nonempty_lines(""))
        self.assertEqual((), project_nonempty_lines(None))  # type: ignore[arg-type]

    def test_cleanup_note_wording(self) -> None:
        gone = source_session_cleanup_note_text("src", False, "?")
        self.assertEqual(
            "source session 'src' is now empty and was closed by tmux "
            "(both agent panes moved out); not killed explicitly.",
            gone,
        )
        alive = source_session_cleanup_note_text("src", True, "2")
        self.assertEqual(
            "source session 'src' still has 2 pane(s) and was left intact "
            "(not killed).",
            alive,
        )


class SessionPresentUseCaseTest(unittest.TestCase):
    def test_present_probes_the_session(self) -> None:
        ops = _FakeCockpitReadOps(session_present=True)
        self.assertTrue(CockpitReadUseCase(ops).session_present("s"))
        self.assertEqual(["s"], ops.session_exists_calls)
        self.assertFalse(
            CockpitReadUseCase(_FakeCockpitReadOps(session_present=False)).session_present("s")
        )

    def test_any_error_degrades_to_false(self) -> None:
        self.assertFalse(
            CockpitReadUseCase(_FakeCockpitReadOps(session_raises=OSError())).session_present("s")
        )
        self.assertFalse(
            CockpitReadUseCase(_FakeCockpitReadOps(session_raises=SystemExit(2))).session_present("s")
        )


class AttachedClientsResultUseCaseTest(unittest.TestCase):
    def test_empty_session_is_no_clients_known_without_a_read(self) -> None:
        ops = _FakeCockpitReadOps()
        self.assertEqual(((), True), CockpitReadUseCase(ops).attached_clients_result(""))
        self.assertEqual([], ops.run_calls)

    def test_success_reads_list_clients_and_projects(self) -> None:
        ops = _FakeCockpitReadOps(run_result=_result(stdout="/dev/ttys003\n/dev/ttys004\n"))
        clients, known = CockpitReadUseCase(ops).attached_clients_result("mozyo-cockpit")
        self.assertEqual(("/dev/ttys003", "/dev/ttys004"), clients)
        self.assertTrue(known)
        (args, kwargs) = ops.run_calls[0]
        self.assertEqual(
            ("list-clients", "-t", "mozyo-cockpit", "-F", "#{client_tty}"), args
        )
        self.assertEqual({"check": False}, kwargs)

    def test_success_empty_is_known(self) -> None:
        ops = _FakeCockpitReadOps(run_result=_result(stdout=""))
        self.assertEqual(((), True), CockpitReadUseCase(ops).attached_clients_result("s"))

    def test_failed_read_is_unknown(self) -> None:
        # Non-zero exit and a raised read both fail closed to "unknown".
        self.assertEqual(
            ((), False),
            CockpitReadUseCase(_FakeCockpitReadOps(run_result=_result(returncode=1))).attached_clients_result("s"),
        )
        self.assertEqual(
            ((), False),
            CockpitReadUseCase(_FakeCockpitReadOps(run_raises=RuntimeError("no server"))).attached_clients_result("s"),
        )


class SourceSessionCleanupNoteUseCaseTest(unittest.TestCase):
    def test_absent_session_reports_closed_by_tmux(self) -> None:
        ops = _FakeCockpitReadOps(session_present=False)
        note = CockpitReadUseCase(ops).source_session_cleanup_note("src")
        self.assertIn("closed by tmux", note)
        self.assertIn("not killed explicitly", note)
        # No pane-count read on an absent session.
        self.assertEqual([], ops.run_calls)

    def test_alive_session_reports_remaining_pane_count(self) -> None:
        ops = _FakeCockpitReadOps(run_result=_result(stdout="%1\n%2\n"))
        note = CockpitReadUseCase(ops).source_session_cleanup_note("src")
        self.assertIn("still has 2 pane(s)", note)
        self.assertIn("left intact", note)
        (args, kwargs) = ops.run_calls[0]
        self.assertEqual(("list-panes", "-s", "-t", "src", "-F", "#{pane_id}"), args)
        self.assertEqual({"check": False}, kwargs)

    def test_unreadable_pane_count_degrades_to_question_mark(self) -> None:
        for ops in (
            _FakeCockpitReadOps(run_result=_result(returncode=1)),
            _FakeCockpitReadOps(run_raises=RuntimeError("boom")),
        ):
            note = CockpitReadUseCase(ops).source_session_cleanup_note("src")
            self.assertIn("still has ? pane(s)", note)

    def test_unprobeable_session_reads_as_gone(self) -> None:
        # session_exists raising degrades to "absent" — same as the original body.
        ops = _FakeCockpitReadOps(session_raises=OSError())
        self.assertIn(
            "closed by tmux", CockpitReadUseCase(ops).source_session_cleanup_note("src")
        )


if __name__ == "__main__":
    unittest.main()
