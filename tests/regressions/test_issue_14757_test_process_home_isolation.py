"""Test processes must not reach the operator's shared home (#14757).

Recurrence pins for one defect: an ordinary test process resolved the canonical
mozyo-bridge home contract onto the operator's *shared* home and wrote there.
Two confirmed shapes, both pinned below:

- **schema forward-migration** — a run whose tests were green migrated the
  operator's shared store v7 -> v8 (#14477 j#94521 / j#94527 / j#94528). The
  reach happened through the cleared-env fallback: dozens of test files use
  ``patch.dict(os.environ, {}, clear=True)``, and ``~/.mozyo_bridge`` then
  resolves through ``expanduser()`` -- which falls back to the passwd database
  when ``HOME`` is also unset -- straight onto the operator's home.
- **registry row insertion** — the two ``_reach_preflight`` tests in
  ``test_issue_14741_recovery_launch_cause.py`` passed ``MOZYO_BRIDGE_HOME`` as a
  function argument while production resolved it from ``os.environ``, registering
  a throwaway workspace into the operator's live registry twice per run
  (#14757 j#100381).

Every assertion here is recurrence detection for that defect. Nothing in this
file reads or writes the operator's real home: the "operator home" is always a
temp directory standing in for it.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E402
    test_home_fence,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application.test_home_fence import (  # noqa: E402,E501
    ambient_homes,
    effective_home,
    isolated_env,
    snapshot_home,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E402,E501
    LIVE_LANE_ENV_KEYS,
    compare_snapshots,
)
from mozyo_bridge.shared import paths as shared_paths  # noqa: E402
from mozyo_bridge.shared.paths import (  # noqa: E402
    HomeFence,
    OperatorHomeFenceViolation,
    bind_process_home_fence,
    mozyo_bridge_home,
)


def _write_store(path: Path, *, user_version: int, rows: int) -> None:
    """A minimal home-shaped SQLite store, so the guard has something to read."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO workspaces VALUES (?)", [(f"ws-{i}",) for i in range(rows)]
        )
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.commit()
    finally:
        conn.close()


class ClearedEnvNeverReachesTheOperatorHomeTest(unittest.TestCase):
    """A fenced process with no environment left still resolves inside the fence."""

    def setUp(self) -> None:
        self._task = TemporaryDirectory()
        self.addCleanup(self._task.cleanup)
        root = Path(self._task.name)
        # Resolved: on macOS the temp root is reached through a symlink, and the
        # fence resolves both sides, so an unresolved expectation would compare
        # two spellings of the same directory.
        self.fence_home = (root / "fence-home").resolve()
        self.operator_home = (root / "operator-home").resolve()
        for directory in (self.fence_home, self.operator_home):
            directory.mkdir()
        self.addCleanup(bind_process_home_fence, bind_process_home_fence(None))

    def test_a_cleared_environment_resolves_to_the_fence_not_the_operator_home(
        self,
    ) -> None:
        """The exact #14477 reach: env cleared, so the fallback must not be shared.

        With the environment gone, `expanduser()` falls through to the passwd
        database and reaches the *real* operator default -- so that spelling has to
        be in the deny set for the redirect to fire. That is not a contrivance:
        `ambient_homes()` always includes it, which is why the runner's deny set is
        built from both the default spelling and any explicit one.
        """
        bind_process_home_fence(
            HomeFence(
                root=self.fence_home,
                denied=(self.operator_home, *ambient_homes()),
            )
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mozyo_bridge_home(), self.fence_home)

    def test_resolving_onto_the_operator_home_is_refused_not_returned(self) -> None:
        """A named reach is a refusal at the resolver, not a silent mutation."""
        bind_process_home_fence(
            HomeFence(root=self.fence_home, denied=(self.operator_home,))
        )
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.operator_home)}):
            with self.assertRaises(OperatorHomeFenceViolation):
                mozyo_bridge_home()

    def test_a_path_below_the_operator_home_is_refused_too(self) -> None:
        """The fence denies the subtree, not just the exact root."""
        bind_process_home_fence(
            HomeFence(root=self.fence_home, denied=(self.operator_home,))
        )
        nested = self.operator_home / "nested"
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(nested)}):
            with self.assertRaises(OperatorHomeFenceViolation):
                mozyo_bridge_home()

    def test_a_safe_tilde_default_is_kept_rather_than_overwritten(self) -> None:
        """The fence is narrow: it only changes answers that were unsafe.

        A test that points `HOME` at its own temp dir and then reads the home
        contract is characterising the documented `~/.mozyo_bridge` fallback, and
        its expected answer is already isolated. An earlier version of this fence
        substituted the fence root unconditionally whenever `MOZYO_BRIDGE_HOME`
        was absent, which broke exactly that characterisation
        (`test_scaffold.ScaffoldRulesTest.test_rules_home_resolved_expands_tilde_default`,
        measured in the #14757 R1 full run).
        """
        bind_process_home_fence(
            HomeFence(root=self.fence_home, denied=(self.operator_home,))
        )
        fake_home = self.operator_home.parent / "fake-home"
        fake_home.mkdir()
        with patch.dict(os.environ, {"HOME": str(fake_home)}) as _env:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
            self.assertEqual(
                mozyo_bridge_home(), (fake_home / ".mozyo_bridge").resolve()
            )

    def test_an_unsafe_tilde_default_is_redirected_to_the_fence(self) -> None:
        """The same branch, when the expansion does land on shared state."""
        bind_process_home_fence(
            HomeFence(root=self.fence_home, denied=(self.operator_home.parent,))
        )
        with patch.dict(os.environ, {"HOME": str(self.operator_home.parent)}) as _env:
            os.environ.pop("MOZYO_BRIDGE_HOME", None)
            self.assertEqual(mozyo_bridge_home(), self.fence_home)

    def test_an_unfenced_process_is_byte_invariant(self) -> None:
        """Production is unfenced and must behave exactly as before the fence."""
        bind_process_home_fence(None)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.operator_home)}):
            self.assertEqual(mozyo_bridge_home(), self.operator_home.resolve())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                mozyo_bridge_home(),
                Path("~/.mozyo_bridge").expanduser().resolve(),
            )

    def test_removing_the_rebind_reaches_the_operator_home_and_is_detected(
        self,
    ) -> None:
        """Negative probe (#14757 acceptance 4): without the fence, the reach lands.

        Two halves, and both are required. The first shows the fence is doing real
        work rather than agreeing with an already-safe environment: with the
        rebind removed, the same resolution returns the operator home. The second
        shows the escape is not silent: a write there moves the guarded snapshot,
        so the run is fail-closed even though no exception was raised.
        """
        store = self.operator_home / "registry.sqlite"
        _write_store(store, user_version=7, rows=2)
        before = snapshot_home(self.operator_home)

        bind_process_home_fence(None)
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(self.operator_home)}):
            reached = mozyo_bridge_home()
        self.assertEqual(reached, self.operator_home.resolve())

        # The write the unfenced resolution then performs: one more workspace row
        # and a forward-migrated schema version -- the two confirmed shapes.
        conn = sqlite3.connect(reached / "registry.sqlite")
        try:
            conn.execute("INSERT INTO workspaces VALUES ('ws-leaked')")
            conn.execute("PRAGMA user_version = 8")
            conn.commit()
        finally:
            conn.close()

        verdict = compare_snapshots(before, snapshot_home(self.operator_home))
        self.assertFalse(verdict.unchanged)
        self.assertIn("schema", {delta.tier for delta in verdict.deltas})
        self.assertIn("identity", {delta.tier for delta in verdict.deltas})


class GuardDetectsOnlyRealChangesTest(unittest.TestCase):
    """The guard must not cry wolf, or it gets switched off within a week."""

    def setUp(self) -> None:
        self._task = TemporaryDirectory()
        self.addCleanup(self._task.cleanup)
        self.home = Path(self._task.name) / "operator-home"
        self.home.mkdir()
        _write_store(self.home / "registry.sqlite", user_version=7, rows=3)

    def test_an_untouched_home_is_unchanged(self) -> None:
        before = snapshot_home(self.home)
        self.assertTrue(compare_snapshots(before, snapshot_home(self.home)).unchanged)

    def test_a_concurrent_touch_of_an_existing_row_is_not_a_test_write(self) -> None:
        """The operator's own cockpit updates existing rows continuously.

        Row *counts* are guarded, not row contents, so `last_seen` /`updated_at`
        churn from the live lane does not read as a test process writing.
        """
        before = snapshot_home(self.home)
        conn = sqlite3.connect(self.home / "registry.sqlite")
        try:
            conn.execute("UPDATE workspaces SET workspace_id = workspace_id")
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(compare_snapshots(before, snapshot_home(self.home)).unchanged)

    def test_a_new_file_in_home_is_a_change(self) -> None:
        before = snapshot_home(self.home)
        (self.home / "redmine-credentials.yaml").write_text("x", encoding="utf-8")
        verdict = compare_snapshots(before, snapshot_home(self.home))
        self.assertFalse(verdict.unchanged)
        self.assertIn("entries", {delta.tier for delta in verdict.deltas})

    def test_a_pre_write_backup_is_a_change(self) -> None:
        """A migration that backed the store up then rolled back still shows."""
        before = snapshot_home(self.home)
        backup = self.home / "backups" / "state-20260806T000000Z"
        backup.mkdir(parents=True)
        (backup / "state.sqlite").write_bytes(b"")
        verdict = compare_snapshots(before, snapshot_home(self.home))
        self.assertFalse(verdict.unchanged)
        self.assertIn("backups", {delta.tier for delta in verdict.deltas})

    def test_creating_the_home_from_nothing_is_a_change(self) -> None:
        absent = Path(self._task.name) / "never-existed"
        before = snapshot_home(absent)
        self.assertTrue(before.missing)
        absent.mkdir()
        verdict = compare_snapshots(before, snapshot_home(absent))
        self.assertFalse(verdict.unchanged)
        self.assertIn("existence", {delta.tier for delta in verdict.deltas})

    def test_an_unreadable_store_fails_the_guard_instead_of_passing(self) -> None:
        """"I could not look" must never read as "nothing changed"."""
        (self.home / "corrupt.sqlite").write_bytes(b"not a database at all")
        snapshot = snapshot_home(self.home)
        self.assertTrue(snapshot.unreadable)
        self.assertFalse(compare_snapshots(snapshot, snapshot).unchanged)


class SnapshotNeverAssertsImmutabilityTest(unittest.TestCase):
    """#14757 acceptance 5: no ``immutable=1`` on mutable evidence."""

    def test_the_snapshot_source_uses_mode_ro_and_never_immutable(self) -> None:
        """A source pin, because the prohibition is about the URI we may write.

        ``immutable=1`` asserts the file cannot change, which is false while the
        operator's cockpit is running; a torn read would then be reported as a
        schema change. The consistency requirement is met by the online backup
        API instead, which the behavioural test below exercises.
        """
        tree = ast.parse(Path(test_home_fence.__file__).read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                text = ast.get_docstring(node, clean=False)
                if text is not None:
                    docstrings.add(text)
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ]
        # The prose above is allowed to *name* the prohibition; only the strings
        # that reach `sqlite3.connect` are constrained.
        self.assertNotIn(
            True, [("immutable" in literal) for literal in literals], "immutable URI"
        )
        self.assertTrue(any("mode=ro" in literal for literal in literals))

    def test_the_snapshot_reads_a_store_that_is_being_written(self) -> None:
        """An open write transaction elsewhere must not tear the snapshot."""
        with TemporaryDirectory() as task:
            home = Path(task)
            store = home / "registry.sqlite"
            _write_store(store, user_version=7, rows=2)
            writer = sqlite3.connect(store)
            try:
                writer.execute("BEGIN")
                writer.execute("INSERT INTO workspaces VALUES ('ws-uncommitted')")
                snapshot = snapshot_home(home)
                # The uncommitted row is invisible and nothing is reported
                # unreadable: the copy is a consistent point-in-time image.
                self.assertEqual(snapshot.unreadable, ())
                writer.rollback()
            finally:
                writer.close()
            self.assertTrue(compare_snapshots(snapshot, snapshot_home(home)).unchanged)

    def test_the_snapshot_does_not_create_a_missing_store(self) -> None:
        """``mode=ro`` must refuse to create, so probing cannot itself write."""
        with TemporaryDirectory() as task:
            home = Path(task)
            self.assertEqual(snapshot_home(home).entry_count, 0)
            self.assertEqual(list(home.iterdir()), [])


class IsolationEnvContractTest(unittest.TestCase):
    """#14757 acceptance 1: task-specific root, and ``HOME`` left alone."""

    def test_the_child_env_pins_the_home_contract_without_repurposing_home(
        self,
    ) -> None:
        """Pointing ``HOME`` at a temp dir is the repair this rail must not need.

        A repurposed ``HOME`` hides user site-packages and the operator's git
        identity, which #13733 had to patch around with ``PYTHONUSERBASE`` and a
        synthetic committer. Isolation here comes from the resolver instead.
        """
        with TemporaryDirectory() as task:
            base = {"HOME": "/operator/home", "PATH": "/usr/bin"}
            layout, env, _interpreter, _ledger = isolated_env(
                Path(task),
                denied_homes=(Path(task) / "denied-home",),
                base_env=base,
            )
            self.assertEqual(env["HOME"], "/operator/home")
            self.assertEqual(env["MOZYO_BRIDGE_HOME"], str(layout.home))
            for key in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(env[key], str(layout.tmp))
            for key in (
                "XDG_CONFIG_HOME",
                "XDG_CACHE_HOME",
                "XDG_DATA_HOME",
                "XDG_STATE_HOME",
            ):
                self.assertTrue(env[key].startswith(str(layout.root)))
            self.assertEqual(
                env[shared_paths.HOME_FENCE_ROOT_ENV], str(layout.home)
            )
            self.assertIn(
                str(Path(task) / "denied-home"),
                env[shared_paths.HOME_FENCE_DENY_ENV],
            )
            for directory in layout.directories:
                self.assertTrue(directory.is_dir())

    def test_the_child_env_drops_the_live_lane_pins(self) -> None:
        with TemporaryDirectory() as task:
            base = {key: "live" for key in LIVE_LANE_ENV_KEYS}
            _layout, env, _interpreter, _ledger = isolated_env(
                Path(task), denied_homes=(Path(task) / "denied-home",), base_env=base
            )
            for key in LIVE_LANE_ENV_KEYS:
                self.assertNotIn(key, env)

    def test_the_denied_set_covers_both_spellings_of_the_shared_home(self) -> None:
        """A lane-pinned env must not slip past the default spelling."""
        with TemporaryDirectory() as task:
            lane_home = Path(task) / "lane-home"
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(lane_home)}):
                denied = ambient_homes()
                self.assertIn(lane_home.resolve(), denied)
                self.assertIn(Path("~/.mozyo_bridge").expanduser().resolve(), denied)
                # The guarded target is the one this invocation would have used.
                self.assertEqual(effective_home(), lane_home.resolve())


class LaunchPreflightRegressionDoesNotRegisterIntoAmbientHomeTest(unittest.TestCase):
    """The #14741 producer, pinned where it happened (#14757 j#100381).

    `_prepare_session_locked` resolves the home contract from `os.environ` and
    calls `register_workspace(repo_root)` with no `home=`, so passing the home
    only as a function argument registered a throwaway workspace into whatever
    home the *process* pointed at. Here the ambient home is a temp stand-in, and
    the pin is that the regression leaves it with zero workspaces.
    """

    def test_reaching_the_preflight_leaves_the_ambient_home_untouched(self) -> None:
        from tests.regressions.test_issue_14741_recovery_launch_cause import (
            PropagationTest,
        )

        with TemporaryDirectory() as task:
            ambient = Path(task) / "ambient-home"
            ambient.mkdir()
            case = PropagationTest("test_an_ordinary_launch_arrives_unarmed")
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(ambient)}):
                before = snapshot_home(ambient)
                case._reach_preflight()
                verdict = compare_snapshots(before, snapshot_home(ambient))
            self.assertTrue(
                verdict.unchanged,
                f"the regression wrote into the ambient home: {verdict.reasons}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
