"""The lock-held v1 replacement path honours the alias rail (#15190, j#102107 F4).

``prepare_actuator_lane_session(admission_lock_held=True)`` deliberately does NOT
call the public :func:`prepare_session` — it already owns the shared attestation
store lock, so it calls :func:`_prepare_session_locked` directly. The first
implementation of the nested-workspace alias rail placed its chokepoint only on
the public entry, so the live replacement driver
(``sublane_actuator_herdr_ops``, which passes ``admission_lock_held=True``)
bypassed it entirely: a ``disabled`` or malformed declaration was not evaluated,
and a verified alias did not fold the root.

These tests drive that exact branch and assert on what reaches
``_prepare_session_locked``:

- a verified alias hands it the **canonical** root, never the nested one;
- a launch-disabled or malformed declaration never lets it be reached at all.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mozyo_bridge.core.state.workspace_registry import ANCHOR_SCHEMA_VERSION
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    MODE_ALIAS,
    MODE_DISABLED,
    REASON_DECLARATION_INVALID,
    REASON_NOT_REGULAR_FILE,
    WorkspaceAliasDeclaration,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure.workspace_alias_store import (  # noqa: E501
    alias_path,
    write_declaration,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
    herdr_session_start_v1_replacement_binding as binding,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    DEFAULT_COORDINATOR_PLACEMENT_MODE,
)

CANONICAL_ID = "ddd145b984ea4bc6ba842f72d4d4161f"
NESTED_ID = "262468a689664fb08615f56b5ef1afe1"


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


class V1ReplacementAliasBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        if not _git_available():  # pragma: no cover - environment guard
            self.skipTest("git is required for workspace alias topology tests")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.canonical = self.base / "repo"
        self.canonical.mkdir()
        subprocess.run(
            ["git", "init", "-q"], cwd=str(self.canonical), check=True,
            capture_output=True, text=True,
        )
        self.nested = self.canonical / "Source" / "rails"
        self.nested.mkdir(parents=True)
        _write_anchor(self.canonical, CANONICAL_ID, "mozyo-repo-aaaaaaaa")
        _write_anchor(self.nested, NESTED_ID, "mozyo-rails-bbbbbbbb")

    def _declare_alias(self) -> None:
        write_declaration(
            self.nested,
            WorkspaceAliasDeclaration(
                mode=MODE_ALIAS,
                canonical_path=str(self.canonical),
                canonical_workspace_id=CANONICAL_ID,
            ),
        )

    def _run_lock_held(self) -> dict:
        """Drive the ``admission_lock_held=True`` branch to the first repo_root use.

        ``_prepare_session_locked`` is deliberately NOT stubbed — it is the entry
        the rail now lives in, so replacing it would delete what these tests
        exist to check. Instead the collaborators *after* the rail are stubbed,
        and the run is stopped at :func:`register_workspace`, the first thing to
        receive the (possibly folded) root. What that call sees is the root the
        launch would actually use.
        """
        seen: dict = {}

        class _Stop(Exception):
            pass

        def _capture_root(root, *args, **kwargs):
            seen["repo_root"] = Path(root)
            raise _Stop

        session = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_session_start."
        )
        with mock.patch(
            "mozyo_bridge.application.repo_local_config_loader."
            "load_repo_local_config",
            return_value=mock.MagicMock(lane_placement=None, agent_launch=None),
        ), mock.patch(
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.coordinator_placement_loader."
            "load_coordinator_placement_for_launch",
            return_value=mock.MagicMock(
                mode=DEFAULT_COORDINATOR_PLACEMENT_MODE, top_workspace_id=""
            ),
        ), mock.patch(
            session + "_resolve_binary_or_die", return_value="herdr"
        ), mock.patch(
            session + "require_herdr_cli_capabilities", return_value=None
        ), mock.patch(
            session + "register_workspace", side_effect=_capture_root
        ):
            try:
                binding.prepare_actuator_lane_session(
                    worktree_path=str(self.nested),
                    config_repo_root=self.canonical,
                    providers=["codex", "claude"],
                    lane_id="default",
                    env={},
                    runner=None,
                    timeout=5.0,
                    replacement_action_id="action-1",
                    admission_lock_held=True,
                )
            except _Stop:
                pass
        return seen

    def test_verified_alias_folds_the_lock_held_launch_to_canonical(self) -> None:
        self._declare_alias()
        seen = self._run_lock_held()
        self.assertEqual(
            Path(seen["repo_root"]),
            self.canonical,
            "the lock-held replacement path must launch at the canonical root",
        )

    def test_undeclared_workspace_is_unchanged_on_the_lock_held_path(self) -> None:
        seen = self._run_lock_held()
        self.assertEqual(Path(seen["repo_root"]), self.nested)

    def _assert_refused_without_side_effect(self, expected: str) -> None:
        """The launch is refused AND nothing downstream of the rail ran.

        ``seen`` staying empty is the zero-side-effect half: ``register_workspace``
        is the first thing after the rail that would touch durable state, so an
        empty capture proves the refusal landed before any of it.
        """
        with self.assertRaises(HerdrSessionStartError) as caught:
            seen = self._run_lock_held()
            self.assertEqual(seen, {})
        self.assertIn(expected, str(caught.exception))

    def test_launch_disabled_never_reaches_the_lock_held_entry(self) -> None:
        write_declaration(
            self.nested, WorkspaceAliasDeclaration(mode=MODE_DISABLED)
        )
        self._assert_refused_without_side_effect("launch-disabled")

    def test_malformed_declaration_never_reaches_the_lock_held_entry(self) -> None:
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
        self._assert_refused_without_side_effect(REASON_DECLARATION_INVALID)

    def test_non_regular_declaration_never_reaches_the_lock_held_entry(self) -> None:
        alias_path(self.nested).mkdir(parents=True)
        self._assert_refused_without_side_effect(REASON_NOT_REGULAR_FILE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
