"""Fake-port / verdict-policy specifications for the doctor workspace-registry
section boundary (#12924).

These exercise the ``doctor_workspace_registry`` verdict policy directly with a
synthetic :class:`WorkspaceRegistryReads` port — without a real home registry,
without a real workspace anchor, and without a real tmux server. They pin the
four-layer section dict shape, the status vocabulary
(``ok``/``error``/``invalid``/``drifted``), the registration / consistency /
runtime branching, and the conditional registry-row read (only loaded when the
health probe says the registry is usable). The end-to-end read-only invariant
over real registry / anchor files stays pinned by the
``test_workspace_registry`` / ``test_rename_compat`` integration tests; this
file pins the verdict policy in isolation.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mozyo_bridge.application.doctor_workspace_registry import (
    LiveWorkspaceRegistryReads,
    WorkspaceRegistryReads,
    WorkspaceRegistrySectionUseCase,
    evaluate_workspace_registry_section,
)
from mozyo_bridge.workspace_registry import (
    REGISTRY_HEALTH_INVALID_SCHEMA,
    REGISTRY_HEALTH_MISSING,
    REGISTRY_HEALTH_OK,
    REGISTRY_HEALTH_UNREADABLE,
)

TARGET = Path("/repo")
HOME = Path("/home/.mozyo_bridge")


@dataclass
class _Record:
    workspace_id: str = "ws-1"
    canonical_path: str = "/repo"
    canonical_session: str = "repo-session"
    display_path: str = "/repo"
    preset: str = "redmine-governed"
    preset_version: str = "2026-06-21"
    last_seen: str = "2026-06-30T00:00:00Z"


def _liveness(**overrides: Any) -> dict[str, Any]:
    """A healthy canonical_path liveness fact by default (#13152)."""
    base = {
        "canonical_path": "/repo",
        "exists": True,
        "is_dir": True,
        "is_git": True,
        "is_main_worktree": True,
    }
    base.update(overrides)
    return base


@dataclass
class _AnchorNames:
    both_exist: bool = False
    using_legacy: bool = False
    new_exists: bool = True


@dataclass
class _Resolved:
    name: str = "repo-session"


@dataclass
class _FakeWorkspaceRegistryReads:
    """Synthetic :class:`WorkspaceRegistryReads`: every read is a fixed return.

    No real registry, no anchor file, no tmux. ``health`` drives the usability
    gate, ``record``/``anchor`` drive registration + consistency, ``anchor_names``
    the name-compat layer, ``resolved`` the canonical session, and
    ``live_sessions`` the runtime layer (``None`` = tmux unavailable).
    """

    health: dict[str, Any]
    record: Any | None = None
    anchor: dict[str, Any] | None = None
    anchor_names: _AnchorNames = field(default_factory=_AnchorNames)
    resolved: _Resolved = field(default_factory=_Resolved)
    live_sessions: set[str] | None = None
    canonical_state: dict[str, Any] | None = None
    loaded: bool = False

    def inspect_registry_health(self, home: Path | None) -> dict[str, Any]:
        return self.health

    def load_workspace_by_path(self, target: Path, home: Path | None) -> Any | None:
        self.loaded = True
        return self.record

    def read_anchor(self, target: Path) -> dict[str, Any] | None:
        return self.anchor

    def anchor_resolution(self, target: Path) -> _AnchorNames:
        return self.anchor_names

    def resolve_canonical_session(self, target: Path, home: Path | None) -> _Resolved:
        return self.resolved

    def anchor_path(self, target: Path) -> Path:
        return target / ".mozyo-bridge/workspace-anchor.json"

    def legacy_anchor_path(self, target: Path) -> Path:
        return target / ".mozyo-bridge/workspace.json"

    def live_session_names(self) -> set[str] | None:
        return self.live_sessions

    def probe_canonical_liveness(self, canonical_path: str | None) -> dict[str, Any]:
        if self.canonical_state is not None:
            return {**self.canonical_state, "canonical_path": canonical_path}
        return _liveness(canonical_path=canonical_path)


def _health(status: str, **extra: Any) -> dict[str, Any]:
    base = {
        "status": status,
        "path": "/home/.mozyo_bridge/registry.sqlite",
        "schema_version": 1,
        "expected_schema_version": 1,
    }
    base.update(extra)
    return base


def _evaluate(reads: WorkspaceRegistryReads) -> dict[str, Any]:
    return evaluate_workspace_registry_section(TARGET, HOME, reads)


class SectionShapeTest(unittest.TestCase):
    def test_section_dict_key_order_is_preserved(self) -> None:
        reads = _FakeWorkspaceRegistryReads(health=_health(REGISTRY_HEALTH_OK))
        result = _evaluate(reads)
        self.assertEqual(
            [
                "status",
                "target",
                "home_registry",
                "registration",
                "anchor",
                "consistency",
                "runtime",
                "identity",
                "next_action",
            ],
            list(result.keys()),
        )
        self.assertEqual(str(TARGET), result["target"])
        self.assertEqual(reads.health, result["home_registry"])

    def test_anchor_info_paths_come_from_the_port(self) -> None:
        reads = _FakeWorkspaceRegistryReads(health=_health(REGISTRY_HEALTH_MISSING))
        anchor = _evaluate(reads)["anchor"]
        self.assertEqual(
            str(TARGET / ".mozyo-bridge/workspace-anchor.json"), anchor["path"]
        )
        self.assertEqual(
            str(TARGET / ".mozyo-bridge/workspace.json"), anchor["legacy_path"]
        )


class RegistrationLayerTest(unittest.TestCase):
    def test_usable_registry_with_row_is_registered(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK), record=_Record()
        )
        registration = _evaluate(reads)["registration"]
        self.assertTrue(registration["registered"])
        self.assertEqual("ws-1", registration["workspace_id"])
        self.assertEqual("redmine-governed", registration["preset"])

    def test_usable_registry_without_row_is_unregistered_not_none(self) -> None:
        reads = _FakeWorkspaceRegistryReads(health=_health(REGISTRY_HEALTH_MISSING))
        registration = _evaluate(reads)["registration"]
        self.assertFalse(registration["registered"])
        self.assertIsNone(registration["workspace_id"])

    def test_unusable_registry_leaves_registration_unknown_and_skips_load(self) -> None:
        # An unreadable registry: the row load must be skipped (registry not
        # trusted) and registration state stays None ("unknown").
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_UNREADABLE), record=_Record()
        )
        registration = _evaluate(reads)["registration"]
        self.assertIsNone(registration["registered"])
        self.assertIsNone(registration["workspace_id"])
        self.assertFalse(reads.loaded)


class ConsistencyLayerTest(unittest.TestCase):
    def test_registry_and_anchor_agree(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(workspace_id="ws-1"),
            anchor={"workspace_id": "ws-1", "canonical_session": "repo-session"},
        )
        self.assertEqual("ok", _evaluate(reads)["consistency"]["status"])

    def test_registry_and_anchor_disagree_is_drift(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(workspace_id="ws-1"),
            anchor={"workspace_id": "ws-2"},
        )
        result = _evaluate(reads)
        self.assertEqual("drift", result["consistency"]["status"])
        self.assertEqual("drifted", result["status"])
        self.assertIn("anchor wins", result["next_action"][0])

    def test_registry_only(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK), record=_Record(), anchor=None
        )
        result = _evaluate(reads)
        self.assertEqual("registry-only", result["consistency"]["status"])
        self.assertEqual("ok", result["status"])
        self.assertTrue(any("anchor is missing" in a for a in result["next_action"]))

    def test_anchor_only(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_MISSING),
            record=None,
            anchor={"workspace_id": "ws-9"},
        )
        result = _evaluate(reads)
        self.assertEqual("anchor-only", result["consistency"]["status"])
        self.assertEqual("ok", result["status"])
        self.assertTrue(
            any("no row for this workspace" in a for a in result["next_action"])
        )

    def test_unregistered(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK), record=None, anchor=None
        )
        result = _evaluate(reads)
        self.assertEqual("unregistered", result["consistency"]["status"])
        self.assertEqual("ok", result["status"])
        self.assertTrue(any("not registered" in a for a in result["next_action"]))

    def test_unusable_registry_consistency_is_unknown(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_UNREADABLE),
            anchor={"workspace_id": "ws-1"},
        )
        self.assertEqual("unknown", _evaluate(reads)["consistency"]["status"])


class OverallStatusTest(unittest.TestCase):
    def test_unreadable_registry_is_error(self) -> None:
        reads = _FakeWorkspaceRegistryReads(health=_health(REGISTRY_HEALTH_UNREADABLE))
        result = _evaluate(reads)
        self.assertEqual("error", result["status"])
        self.assertIn("unreadable", result["next_action"][0])

    def test_invalid_schema_is_invalid(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(
                REGISTRY_HEALTH_INVALID_SCHEMA,
                schema_version=99,
                expected_schema_version=1,
            )
        )
        result = _evaluate(reads)
        self.assertEqual("invalid", result["status"])
        self.assertIn("schema version", result["next_action"][0])

    def test_both_anchor_names_exist_is_drifted(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            anchor_names=_AnchorNames(both_exist=True),
        )
        result = _evaluate(reads)
        self.assertEqual("drifted", result["status"])
        self.assertEqual("both", result["anchor"]["name_state"])
        self.assertIn("no silent merge", result["next_action"][0])

    def test_legacy_anchor_name_stays_ok_with_migration_hint(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(),
            anchor={"workspace_id": "ws-1"},
            anchor_names=_AnchorNames(using_legacy=True, new_exists=False),
        )
        result = _evaluate(reads)
        self.assertEqual("ok", result["status"])
        self.assertEqual("legacy", result["anchor"]["name_state"])
        self.assertTrue(any("legacy name" in a for a in result["next_action"]))


class RuntimeLayerTest(unittest.TestCase):
    def test_session_live_is_active(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(),
            anchor={"workspace_id": "ws-1"},
            resolved=_Resolved(name="repo-session"),
            live_sessions={"repo-session", "other"},
        )
        runtime = _evaluate(reads)["runtime"]
        self.assertTrue(runtime["session_live"])
        self.assertEqual("active", runtime["status"])

    def test_session_not_live_is_stale(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            resolved=_Resolved(name="repo-session"),
            live_sessions={"other"},
        )
        runtime = _evaluate(reads)["runtime"]
        self.assertFalse(runtime["session_live"])
        self.assertEqual("stale", runtime["status"])

    def test_tmux_unavailable_is_unknown(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK), live_sessions=None
        )
        runtime = _evaluate(reads)["runtime"]
        self.assertIsNone(runtime["session_live"])
        self.assertEqual("unknown", runtime["status"])

    def test_last_seen_comes_from_record_else_none(self) -> None:
        with_row = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(last_seen="2026-06-30T00:00:00Z"),
        )
        self.assertEqual(
            "2026-06-30T00:00:00Z", _evaluate(with_row)["runtime"]["last_seen"]
        )
        no_row = _FakeWorkspaceRegistryReads(health=_health(REGISTRY_HEALTH_MISSING))
        self.assertIsNone(_evaluate(no_row)["runtime"]["last_seen"])


class IdentityInvariantTest(unittest.TestCase):
    """Canonical_path identity invariant (#13152)."""

    def test_live_main_worktree_canonical_is_ok(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(canonical_path="/repo"),
            anchor={"workspace_id": "ws-1"},
            canonical_state=_liveness(),
        )
        result = _evaluate(reads)
        self.assertEqual("ok", result["identity"]["status"])
        self.assertEqual("ok", result["status"])

    def test_dead_canonical_path_is_drifted_with_repair_hint(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(canonical_path="/gone"),
            anchor={"workspace_id": "ws-1"},
            canonical_state=_liveness(exists=False, is_dir=False, is_git=None,
                                      is_main_worktree=None),
        )
        result = _evaluate(reads)
        self.assertEqual("missing", result["identity"]["status"])
        self.assertEqual("drifted", result["status"])
        self.assertTrue(
            any("does not exist" in a and "#13152" in a for a in result["next_action"])
        )

    def test_worktree_canonical_path_is_drifted_with_move_hint(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(canonical_path="/worktree"),
            anchor={"workspace_id": "ws-1"},
            canonical_state=_liveness(is_main_worktree=False),
        )
        result = _evaluate(reads)
        self.assertEqual("not-main-worktree", result["identity"]["status"])
        self.assertEqual("drifted", result["status"])
        self.assertTrue(
            any("linked git worktree" in a and "--move" in a
                for a in result["next_action"])
        )

    def test_unregistered_identity_is_unknown_not_checked(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_MISSING), record=None
        )
        result = _evaluate(reads)
        self.assertEqual("unknown", result["identity"]["status"])
        self.assertEqual("ok", result["status"])


class UseCaseAndPortTest(unittest.TestCase):
    def test_live_adapter_satisfies_the_port_protocol(self) -> None:
        self.assertIsInstance(LiveWorkspaceRegistryReads(), WorkspaceRegistryReads)

    def test_use_case_matches_evaluate(self) -> None:
        reads = _FakeWorkspaceRegistryReads(
            health=_health(REGISTRY_HEALTH_OK),
            record=_Record(),
            anchor={"workspace_id": "ws-1"},
        )
        self.assertEqual(
            evaluate_workspace_registry_section(TARGET, HOME, reads),
            WorkspaceRegistrySectionUseCase(reads).execute(TARGET, HOME),
        )


if __name__ == "__main__":
    unittest.main()
