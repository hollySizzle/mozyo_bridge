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
import errno
import io
import json
import os
import pwd
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E402
    commands_test_run,
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
    HomeGuardVerdict,
    IsolatedRunOutcome,
    compare_snapshots,
)
from mozyo_bridge.shared import paths as shared_paths  # noqa: E402
from mozyo_bridge.shared.paths import (  # noqa: E402
    HomeFence,
    OperatorHomeFenceViolation,
    bind_process_home_fence,
    mozyo_bridge_home,
)
from tests.support.private_path_fixtures import macos_home_path  # noqa: E402


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
        database, so the deny set has to hold *that* spelling for the redirect to
        fire. It is modelled here rather than borrowed from `ambient_homes()`
        (#15229). `ambient_homes()` expands `~` from the **current** `HOME`, and
        under `mozyo-bridge tests parallel` every shard is handed a fresh
        task-local `HOME` (#13733 acceptance 3) -- so it named the shard's home
        while the cleared-env fallback still reached the operator's real account.
        The premise silently failed and the resolver was then measured returning
        the operator home: serial green, parallel red, for a reason about where
        the fixture got its expectation rather than about the fence.

        Patching only `pwd.getpwuid` keeps the environment genuinely empty and the
        production fallback genuinely exercised, with a deterministic answer no
        launch mode can move -- the shape j#100498 adopted for the end-to-end twin
        of this test. `ambient_homes()` stays in the deny set: this makes the set
        broader, never narrower.
        """
        account = Path(self._task.name).resolve() / "fake-account"
        account.mkdir()
        real = pwd.getpwuid(os.getuid())
        synthetic = type(real)(
            (real.pw_name, real.pw_passwd, real.pw_uid, real.pw_gid,
             real.pw_gecos, str(account), real.pw_shell)
        )
        bind_process_home_fence(
            HomeFence(
                root=self.fence_home,
                denied=(
                    self.operator_home,
                    (account / ".mozyo_bridge").resolve(),
                    *ambient_homes(),
                ),
            )
        )
        with patch.dict(os.environ, {}, clear=True), patch.object(
            pwd, "getpwuid", return_value=synthetic
        ):
            # The premise: the fallback really did reach the modelled account, so
            # a redirect to the fence root is evidence and not a coincidence.
            self.assertEqual(
                str(Path("~/.mozyo_bridge").expanduser()),
                str(account / ".mozyo_bridge"),
            )
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
            denied = Path(task) / "denied-home"
            denied.mkdir()
            base = {"HOME": "/operator/home", "PATH": "/usr/bin"}
            layout, env, _interp, _ledger, os_fence = isolated_env(
                Path(task),
                denied_homes=(denied,),
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
            # The deny pins come from the FINAL fence authority, not the caller's
            # request (j#100489 F2) -- and under an inherited boundary those are the
            # outer protected roots, not this caller's subset. Asserting the
            # requested path here made the verdict depend on whether the suite was
            # launched inside a fence. The contract is that all four consumers read
            # ONE authority, so assert exactly that.
            self.assertEqual(
                env[shared_paths.HOME_FENCE_DENY_ENV].split(os.pathsep),
                [str(path) for path in os_fence.denied],
            )
            if not os_fence.inherited:
                self.assertEqual(os_fence.denied, (denied.resolve(),))
            for directory in layout.directories:
                self.assertTrue(directory.is_dir())

    def test_the_child_env_drops_the_live_lane_pins(self) -> None:
        with TemporaryDirectory() as task:
            denied = Path(task) / "denied-home"
            denied.mkdir()
            base = {key: "live" for key in LIVE_LANE_ENV_KEYS}
            _layout, env, _interp, _ledger, _os_fence = isolated_env(
                Path(task), denied_homes=(denied,), base_env=base
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


class IdentityDeltaIsDiagnosticButNeverDisclosingTest(unittest.TestCase):
    """The identity delta names what moved, without disclosing a single row.

    j#100482 produced a red run whose only evidence was two opaque digests, so it
    could not be attributed. j#100487 rejected narrowing the guard to fix that --
    every table still counts, and any drift is still non-zero -- and asked instead
    for value-free granularity in the *report*.
    """

    def _snapshot(self, counts, **kw):
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E501
            HomeSnapshot,
            TableCount,
            digest,
        )

        parts = tuple(TableCount(store=s, table=t, rows=c) for s, t, c in counts)
        return HomeSnapshot(
            home="/nowhere",
            identity_digest=digest(tuple(p.as_key() + f"={p.rows}" for p in parts)),
            identity_counts=parts,
            **kw,
        )

    def test_it_names_the_store_table_and_row_counts_that_moved(self) -> None:
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E501
            compare_snapshots,
        )

        before = self._snapshot(
            [("registry.sqlite", "workspaces", 43), ("registry.sqlite", "events", 7)]
        )
        after = self._snapshot(
            [("registry.sqlite", "workspaces", 44), ("registry.sqlite", "events", 7)]
        )
        verdict = compare_snapshots(before, after)
        self.assertFalse(verdict.unchanged)
        detail = next(d.detail for d in verdict.deltas if d.tier == "identity")
        self.assertIn("registry.sqlite/workspaces 43->44", detail)
        # The table that did NOT move stays out of the report.
        self.assertNotIn("events", detail)

    def test_a_changed_identity_set_is_reported_without_naming_any_id(self) -> None:
        """Row counts equal, digest moved: say so, but never print a workspace id."""
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E501
            HomeSnapshot,
            compare_snapshots,
        )

        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E501
            TableCount,
        )

        counts = (TableCount(store="registry.sqlite", table="workspaces", rows=2),)
        before = HomeSnapshot(
            home="/nowhere", identity_digest="aaa", identity_counts=counts
        )
        after = HomeSnapshot(
            home="/nowhere", identity_digest="bbb", identity_counts=counts
        )
        verdict = compare_snapshots(before, after)
        detail = next(d.detail for d in verdict.deltas if d.tier == "identity")
        self.assertEqual(detail, "registry workspace identity set changed")

    def test_the_guard_still_fails_on_any_drift(self) -> None:
        """The report got finer; the guard did not get weaker (j#100487).

        A single row appended anywhere is still a non-zero verdict -- including in a
        high-churn table, which is exactly where a test-process append would hide if
        the guard were narrowed to an authority allowlist.
        """
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (  # noqa: E501
            compare_snapshots,
        )

        before = self._snapshot([("telemetry.sqlite", "spans", 10_000)])
        after = self._snapshot([("telemetry.sqlite", "spans", 10_001)])
        self.assertFalse(compare_snapshots(before, after).unchanged)


def _bind_source_for(prefix: list[str], flag: str, target: Path) -> str | None:
    """The source mounted by one bwrap ``(flag, source, target)`` operation."""
    for i, token in enumerate(prefix):
        if token == flag and i + 2 < len(prefix) and prefix[i + 2] == str(target):
            return prefix[i + 1]
    return None


def _has_unary_mount(prefix: list[str], flag: str, target: Path) -> bool:
    """Whether bwrap carries one ``(flag, target)`` operation."""
    return any(
        token == flag and i + 1 < len(prefix) and prefix[i + 1] == str(target)
        for i, token in enumerate(prefix)
    )


class AbsentDeniedRootOnLinuxTest(unittest.TestCase):
    """bwrap must protect an absent root without materialising it on the host.

    A clean Linux runner has no ``~/.mozyo_bridge`` until something creates it, and
    bwrap creates the destination of a normal ``--ro-bind`` before mounting it.
    The rejected R3 argv performed that creation through a read-write host-root
    bind. The corrected boundary starts with a read-only host view and opens only
    the task root, so an absent external denied root needs no destination mount.
    """

    def _resolve_linux(self, denied, work_dir):
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E501
            test_home_os_fence as mod,
        )

        canary = work_dir / "canary"
        canary.mkdir(parents=True, exist_ok=True)
        (work_dir / "control").mkdir(parents=True, exist_ok=True)
        # Also force the fresh branch: under `mozyo-bridge tests run` this process
        # is already inside a boundary, `resolve_os_fence` returns the prefix-less
        # inherited fence, and no bwrap argv is built at all -- so without this the
        # test silently measures nothing when run under the rail it belongs to.
        with patch.object(mod.platform, "system", return_value="Linux"), patch.object(
            mod.shutil, "which", return_value="/usr/bin/bwrap"
        ), patch.object(mod, "inherited_fence", return_value=None):
            return mod.resolve_os_fence(
                denied, work_dir=work_dir, canary=canary
            )

    def test_an_absent_external_root_uses_the_read_only_host_view(self) -> None:
        with TemporaryDirectory() as parent:
            parent_path = Path(parent).resolve()
            work = parent_path / "task"
            work.mkdir()
            absent = parent_path / "no-such-home"
            fence = self._resolve_linux((absent,), work)
            prefix = list(fence.argv_prefix)

            self.assertEqual(_bind_source_for(prefix, "--ro-bind", Path("/")), "/")
            self.assertEqual(_bind_source_for(prefix, "--bind", work), str(work))
            self.assertIsNone(
                _bind_source_for(prefix, "--ro-bind", absent),
                "bwrap would create the absent host destination before binding it",
            )
            private_tmp = Path("/tmp").resolve()
            if private_tmp in absent.parents:
                self.assertTrue(_has_unary_mount(prefix, "--tmpfs", absent))
                self.assertTrue(_has_unary_mount(prefix, "--remount-ro", absent))
            self.assertFalse(
                absent.exists(), "resolving the fence created the operator home"
            )

    def test_the_existing_task_local_canary_is_rebound_read_only(self) -> None:
        with TemporaryDirectory() as parent:
            parent_path = Path(parent).resolve()
            work = parent_path / "task"
            work.mkdir()
            canary = work / "canary"
            canary.mkdir()
            (work / "control").mkdir()
            denied = parent_path / "operator-home"
            denied.mkdir()
            prefix = list(self._resolve_linux((denied,), work).argv_prefix)
            self.assertEqual(
                _bind_source_for(prefix, "--ro-bind", canary), str(canary)
            )

    def test_an_absent_denied_root_inside_the_write_hole_is_refused(self) -> None:
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E501
            test_home_os_fence as mod,
        )

        with TemporaryDirectory() as task:
            work = Path(task).resolve()
            absent = work / "absent-denied"
            with self.assertRaisesRegex(mod.OsFenceUnavailable, "overlaps"):
                self._resolve_linux((absent,), work)

    def test_a_task_root_inside_a_denied_root_is_refused(self) -> None:
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E501
            test_home_os_fence as mod,
        )

        with TemporaryDirectory() as parent:
            denied = Path(parent).resolve()
            work = denied / "task"
            work.mkdir()
            with self.assertRaisesRegex(mod.OsFenceUnavailable, "overlaps"):
                self._resolve_linux((denied,), work)

    def test_live_bwrap_refuses_creation_and_leaves_the_host_root_absent(self) -> None:
        """Observe the host while bwrap is live; argv inspection is not evidence."""
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E501
            test_home_os_fence as mod,
        )

        if mod.platform.system() != "Linux" or not mod.shutil.which("bwrap"):
            self.skipTest("live bubblewrap is a Linux CI contract")
        if mod.load_outer_context() is not None:
            self.skipTest("the bare CI hard check owns the non-nested live probe")

        with TemporaryDirectory(dir="/tmp") as task, TemporaryDirectory(
            dir="/var/tmp"
        ) as guard_parent:
            work = Path(task).resolve()
            absent = Path(guard_parent).resolve() / "host-absent-denied"
            fence = self._resolve_linux((absent,), work)
            ready = work / "control" / "ready"
            release = work / "control" / "release"
            code = (
                "import errno, os, pathlib, sys, time\n"
                f"path = pathlib.Path({str(absent)!r})\n"
                f"ready = pathlib.Path({str(ready)!r})\n"
                f"release = pathlib.Path({str(release)!r})\n"
                "try:\n"
                "    os.mkdir(path)\n"
                "except OSError as exc:\n"
                "    print(f'REFUSED {exc.errno}')\n"
                "    if exc.errno != errno.EROFS:\n"
                "        sys.exit(2)\n"
                "else:\n"
                "    print('CREATED')\n"
                "    sys.exit(3)\n"
                "ready.write_text('ready', encoding='utf-8')\n"
                "for _ in range(200):\n"
                "    if release.is_file():\n"
                "        sys.exit(0)\n"
                "    time.sleep(0.05)\n"
                "sys.exit(4)\n"
            )
            proc = subprocess.Popen(
                fence.wrap([sys.executable, "-c", code]),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                while (
                    not ready.is_file()
                    and proc.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                self.assertTrue(ready.is_file(), "the live child never reached its hold point")
                self.assertIsNone(proc.poll(), "the child was not live at host inspection")
                self.assertFalse(
                    absent.exists(), "bwrap transiently created the denied host root"
                )
                release.write_text("release", encoding="utf-8")
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, stderr or stdout)
                self.assertEqual(stdout.strip(), f"REFUSED {errno.EROFS}")
                self.assertFalse(absent.exists(), "bwrap left the denied host root")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)

    def test_an_existing_external_root_is_covered_by_the_read_only_host_view(self) -> None:
        with TemporaryDirectory() as parent:
            parent_path = Path(parent).resolve()
            work = parent_path / "task"
            work.mkdir()
            present = parent_path / "real-home"
            present.mkdir()
            prefix = list(self._resolve_linux((present,), work).argv_prefix)
            self.assertEqual(_bind_source_for(prefix, "--ro-bind", Path("/")), "/")
            expected = (
                str(present) if Path("/tmp").resolve() in present.parents else None
            )
            self.assertEqual(
                _bind_source_for(prefix, "--ro-bind", present), expected
            )


class MandatoryHardCheckCannotSkipTest(unittest.TestCase):
    """Backend resolution failure must be non-zero, never a skip (j#100490 item 1).

    `OsBoundaryRefusesEveryKnownBypassTest` is the mandatory OS-boundary hard check
    and its own docstring says skipping is not an option -- but its setup reached
    for `skipTest`, so on a host where the backend would not resolve the whole class
    reported success while measuring nothing. This drives the real class with
    resolution forced to fail and requires a failing result.
    """

    def test_a_backend_that_will_not_resolve_fails_the_hard_check(self) -> None:
        from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.application import (  # noqa: E501
            test_home_os_fence as fence_mod,
        )
        from tests.integration.e_150_quality_architecture.f_150_ci_verification import (  # noqa: E501
            test_test_home_isolation_runner as runner_mod,
        )

        case = runner_mod.OsBoundaryRefusesEveryKnownBypassTest(
            "test_a_dir_fd_relative_write_is_refused"
        )
        with patch.object(
            runner_mod, "load_outer_context", return_value=None
        ), patch.object(
            runner_mod,
            "resolve_os_fence",
            side_effect=fence_mod.OsFenceUnavailable("no backend on this host"),
        ):
            result = case.run()

        self.assertEqual(len(result.skipped), 0, "the mandatory hard check skipped")
        self.assertTrue(
            result.failures or result.errors,
            "an unresolvable backend produced a passing hard check",
        )
        self.assertFalse(result.wasSuccessful())


class RealSqliteIdentityDiagnosticTest(unittest.TestCase):
    """Real SQLite, not hand-built snapshots (j#100490 item 2).

    The diagnostic is only trustworthy if it survives what SQLite actually permits:
    identifiers containing tabs and newlines (which is why the surface is typed
    rather than tab-delimited text), tables appearing and disappearing, and a
    change that leaves every row count identical.
    """

    def _home(self, tables) -> Path:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = sqlite3.connect(home / "registry.sqlite")
        try:
            for name, rows in tables:
                escaped = name.replace('"', '""')
                conn.execute(f'CREATE TABLE "{escaped}" (workspace_id TEXT)')
                for i in range(rows):
                    conn.execute(
                        f'INSERT INTO "{escaped}" VALUES (?)', (f"ws-{i}",)
                    )
            conn.commit()
        finally:
            conn.close()
        return home

    def _snapshot(self, home: Path):
        return test_home_fence.snapshot_home(home)

    def test_a_created_table_is_named_in_the_detail(self) -> None:
        home = self._home([("workspaces", 1)])
        before = self._snapshot(home)
        conn = sqlite3.connect(home / "registry.sqlite")
        conn.execute("CREATE TABLE leases (id TEXT)")
        conn.commit()
        conn.close()
        verdict = compare_snapshots(before, self._snapshot(home))
        detail = next(d.detail for d in verdict.deltas if d.tier == "identity")
        self.assertIn("registry.sqlite/leases absent->0", detail)

    def test_a_dropped_table_is_named_in_the_detail(self) -> None:
        home = self._home([("workspaces", 1), ("leases", 0)])
        before = self._snapshot(home)
        conn = sqlite3.connect(home / "registry.sqlite")
        conn.execute("DROP TABLE leases")
        conn.commit()
        conn.close()
        detail = next(
            d.detail
            for d in compare_snapshots(before, self._snapshot(home)).deltas
            if d.tier == "identity"
        )
        self.assertIn("registry.sqlite/leases 0->absent", detail)

    def test_a_renamed_table_shows_both_sides(self) -> None:
        home = self._home([("workspaces", 2)])
        before = self._snapshot(home)
        conn = sqlite3.connect(home / "registry.sqlite")
        conn.execute("ALTER TABLE workspaces RENAME TO tenants")
        conn.commit()
        conn.close()
        detail = next(
            d.detail
            for d in compare_snapshots(before, self._snapshot(home)).deltas
            if d.tier == "identity"
        )
        self.assertIn("registry.sqlite/workspaces 2->absent", detail)
        self.assertIn("registry.sqlite/tenants absent->2", detail)

    def test_an_identity_change_with_identical_counts_is_still_red(self) -> None:
        """Delete one workspace, insert another: counts equal, identity moved.

        This is the case a row-count-only guard would miss, and the reason the
        registry's identity SET is digested alongside the counts.
        """
        home = self._home([("workspaces", 2)])
        before = self._snapshot(home)
        conn = sqlite3.connect(home / "registry.sqlite")
        conn.execute("DELETE FROM workspaces WHERE workspace_id = 'ws-0'")
        conn.execute("INSERT INTO workspaces VALUES ('ws-new')")
        conn.commit()
        conn.close()
        verdict = compare_snapshots(before, self._snapshot(home))
        self.assertFalse(verdict.unchanged)
        detail = next(d.detail for d in verdict.deltas if d.tier == "identity")
        # No count moved, so the detail says so -- and names no workspace id.
        self.assertEqual(detail, "registry workspace identity set changed")
        self.assertNotIn("ws-", detail)

    def test_identifiers_with_control_bytes_do_not_collide_or_leak_raw(self) -> None:
        """Tabs in table names broke the old delimited surface two ways.

        `a\tb` and a table literally named with the separator produced the same
        parsed key, and the raw control byte was rendered into output verbatim.
        """
        home = self._home([("odd\tname", 1), ("odd\nother", 1)])
        before = self._snapshot(home)
        conn = sqlite3.connect(home / "registry.sqlite")
        conn.execute('INSERT INTO "odd\tname" VALUES (?)', ("x",))
        conn.commit()
        conn.close()
        detail = next(
            d.detail
            for d in compare_snapshots(before, self._snapshot(home)).deltas
            if d.tier == "identity"
        )
        # Escaped for output, and only the table that actually moved is named.
        self.assertIn("odd\\tname 1->2", detail)
        self.assertNotIn("\t", detail)
        self.assertNotIn("\n", detail)
        self.assertNotIn("odd\\nother", detail)


class DefaultOutputCarriesNoOperatorPathTest(unittest.TestCase):
    """Default text and JSON verdicts disclose no absolute path (j#100490 item 4).

    These verdicts are pasted into Redmine journals and CI logs. An absolute
    operator-home path discloses the account name and local layout, and a task root
    discloses the same under a temp prefix. Both are withheld by default and
    available only behind an explicit local-debug flag.
    """

    def _sample_outcome(self):
        return IsolatedRunOutcome(
            guards=(
                HomeGuardVerdict(
                    home=macos_home_path("someone", ".mozyo_bridge"), ordinal=0
                ),
                HomeGuardVerdict(
                    home=macos_home_path("someone", "other-home"), ordinal=1
                ),
            ),
            suite_success=True,
            returncode=0,
            fence_root=macos_home_path("someone", "task", "mozyo-home"),
        )

    def _render(self, fmt: str, **kwargs) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            commands_test_run.render_outcome(
                self._sample_outcome(), label="unittest x", fmt=fmt, **kwargs
            )
        return buffer.getvalue()

    def test_text_output_hides_absolute_paths_by_default(self) -> None:
        rendered = self._render("text")
        self.assertNotIn(macos_home_path("someone"), rendered)
        self.assertIn("guarded-home[0]", rendered)
        # The two homes stay distinguishable without their paths.
        self.assertIn("guarded-home[1]", rendered)
        self.assertIn("fence-root[0]", rendered)

    def test_json_output_hides_absolute_paths_by_default(self) -> None:
        rendered = self._render("json")
        self.assertNotIn(macos_home_path("someone"), rendered)
        payload = json.loads(rendered)
        self.assertTrue(payload["fence_root"].startswith("fence-root["))
        labels = [guard["home"] for guard in payload["home_guards"]]
        self.assertEqual(len(set(labels)), 2, "the two homes collapsed to one label")

    def test_the_local_debug_flag_restores_absolute_paths(self) -> None:
        """Withholding must be a default, not a loss of the debugging surface."""
        for fmt in ("text", "json"):
            with self.subTest(fmt=fmt):
                rendered = self._render(fmt, reveal_paths=True)
                self.assertIn(
                    macos_home_path("someone", ".mozyo_bridge"), rendered
                )
                self.assertIn(
                    macos_home_path("someone", "task", "mozyo-home"), rendered
                )

    def test_the_same_home_gets_the_same_label_across_runs(self) -> None:
        """A digest label is only useful if it is stable and comparable."""
        first = HomeGuardVerdict(
            home=macos_home_path("someone", ".mozyo_bridge"), ordinal=0
        ).label
        second = HomeGuardVerdict(
            home=macos_home_path("someone", ".mozyo_bridge"), ordinal=0
        ).label
        other = HomeGuardVerdict(
            home=macos_home_path("someone", "elsewhere"), ordinal=0
        ).label
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
