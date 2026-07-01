"""Fake-port / pure specifications for the status/session helper boundary (#12974).

These exercise the ``status_session_helper`` use cases and pure policy directly
with a synthetic reads port — no real tmux server, no ``commands.*``
monkeypatch. They pin:

- the pure helpers (``cwd_is_under_repo``, the session-cwd-mismatch selection,
  and the legacy-basename notice wording),
- the ``SessionCwdMismatchUseCase`` walk over a fake pane snapshot,
- the ``LegacyBasenameNoticeUseCase`` short-circuits (empty / equal basename,
  missing legacy session, foreign-repo session) and the emitted notice,
- the ``ResolveStatusSessionUseCase`` precedence (explicit > current tmux >
  canonical fallback).

The end-to-end behavior over the live ``commands.*`` reads stays pinned by the
``session_cwd_mismatch`` / ``legacy_basename_session_notice`` /
``resolve_status_session`` characterization tests in the integration suite; this
file pins the boundary in isolation — the OOP-first carve's payoff.
"""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.application.status_session_helper import (
    LegacyBasenameNoticeUseCase,
    ResolveStatusSessionUseCase,
    SessionCwdMismatchUseCase,
    cwd_is_under_repo,
    legacy_basename_notice_text,
    select_offending_cwds,
)


class _FakeReads:
    """A synthetic :class:`StatusSessionHelperReads`; every read is configured."""

    def __init__(
        self,
        *,
        panes: list | None = None,
        session_exists: bool = False,
        cwd_mismatch: list[str] | None = None,
        current_session: str | None = None,
        repo_root: Path = Path("/repo"),
        canonical: str = "mozyo-repo-deadbeef",
    ) -> None:
        self._panes = panes or []
        self._session_exists = session_exists
        self._cwd_mismatch = cwd_mismatch or []
        self._current_session = current_session
        self._repo_root = repo_root
        self._canonical = canonical
        self.calls: list[tuple] = []

    def pane_lines(self) -> list:
        self.calls.append(("pane_lines",))
        return list(self._panes)

    def session_exists(self, session: str) -> bool:
        self.calls.append(("session_exists", session))
        return self._session_exists

    def session_cwd_mismatch(self, session: str, repo_root: Path) -> list[str]:
        self.calls.append(("session_cwd_mismatch", session, repo_root))
        return list(self._cwd_mismatch)

    def current_session_name(self) -> str | None:
        self.calls.append(("current_session_name",))
        return self._current_session

    def repo_root_from_args(self, args: argparse.Namespace) -> Path:
        self.calls.append(("repo_root_from_args",))
        return self._repo_root

    def canonical_session_name(self, repo_root: Path) -> str:
        self.calls.append(("canonical_session_name", repo_root))
        return self._canonical


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


class CwdIsUnderRepoTest(unittest.TestCase):
    def test_empty_cwd_is_treated_as_under(self) -> None:
        self.assertTrue(cwd_is_under_repo("", Path("/repo")))

    def test_path_inside_repo_is_under(self) -> None:
        with mock.patch.object(Path, "resolve", lambda self: self):
            self.assertTrue(cwd_is_under_repo("/repo/sub", Path("/repo")))

    def test_foreign_path_is_not_under(self) -> None:
        with mock.patch.object(Path, "resolve", lambda self: self):
            self.assertFalse(cwd_is_under_repo("/elsewhere", Path("/repo")))


class SelectOffendingCwdsTest(unittest.TestCase):
    def _panes(self, *entries: tuple[str, str]) -> list[dict]:
        return [{"location": loc, "cwd": cwd} for loc, cwd in entries]

    def test_no_panes_in_session_returns_empty(self) -> None:
        panes = self._panes(("elsewhere:0.0", "/tmp"))
        self.assertEqual([], select_offending_cwds(panes, "my-project", Path("/repo")))

    def test_any_pane_under_repo_returns_empty(self) -> None:
        with mock.patch.object(Path, "resolve", lambda self: self):
            panes = self._panes(("s:0.0", "/repo/a"), ("s:0.1", "/elsewhere"))
            self.assertEqual([], select_offending_cwds(panes, "s", Path("/repo")))

    def test_all_panes_outside_repo_returns_offenders(self) -> None:
        with mock.patch.object(Path, "resolve", lambda self: self):
            panes = self._panes(("s:0.0", "/x"), ("s:0.1", "/y"))
            self.assertEqual(["/x", "/y"], select_offending_cwds(panes, "s", Path("/repo")))

    def test_empty_cwd_suppresses_mismatch(self) -> None:
        # An empty/absent cwd counts as "under" (unknown-but-not-foreign), so a
        # session that has any such pane is never flagged, exactly as the
        # procedural handler behaved.
        with mock.patch.object(Path, "resolve", lambda self: self):
            panes = [{"location": "s:0.0"}, {"location": "s:0.1", "cwd": "/x"}]
            self.assertEqual([], select_offending_cwds(panes, "s", Path("/repo")))


class LegacyBasenameNoticeTextTest(unittest.TestCase):
    def test_wording_names_legacy_and_derived(self) -> None:
        text = legacy_basename_notice_text("my-project", "mozyo-my-project-dead")
        self.assertIn("legacy session 'my-project'", text)
        self.assertIn("mozyo-my-project-dead", text)
        self.assertIn("tmux kill-session -t my-project", text)


# --------------------------------------------------------------------------- #
# Use cases
# --------------------------------------------------------------------------- #


class SessionCwdMismatchUseCaseTest(unittest.TestCase):
    def test_reads_pane_snapshot_and_flags_foreign_session(self) -> None:
        with mock.patch.object(Path, "resolve", lambda self: self):
            reads = _FakeReads(panes=[{"location": "s:0.0", "cwd": "/x"}])
            result = SessionCwdMismatchUseCase(reads).resolve("s", Path("/repo"))
        self.assertEqual(["/x"], result)
        self.assertIn(("pane_lines",), reads.calls)


class LegacyBasenameNoticeUseCaseTest(unittest.TestCase):
    def test_empty_basename_short_circuits(self) -> None:
        reads = _FakeReads(session_exists=True)
        # Path("/") has an empty name.
        self.assertIsNone(
            LegacyBasenameNoticeUseCase(reads).resolve(Path("/"), "derived")
        )
        self.assertEqual([], reads.calls)

    def test_basename_equals_derived_short_circuits(self) -> None:
        reads = _FakeReads(session_exists=True)
        self.assertIsNone(
            LegacyBasenameNoticeUseCase(reads).resolve(Path("/tmp/foo"), "foo")
        )
        self.assertEqual([], reads.calls)

    def test_missing_legacy_session_returns_none(self) -> None:
        reads = _FakeReads(session_exists=False)
        self.assertIsNone(
            LegacyBasenameNoticeUseCase(reads).resolve(Path("/tmp/foo"), "derived")
        )

    def test_foreign_repo_session_returns_none(self) -> None:
        reads = _FakeReads(session_exists=True, cwd_mismatch=["/elsewhere"])
        self.assertIsNone(
            LegacyBasenameNoticeUseCase(reads).resolve(Path("/tmp/foo"), "derived")
        )

    def test_own_legacy_session_emits_notice(self) -> None:
        reads = _FakeReads(session_exists=True, cwd_mismatch=[])
        notice = LegacyBasenameNoticeUseCase(reads).resolve(
            Path("/tmp/2026PBL_local"), "mozyo-2026pbl-dead"
        )
        self.assertIsNotNone(notice)
        self.assertIn("2026PBL_local", notice)
        self.assertIn("mozyo-2026pbl-dead", notice)


class ResolveStatusSessionUseCaseTest(unittest.TestCase):
    def test_explicit_session_wins(self) -> None:
        reads = _FakeReads(current_session="other")
        args = argparse.Namespace(session="custom", repo=None)
        self.assertEqual("custom", ResolveStatusSessionUseCase(reads).resolve(args))
        # Explicit wins before any read.
        self.assertEqual([], reads.calls)

    def test_current_tmux_session_next(self) -> None:
        reads = _FakeReads(current_session="mozyo_bridge")
        args = argparse.Namespace(session=None, repo=None)
        self.assertEqual(
            "mozyo_bridge", ResolveStatusSessionUseCase(reads).resolve(args)
        )

    def test_canonical_fallback_when_not_in_tmux(self) -> None:
        reads = _FakeReads(current_session=None, canonical="mozyo-repo-dead")
        args = argparse.Namespace(session=None, repo="/repo")
        self.assertEqual(
            "mozyo-repo-dead", ResolveStatusSessionUseCase(reads).resolve(args)
        )
        self.assertIn(("repo_root_from_args",), reads.calls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
