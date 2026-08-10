"""Public-CLI tests for the `workspace alias` command family (#15190).

Review j#102104 required these specifically: the first round's tests called the
domain, the store and the launch chokepoint directly, so the argparse wiring,
the command handler's outcome mapping and the process exit codes — everything an
operator actually touches — were unverified.

Every case here goes through :func:`build_parser`, so the registered subcommand,
its flags, its handler and its exit code are all exercised together.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.workspace_registry import ANCHOR_SCHEMA_VERSION
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    MAX_DECLARATION_BYTES,
    REASON_ALIAS_CYCLE,
    REASON_CONCURRENT_CHANGE,
    REASON_DURABILITY_FAILED,
    REASON_CROSS_REPOSITORY,
    REASON_DECLARATION_INVALID,
    REASON_DECLARATION_UNREADABLE,
    REASON_NOT_REGULAR_FILE,
    REASON_PARENT_DRIFT,
    REASON_READBACK_FAILED,
    REASON_REMOVE_FAILED,
    REASON_SNAPSHOT_FAILED,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED_SCHEMA,
    REASON_TARGET_IDENTITY_UNRESOLVED,
    REASON_TARGET_MISSING,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure import (  # noqa: E501
    workspace_alias_store as store,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure.workspace_alias_store import (  # noqa: E501
    alias_path,
)


CANONICAL_ID = "ddd145b984ea4bc6ba842f72d4d4161f"
NESTED_ID = "262468a689664fb08615f56b5ef1afe1"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _write_anchor(root: Path, workspace_id: str, session: str) -> None:
    path = root / ".mozyo-bridge" / "workspace-anchor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": ANCHOR_SCHEMA_VERSION,
                "workspace_id": workspace_id,
                "canonical_session": session,
                "project_name": root.name,
                "created_at": "2026-08-09T00:00:00+00:00",
                "updated_at": "2026-08-09T00:00:00+00:00",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


class WorkspaceAliasCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        if not _git_available():  # pragma: no cover - environment guard
            self.skipTest("git is required for workspace alias CLI tests")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()

        home = self.base / "home"
        home.mkdir()
        previous = os.environ.get("MOZYO_BRIDGE_HOME")
        os.environ["MOZYO_BRIDGE_HOME"] = str(home)

        def _restore() -> None:
            if previous is None:
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
            else:
                os.environ["MOZYO_BRIDGE_HOME"] = previous

        self.addCleanup(_restore)

        self.canonical = self.base / "repo"
        self.canonical.mkdir()
        _git("init", "-q", cwd=self.canonical)
        self.nested = self.canonical / "Source" / "rails"
        self.nested.mkdir(parents=True)
        _write_anchor(self.canonical, CANONICAL_ID, "mozyo-repo-aaaaaaaa")
        _write_anchor(self.nested, NESTED_ID, "mozyo-rails-bbbbbbbb")

        self.parser = build_parser()

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        """Run one CLI invocation, returning ``(exit_code, parsed_json)``."""
        args = self.parser.parse_args(list(argv) + ["--json"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = args.func(args)
        raw = buffer.getvalue().strip()
        return code, (json.loads(raw) if raw else {})

    # --- happy path ---------------------------------------------------------

    def test_show_on_an_undeclared_workspace(self) -> None:
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "no_declaration")
        self.assertIsNone(payload["declaration"])

    def test_set_then_show_round_trip(self) -> None:
        code, payload = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
            "--reason", "rails app root",
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "aliased")
        self.assertEqual(Path(payload["launch_root"]), self.canonical)
        self.assertTrue(alias_path(self.nested).is_file())

        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "aliased")
        self.assertEqual(
            payload["declaration"]["canonical_workspace_id"], CANONICAL_ID
        )
        self.assertEqual(payload["declaration"]["reason"], "rails app root")

    def test_disable_then_show_reports_zero_launch(self) -> None:
        code, _ = self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "app root only",
        )
        self.assertEqual(code, 0)
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        # `show` answers the question session-start asks, so a disabled
        # workspace is a non-zero exit even though the command succeeded.
        self.assertEqual(code, 1)
        self.assertEqual(payload["state"], "launch_disabled")
        self.assertFalse(payload["ok"])

    def test_clear_restores_independent_launch(self) -> None:
        self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
        )
        code, payload = self.run_cli(
            "workspace", "alias", "clear", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "cleared")
        self.assertFalse(alias_path(self.nested).exists())
        # The identity anchor is untouched.
        self.assertTrue(
            (self.nested / ".mozyo-bridge" / "workspace-anchor.json").is_file()
        )
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "no_declaration")

    def test_clear_when_nothing_is_declared(self) -> None:
        code, payload = self.run_cli(
            "workspace", "alias", "clear", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "no_declaration")

    def test_redeclaring_preserves_created_at(self) -> None:
        _, first = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
        )
        _, second = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
        )
        self.assertEqual(
            first["declaration"]["created_at"], second["declaration"]["created_at"]
        )

    # --- write-time verification (nothing is written on refusal) ------------

    def test_set_refuses_a_target_without_durable_identity(self) -> None:
        plain = self.canonical / "unregistered"
        plain.mkdir()
        code, payload = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(plain),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_TARGET_IDENTITY_UNRESOLVED)
        self.assertFalse(alias_path(self.nested).exists())

    def test_set_refuses_a_missing_target(self) -> None:
        code, payload = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.base / "nope"),
        )
        self.assertEqual(code, 1)
        self.assertIn(
            payload["reason"], {REASON_TARGET_MISSING, REASON_TARGET_IDENTITY_UNRESOLVED}
        )
        self.assertFalse(alias_path(self.nested).exists())

    def test_set_refuses_a_cross_repository_target(self) -> None:
        inner = self.canonical / "vendor" / "engine"
        inner.mkdir(parents=True)
        _git("init", "-q", cwd=inner)
        inner_nested = inner / "app"
        inner_nested.mkdir()
        _write_anchor(inner_nested, "a" * 32, "mozyo-inner-cccccccc")
        code, payload = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(inner_nested), "--to", str(self.canonical),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_CROSS_REPOSITORY)
        self.assertFalse(alias_path(inner_nested).exists())

    def test_set_refuses_an_alias_chain(self) -> None:
        self.run_cli(
            "workspace", "alias", "disable", "--repo", str(self.canonical)
        )
        code, payload = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_ALIAS_CYCLE)
        self.assertFalse(alias_path(self.nested).exists())


class WorkspaceAliasCliFilesystemSafetyTests(WorkspaceAliasCliTestCase):
    """Review j#102104 Findings 1 & 2, driven through the public CLI."""

    def _symlink_declaration_to(self, target: Path) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    def test_disable_does_not_write_through_a_symlink(self) -> None:
        victim = self.base / "victim.txt"
        victim.write_text("SECRET-ORIGINAL", encoding="utf-8")
        self._symlink_declaration_to(victim)

        code, payload = self.run_cli(
            "workspace", "alias", "disable", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_NOT_REGULAR_FILE)
        self.assertEqual(victim.read_text(encoding="utf-8"), "SECRET-ORIGINAL")
        # Zero mutation: the symlink itself is left for the operator to inspect.
        self.assertTrue(alias_path(self.nested).is_symlink())

    def test_set_does_not_write_through_a_symlink(self) -> None:
        victim = self.base / "victim.txt"
        victim.write_text("SECRET-ORIGINAL", encoding="utf-8")
        self._symlink_declaration_to(victim)

        code, payload = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_NOT_REGULAR_FILE)
        self.assertEqual(victim.read_text(encoding="utf-8"), "SECRET-ORIGINAL")

    def test_show_refuses_a_symlinked_declaration(self) -> None:
        victim = self.base / "victim.txt"
        victim.write_text("{}", encoding="utf-8")
        self._symlink_declaration_to(victim)
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_NOT_REGULAR_FILE)

    def test_show_refuses_a_dangling_symlink(self) -> None:
        self._symlink_declaration_to(self.base / "absent-target")
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_NOT_REGULAR_FILE)

    def test_show_refuses_a_directory_at_the_declaration_path(self) -> None:
        alias_path(self.nested).mkdir(parents=True)
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_NOT_REGULAR_FILE)

    def test_clear_does_not_claim_success_when_removal_fails(self) -> None:
        alias_path(self.nested).mkdir(parents=True)
        code, payload = self.run_cli(
            "workspace", "alias", "clear", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_REMOVE_FAILED)
        self.assertTrue(alias_path(self.nested).is_dir())

    def test_clear_removes_a_symlink_without_touching_its_target(self) -> None:
        victim = self.base / "victim.txt"
        victim.write_text("SECRET-ORIGINAL", encoding="utf-8")
        self._symlink_declaration_to(victim)
        code, payload = self.run_cli(
            "workspace", "alias", "clear", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "cleared")
        self.assertFalse(alias_path(self.nested).exists(follow_symlinks=False))
        self.assertEqual(victim.read_text(encoding="utf-8"), "SECRET-ORIGINAL")

    def test_show_refuses_a_declaration_carrying_unknown_fields(self) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "alias",
                    "canonical_path": str(self.canonical),
                    "canonical_workspace_id": CANONICAL_ID,
                    "future_semantics": "launch-anyway",
                }
            ),
            encoding="utf-8",
        )
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_DECLARATION_INVALID)

    def test_show_refuses_malformed_json(self) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_DECLARATION_UNREADABLE)


class WorkspaceAliasRaceAndTypeTests(WorkspaceAliasCliTestCase):
    """Review j#102140 Findings 1–4, driven through the public CLI."""

    def _write_raw(self, payload: object) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    # --- F1: the pinned parent stopped being the visible one ----------------

    def test_parent_directory_drift_is_refused_and_leaves_nothing_visible(self) -> None:
        """A dirfd survives a rename, so a write can land in a detached directory.

        Without the drift check the CLI reported success while the
        workspace-visible path had no declaration at all.
        """
        real_open = store._open_parent

        def drifting(repo_root, *, create):
            fd = real_open(repo_root, create=create)
            parent = Path(repo_root) / ".mozyo-bridge"
            os.rename(parent, Path(repo_root) / ".mozyo-bridge.detached")
            parent.mkdir()
            return fd

        (self.nested / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(store, "_open_parent", drifting):
            code, payload = self.run_cli(
                "workspace", "alias", "disable", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_PARENT_DRIFT)
        self.assertFalse(payload["mutated"])
        self.assertFalse(alias_path(self.nested).exists())

    # --- F2: a post-replace verification failure must not destroy content ---

    def test_readback_failure_restores_the_previous_declaration(self) -> None:
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "ORIGINAL",
        )
        before = alias_path(self.nested).read_text(encoding="utf-8")

        with mock.patch.object(
            store, "_read_with_dirfd",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "disable",
                "--repo", str(self.nested), "--reason", "REPLACEMENT",
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_READBACK_FAILED)
        self.assertFalse(payload["mutated"])
        self.assertEqual(alias_path(self.nested).read_text(encoding="utf-8"), before)
        self.assertNotIn("REPLACEMENT", before)
        # The operator-facing wording must describe the real effective state.
        self.assertIn("unchanged", payload["detail"])

    def test_readback_failure_with_no_previous_declaration_stays_absent(self) -> None:
        (self.nested / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            store, "_read_with_dirfd",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "disable", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertFalse(payload["mutated"])
        self.assertFalse(alias_path(self.nested).exists())

    # --- F3: stat/open failures are typed, and reads cannot block -----------

    def test_stat_permission_error_is_a_typed_refusal_not_an_exception(self) -> None:
        self.run_cli(
            "workspace", "alias", "disable", "--repo", str(self.nested)
        )
        with mock.patch.object(
            store.os, "stat", side_effect=PermissionError(13, "denied")
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "show", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertIn(
            payload["reason"], {REASON_DECLARATION_UNREADABLE, REASON_PARENT_DRIFT}
        )

    def test_regular_to_fifo_swap_does_not_block_the_reader(self) -> None:
        """The lstat says regular; the open must not hang on a swapped-in FIFO."""
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        regular = os.stat_result(
            (stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 10, 0, 0, 0)
        )
        outcome: list = []

        def _read() -> None:
            with mock.patch.object(store, "_lstat_entry", return_value=regular):
                outcome.append(store.read_declaration(self.nested))

        worker = threading.Thread(target=_read, daemon=True)
        worker.start()
        worker.join(10.0)
        self.assertTrue(outcome, "the reader blocked on a FIFO instead of refusing")
        self.assertEqual(outcome[0].reason, REASON_NOT_REGULAR_FILE)

    # --- F4: exact type validation -----------------------------------------

    def test_boolean_schema_version_is_refused(self) -> None:
        self._write_raw(
            {
                "schema_version": True,
                "mode": "alias",
                "canonical_path": str(self.canonical),
                "canonical_workspace_id": CANONICAL_ID,
            }
        )
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_UNSUPPORTED_SCHEMA)

    def test_float_schema_version_is_refused(self) -> None:
        self._write_raw(
            {
                "schema_version": 1.0,
                "mode": "alias",
                "canonical_path": str(self.canonical),
                "canonical_workspace_id": CANONICAL_ID,
            }
        )
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_UNSUPPORTED_SCHEMA)

    def test_non_string_optional_fields_are_refused(self) -> None:
        for field, value in (("reason", False), ("created_at", 0), ("updated_at", 1.5)):
            with self.subTest(field=field):
                self._write_raw(
                    {
                        "schema_version": 1,
                        "mode": "alias",
                        "canonical_path": str(self.canonical),
                        "canonical_workspace_id": CANONICAL_ID,
                        field: value,
                    }
                )
                code, payload = self.run_cli(
                    "workspace", "alias", "show", "--repo", str(self.nested)
                )
                self.assertEqual(code, 1)
                self.assertEqual(payload["reason"], REASON_DECLARATION_INVALID)

    def test_non_string_mode_is_refused(self) -> None:
        self._write_raw({"schema_version": 1, "mode": 1})
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_DECLARATION_INVALID)


class WorkspaceAliasRollbackAndSizeTests(WorkspaceAliasCliTestCase):
    """Review j#102230 Findings 1–2, driven through the public CLI."""

    # --- F1: no mutation may be reported as an unchanged no-op --------------

    def test_unsnapshottable_previous_declaration_is_refused_before_the_replace(
        self,
    ) -> None:
        """A previous entry that cannot be captured must block the write.

        Letting it through leaves a state where a later verification failure has
        nothing to restore: the rollback removes the new entry and the previous
        declaration is gone, reported as if nothing had happened.
        """
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "ORIGINAL",
        )
        before = alias_path(self.nested).read_text(encoding="utf-8")

        with mock.patch.object(
            store, "_read_bytes",
            return_value=store.refused("simulated", "snapshot failed"),
        ), mock.patch.object(
            store, "_read_with_dirfd",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "disable",
                "--repo", str(self.nested), "--reason", "REPLACEMENT",
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_SNAPSHOT_FAILED)
        self.assertFalse(payload["mutated"])
        self.assertEqual(alias_path(self.nested).read_text(encoding="utf-8"), before)

    def test_clear_restores_the_entry_when_removal_cannot_be_confirmed(self) -> None:
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "ORIGINAL",
        )
        before = alias_path(self.nested).read_text(encoding="utf-8")

        with mock.patch.object(
            store, "read_declaration",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "clear", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_REMOVE_FAILED)
        self.assertFalse(payload["mutated"])
        # Restored, so "unchanged" is now a true statement.
        self.assertTrue(alias_path(self.nested).is_file())
        self.assertEqual(alias_path(self.nested).read_text(encoding="utf-8"), before)
        self.assertIn("unchanged", payload["detail"])

    def test_clear_reports_mutation_when_the_entry_cannot_be_restored(self) -> None:
        """An unrestorable removal must be reported as a real mutation."""
        self.run_cli(
            "workspace", "alias", "disable", "--repo", str(self.nested)
        )
        with mock.patch.object(
            store, "read_declaration",
            return_value=store.refused("simulated", "verification failed"),
        ), mock.patch.object(
            store, "_write_temp", side_effect=OSError("no space")
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "clear", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_REMOVE_FAILED)
        self.assertTrue(payload["mutated"])
        self.assertFalse(alias_path(self.nested).exists())
        self.assertIn("could NOT be restored", payload["detail"])

    def test_failed_write_does_not_leave_a_declaration_the_operator_never_set(
        self,
    ) -> None:
        """The previous entry vanishing mid-write must not strand the new one.

        Found by the implementer's own sweep of this surface, not by review: the
        entry existed at the lstat and was gone by the snapshot read, so the
        rollback had a "previous" it could not restore and declined to act —
        leaving the *new* declaration active even though the write had failed.
        The true previous state was "absent", so the rollback must remove it.
        """
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "ORIGINAL",
        )
        path = alias_path(self.nested)
        real_read_bytes = store._read_bytes

        def vanishing(dirfd):
            path.unlink(missing_ok=True)
            return real_read_bytes(dirfd)

        with mock.patch.object(store, "_read_bytes", vanishing), mock.patch.object(
            store, "_read_with_dirfd",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "disable",
                "--repo", str(self.nested), "--reason", "REPLACEMENT",
            )
        self.assertEqual(code, 1)
        self.assertFalse(payload["mutated"])
        self.assertFalse(
            path.exists(),
            "a failed write left a declaration the operator never set",
        )

    # --- F2: a declaration can never force an unbounded allocation ---------

    def test_oversized_declaration_is_a_typed_refusal(self) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * (MAX_DECLARATION_BYTES + 1))
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_TOO_LARGE)

    def test_huge_sparse_declaration_is_refused_without_allocating_it(self) -> None:
        """A sparse file costs no disk but used to cost its full size in RAM.

        The read is bounded before any allocation, so the refusal is reached
        without materializing the declared size.
        """
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(512 * 1024 * 1024)
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_TOO_LARGE)

    def test_oversized_declaration_blocks_the_launch(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
            apply_workspace_alias,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            HerdrSessionStartError,
        )

        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(512 * 1024 * 1024)
        with self.assertRaises(HerdrSessionStartError) as caught:
            apply_workspace_alias(self.nested)
        self.assertIn(REASON_TOO_LARGE, str(caught.exception))

    def test_a_declaration_at_the_size_limit_still_reads(self) -> None:
        """The bound must not reject a legitimate declaration."""
        code, _ = self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
            "--reason", "x" * 1024,
        )
        self.assertEqual(code, 0)
        code, payload = self.run_cli(
            "workspace", "alias", "show", "--repo", str(self.nested)
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "aliased")


class WorkspaceAliasConcurrencyAndDurabilityTests(WorkspaceAliasCliTestCase):
    """Review j#102259 Findings 1–2, driven through the public CLI."""

    @staticmethod
    def _is_dir_fd(fd: int) -> bool:
        try:
            return stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError:  # pragma: no cover - defensive
            return False

    # --- F1: a concurrent declaration must survive a failed rollback -------

    def test_concurrent_declaration_is_not_overwritten_by_a_failed_rollback(
        self,
    ) -> None:
        """Another successful mutation must not be undone by our failure.

        The writer snapshots, a different supported mutation lands a *new*
        inode at the same path, and this writer then fails verification. Its
        rollback must not restore the older bytes over the concurrent winner.
        """
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "OLD",
        )
        path = alias_path(self.nested)
        real_read_bytes = store._read_bytes

        def snapshot_then_concurrent(dirfd):
            data = real_read_bytes(dirfd)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["reason"] = "CONCURRENT"
            temp = path.parent / ".concurrent.tmp"
            temp.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, path)
            return data

        with mock.patch.object(store, "_read_bytes", snapshot_then_concurrent), \
                mock.patch.object(
                    store, "_read_with_dirfd",
                    return_value=store.refused("simulated", "verification failed"),
                ):
            code, payload = self.run_cli(
                "workspace", "alias", "disable",
                "--repo", str(self.nested), "--reason", "NEW",
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_CONCURRENT_CHANGE)
        self.assertFalse(payload["mutated"])
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["reason"],
            "CONCURRENT",
            "a failed write overwrote a concurrently-succeeded declaration",
        )

    def test_supported_mutations_serialize_on_one_lock(self) -> None:
        """A second mutation cannot enter while one holds the lock."""
        entered: list = []
        parent = self.nested / ".mozyo-bridge"
        parent.mkdir(parents=True, exist_ok=True)
        dirfd = store._open_parent(self.nested, create=True)
        try:
            with store._mutation_lock(dirfd):
                def _try_write() -> None:
                    other = store._open_parent(self.nested, create=True)
                    try:
                        # Non-blocking probe of the same lock.
                        fd = os.open(
                            store._LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644,
                            dir_fd=other,
                        )
                        try:
                            import fcntl as _fcntl

                            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                            entered.append("acquired")
                        except OSError:
                            entered.append("blocked")
                        finally:
                            os.close(fd)
                    finally:
                        os.close(other)

                worker = threading.Thread(target=_try_write)
                worker.start()
                worker.join(5.0)
        finally:
            os.close(dirfd)
        self.assertEqual(entered, ["blocked"])

    # --- F2: directory durability is required, not best-effort -------------

    def test_write_refuses_when_the_directory_cannot_be_synced(self) -> None:
        real_fsync = os.fsync

        def failing(fd):
            if self._is_dir_fd(fd):
                raise OSError(5, "EIO")
            return real_fsync(fd)

        with mock.patch.object(store.os, "fsync", failing):
            code, payload = self.run_cli(
                "workspace", "alias", "disable", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_DURABILITY_FAILED)
        # The rollback could not be made durable either, so this is reported as
        # a real mutation needing manual inspection rather than as a no-op.
        self.assertTrue(payload["mutated"])

    def test_clear_syncs_the_parent_directory(self) -> None:
        self.run_cli("workspace", "alias", "disable", "--repo", str(self.nested))
        real_fsync = os.fsync
        seen = {"dir": 0}

        def counting(fd):
            if self._is_dir_fd(fd):
                seen["dir"] += 1
            return real_fsync(fd)

        with mock.patch.object(store.os, "fsync", counting):
            code, _ = self.run_cli(
                "workspace", "alias", "clear", "--repo", str(self.nested)
            )
        self.assertEqual(code, 0)
        self.assertGreaterEqual(seen["dir"], 1, "clear did not sync its directory")

    def test_clear_refuses_when_the_directory_cannot_be_synced(self) -> None:
        self.run_cli("workspace", "alias", "disable", "--repo", str(self.nested))
        real_fsync = os.fsync

        def failing(fd):
            if self._is_dir_fd(fd):
                raise OSError(5, "EIO")
            return real_fsync(fd)

        with mock.patch.object(store.os, "fsync", failing):
            code, payload = self.run_cli(
                "workspace", "alias", "clear", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        # A durability failure keeps the durability reason on every mutation
        # path, so write and clear report the same contract (j#102641 F3).
        self.assertEqual(payload["reason"], REASON_DURABILITY_FAILED)
        self.assertTrue(payload["mutated"])

    def test_rollback_is_also_synced(self) -> None:
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "OLD",
        )
        real_fsync = os.fsync
        seen = {"dir": 0}

        def counting(fd):
            if self._is_dir_fd(fd):
                seen["dir"] += 1
            return real_fsync(fd)

        with mock.patch.object(store.os, "fsync", counting), mock.patch.object(
            store, "_read_with_dirfd",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, _ = self.run_cli(
                "workspace", "alias", "disable",
                "--repo", str(self.nested), "--reason", "NEW",
            )
        self.assertEqual(code, 1)
        self.assertGreaterEqual(
            seen["dir"], 2, "the rollback replace was never made durable"
        )


class WorkspaceAliasIdentityBindingTests(WorkspaceAliasCliTestCase):
    """Review j#102641 Findings 1–2."""

    def test_canonical_identity_swap_between_decision_and_launch_is_refused(
        self,
    ) -> None:
        """The alias is approved for one workspace id; the launch must use it.

        Nothing stops the canonical path's anchor being replaced after the alias
        decision, so the binding is re-checked against the identity actually
        registered at action time.
        """
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
            apply_workspace_alias,
            require_alias_identity,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            HerdrSessionStartError,
        )

        self.run_cli(
            "workspace", "alias", "set",
            "--repo", str(self.nested), "--to", str(self.canonical),
        )
        root, expected = apply_workspace_alias(self.nested)
        self.assertEqual(Path(root), self.canonical)
        self.assertEqual(expected, CANONICAL_ID)

        # The canonical workspace's identity is replaced before actuation.
        _write_anchor(self.canonical, "e" * 32, "mozyo-repo-cccccccc")
        with self.assertRaises(HerdrSessionStartError) as caught:
            require_alias_identity(expected, "e" * 32)
        self.assertIn(CANONICAL_ID, str(caught.exception))
        # The matching case still passes.
        require_alias_identity(expected, CANONICAL_ID)

    def test_undeclared_workspace_carries_no_identity_binding(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
            apply_workspace_alias,
            require_alias_identity,
        )

        root, expected = apply_workspace_alias(self.nested)
        self.assertEqual(Path(root), self.nested)
        self.assertEqual(expected, "")
        require_alias_identity(expected, "anything")  # no binding to enforce

    def test_clear_refuses_a_declaration_that_changed_after_the_snapshot(
        self,
    ) -> None:
        """A concurrent update must not be deleted as if it were the read one."""
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "OLD",
        )
        path = alias_path(self.nested)
        real_read_bytes = store._read_bytes
        fired: list = []

        def once(dirfd):
            data = real_read_bytes(dirfd)
            if not fired:
                fired.append(1)
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["reason"] = "CONCURRENT"
                temp = path.parent / ".concurrent.tmp"
                temp.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                os.replace(temp, path)
            return data

        with mock.patch.object(store, "_read_bytes", once):
            code, payload = self.run_cli(
                "workspace", "alias", "clear", "--repo", str(self.nested)
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_CONCURRENT_CHANGE)
        self.assertTrue(path.exists(), "clear destroyed a concurrent update")
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["reason"], "CONCURRENT"
        )

    def test_same_inode_same_size_update_is_detected(self) -> None:
        """Metadata alone cannot identify a declaration; content must.

        An in-place rewrite of equal length with the mtime restored is invisible
        to a (dev, ino, mtime, size) fence.
        """
        self.run_cli(
            "workspace", "alias", "disable",
            "--repo", str(self.nested), "--reason", "AAAA",
        )
        path = alias_path(self.nested)
        before = os.stat(path)
        real_read_bytes = store._read_bytes
        fired: list = []

        def once(dirfd):
            data = real_read_bytes(dirfd)
            if not fired:
                fired.append(1)
                raw = path.read_bytes().replace(b'"AAAA"', b'"BBBB"')
                fd = os.open(path, os.O_WRONLY)
                os.write(fd, raw)
                os.close(fd)
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return data

        with mock.patch.object(store, "_read_bytes", once), mock.patch.object(
            store, "_read_with_dirfd",
            return_value=store.refused("simulated", "verification failed"),
        ):
            code, payload = self.run_cli(
                "workspace", "alias", "disable",
                "--repo", str(self.nested), "--reason", "NEW",
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], REASON_CONCURRENT_CHANGE)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["reason"],
            "BBBB",
            "a same-inode update was overwritten by a failed rollback",
        )


class WorkspaceAliasLaunchRefusalTests(WorkspaceAliasCliTestCase):
    """A broken declaration must stop the launch, not be ignored by it."""

    def _apply(self, root: Path) -> Path:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start_alias import (  # noqa: E501
            apply_workspace_alias,
        )

        return apply_workspace_alias(root)

    def _error(self):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
            HerdrSessionStartError,
        )

        return HerdrSessionStartError

    def test_directory_declaration_blocks_the_launch(self) -> None:
        alias_path(self.nested).mkdir(parents=True)
        with self.assertRaises(self._error()) as caught:
            self._apply(self.nested)
        self.assertIn(REASON_NOT_REGULAR_FILE, str(caught.exception))

    def test_symlinked_declaration_blocks_the_launch(self) -> None:
        victim = self.base / "victim.txt"
        victim.write_text("{}", encoding="utf-8")
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(victim)
        with self.assertRaises(self._error()) as caught:
            self._apply(self.nested)
        self.assertIn(REASON_NOT_REGULAR_FILE, str(caught.exception))

    def test_unknown_field_declaration_blocks_the_launch(self) -> None:
        path = alias_path(self.nested)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "alias",
                    "canonical_path": str(self.canonical),
                    "canonical_workspace_id": CANONICAL_ID,
                    "future_semantics": "launch-anyway",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(self._error()) as caught:
            self._apply(self.nested)
        self.assertIn(REASON_DECLARATION_INVALID, str(caught.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class WorkspaceAliasProcessConcurrencyTests(WorkspaceAliasCliTestCase):
    """Real concurrent processes racing set / disable / clear (j#102641 条件 5).

    The earlier serialization test only probed the lock helper; this drives the
    public CLI from separate OS processes so the lock, the fence and the
    durability contract are exercised by genuine contention.
    """

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ, MOZYO_BRIDGE_HOME=str(self.base / "home"),
                   PYTHONPATH=str(Path(__file__).resolve().parents[4] / "src"))
        return subprocess.run(
            [sys.executable, "-m", "mozyo_bridge", *argv, "--json"],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_concurrent_mutations_never_corrupt_the_declaration(self) -> None:
        import concurrent.futures

        commands = []
        for _ in range(4):
            commands.append(["workspace", "alias", "disable",
                             "--repo", str(self.nested), "--reason", "D"])
            commands.append(["workspace", "alias", "set", "--repo", str(self.nested),
                             "--to", str(self.canonical), "--reason", "S"])
            commands.append(["workspace", "alias", "clear", "--repo", str(self.nested)])

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(commands)) as pool:
            results = list(pool.map(self._run, commands))

        for result, argv in zip(results, commands):
            self.assertIn(
                result.returncode, (0, 1),
                f"{argv} crashed: rc={result.returncode} stderr={result.stderr[:400]}",
            )
            self.assertNotIn("Traceback", result.stderr, f"{argv} raised: {result.stderr[:400]}")

        # Whatever the interleaving, the surviving state is readable and valid —
        # never a half-written or corrupt declaration.
        final = self._run(["workspace", "alias", "show", "--repo", str(self.nested)])
        self.assertIn(final.returncode, (0, 1))
        payload = json.loads(final.stdout)
        self.assertIn(
            payload["state"],
            {"no_declaration", "aliased", "launch_disabled"},
            f"concurrent mutations left an unreadable state: {payload}",
        )
