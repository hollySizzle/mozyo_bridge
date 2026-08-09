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
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.core.state.workspace_registry import ANCHOR_SCHEMA_VERSION
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    REASON_ALIAS_CYCLE,
    REASON_CROSS_REPOSITORY,
    REASON_DECLARATION_INVALID,
    REASON_DECLARATION_UNREADABLE,
    REASON_NOT_REGULAR_FILE,
    REASON_PARENT_DRIFT,
    REASON_READBACK_FAILED,
    REASON_REMOVE_FAILED,
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
