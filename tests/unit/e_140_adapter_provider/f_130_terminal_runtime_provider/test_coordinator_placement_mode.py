"""Operator-scoped coordinator placement mode tests (Redmine #14139).

Classical, fail-closed coverage of the three pure/IO pieces of the operator
placement knob, with the operator HOME isolated in a temp dir so the real
operator config is never read or written:

- the closed-vocabulary config contract (``CoordinatorPlacementConfig``);
- the home-level loader (missing -> default, malformed / unknown -> fail-closed);
- the shared-coordinators target resolver (``_shared_coordinator_target``):
  own pins, cross-project idempotent adopt, create, multi-project column order,
  and multi-workspace fail-closed — plus the guarantee a sublane slot never pins
  the coordinators space.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.coordinator_placement_loader import (  # noqa: E501
    CoordinatorPlacementLoadError,
    coordinator_placement_path,
    load_coordinator_placement,
    resolve_coordinator_placement_mode,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    SHARED_COORDINATOR_WORKSPACE_LABEL,
    HerdrSessionStartError,
    _shared_coordinator_target,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.coordinator_placement_mode import (  # noqa: E501
    DEFAULT_COORDINATOR_PLACEMENT_MODE,
    PER_PROJECT_SPACE,
    SHARED_SPACE,
    CoordinatorPlacementConfig,
    CoordinatorPlacementError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)


def _row(ws, role, lane, pane):
    return {"name": encode_assigned_name(ws, role, lane), "pane_id": pane}


class CoordinatorPlacementConfigTest(unittest.TestCase):
    def test_default_is_per_project(self) -> None:
        self.assertEqual(CoordinatorPlacementConfig.default().mode, PER_PROJECT_SPACE)
        self.assertEqual(DEFAULT_COORDINATOR_PLACEMENT_MODE, PER_PROJECT_SPACE)

    def test_both_modes_accepted(self) -> None:
        self.assertEqual(
            CoordinatorPlacementConfig.from_record({"mode": PER_PROJECT_SPACE}).mode,
            PER_PROJECT_SPACE,
        )
        self.assertEqual(
            CoordinatorPlacementConfig.from_record({"mode": SHARED_SPACE}).mode,
            SHARED_SPACE,
        )

    def test_empty_record_is_default(self) -> None:
        self.assertEqual(CoordinatorPlacementConfig.from_record(None).mode, PER_PROJECT_SPACE)
        self.assertEqual(CoordinatorPlacementConfig.from_record({}).mode, PER_PROJECT_SPACE)

    def test_unknown_mode_fails_closed(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig.from_record({"mode": "global_grid"})

    def test_direct_construction_validates(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig(mode="nonsense")

    def test_unknown_key_fails_closed(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig.from_record({"mode": SHARED_SPACE, "pane": "w1:p1"})

    def test_non_mapping_fails_closed(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig.from_record(["shared_space"])

    def test_unsupported_version_fails_closed(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig.from_record({"version": 2, "mode": SHARED_SPACE})

    def test_bool_version_fails_closed(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig.from_record({"version": True, "mode": SHARED_SPACE})


class CoordinatorPlacementLoaderTest(unittest.TestCase):
    """The loader always reads an ISOLATED temp home — never the real operator file."""

    def _home(self) -> Path:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return Path(self._tmp.name)

    def test_missing_file_is_default(self) -> None:
        home = self._home()
        self.assertEqual(load_coordinator_placement(home).mode, PER_PROJECT_SPACE)
        self.assertEqual(resolve_coordinator_placement_mode(home), PER_PROJECT_SPACE)

    def test_empty_file_is_default(self) -> None:
        home = self._home()
        coordinator_placement_path(home).write_text("# just a comment\n", encoding="utf-8")
        self.assertEqual(load_coordinator_placement(home).mode, PER_PROJECT_SPACE)

    def test_shared_space_file_is_read(self) -> None:
        home = self._home()
        coordinator_placement_path(home).write_text("mode: shared_space\n", encoding="utf-8")
        self.assertEqual(resolve_coordinator_placement_mode(home), SHARED_SPACE)

    def test_unknown_mode_file_fails_closed(self) -> None:
        home = self._home()
        coordinator_placement_path(home).write_text("mode: everywhere\n", encoding="utf-8")
        with self.assertRaises(CoordinatorPlacementError):
            load_coordinator_placement(home)

    def test_malformed_yaml_fails_closed_as_load_error(self) -> None:
        home = self._home()
        coordinator_placement_path(home).write_text("mode: [unterminated\n", encoding="utf-8")
        with self.assertRaises(CoordinatorPlacementLoadError):
            load_coordinator_placement(home)

    def test_present_but_unreadable_fails_closed(self) -> None:
        # A directory in the file's place is a present-but-unreadable file: it must
        # fail closed (a real broken config), never silently default.
        home = self._home()
        coordinator_placement_path(home).mkdir()
        with self.assertRaises(CoordinatorPlacementLoadError):
            load_coordinator_placement(home)

    def test_load_error_is_subclass_of_domain_error(self) -> None:
        # One `except CoordinatorPlacementError` at the call site catches IO + schema.
        self.assertTrue(issubclass(CoordinatorPlacementLoadError, CoordinatorPlacementError))


class SharedCoordinatorTargetTest(unittest.TestCase):
    """`_shared_coordinator_target`: the shared_space default-lane join rules."""

    def test_own_default_slots_pin_the_target(self) -> None:
        # A heal never splits the coordinator pair across workspaces.
        rows = [
            _row("wsA", "claude", "default", "w3:p1"),
            _row("wsA", "codex", "default", "w3:p2"),
        ]
        self.assertEqual(_shared_coordinator_target(rows, "wsA", []), "w3")

    def test_own_adopted_locator_pins_the_target(self) -> None:
        # This run's adopted (same-project, same-lane) slot pins the target too.
        self.assertEqual(_shared_coordinator_target([], "wsA", ["w7:pX"]), "w7")

    def test_joins_other_projects_shared_space(self) -> None:
        # No own pins: adopt the space ANOTHER project's coordinator occupies —
        # crossing the mozyo workspace_id boundary is the whole point of a SHARED space.
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsB", "codex", "default", "w5:p2"),
        ]
        self.assertEqual(_shared_coordinator_target(rows, "wsA", []), "w5")

    def test_idempotent_adopt_is_stable_across_projects(self) -> None:
        # Three projects' coordinators already in the shared space w5; a fourth and
        # fifth project each resolve to the SAME w5 (idempotent adopt, not a new space).
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "claude", "default", "w5:p3"),
            _row("wsD", "codex", "default", "w5:p4"),
        ]
        self.assertEqual(_shared_coordinator_target(rows, "wsE", []), "w5")
        self.assertEqual(_shared_coordinator_target(rows, "wsF", []), "w5")

    def test_own_pins_win_over_siblings(self) -> None:
        # This project already has a coordinator slot in w2 while siblings sit in w5:
        # the own pin wins (a heal rejoins its own pair), never the sibling space.
        rows = [
            _row("wsA", "claude", "default", "w2:p1"),
            _row("wsB", "claude", "default", "w5:p1"),
        ]
        self.assertEqual(_shared_coordinator_target(rows, "wsA", []), "w2")

    def test_no_pins_creates(self) -> None:
        # Nothing pins the space (only sublane / foreign rows) -> "" -> caller creates.
        rows = [_row("wsB", "claude", "lane-x", "w9:p1")]
        self.assertEqual(_shared_coordinator_target(rows, "wsA", []), "")

    def test_sublane_slots_never_pin_coordinator_space(self) -> None:
        # Only DEFAULT-lane slots are consulted: a live sublane pair (even same
        # project) is the untouched #13380/#13411 axis and never joins the
        # coordinators space.
        rows = [
            _row("wsA", "claude", "lane-x", "w8:p1"),
            _row("wsA", "codex", "lane-x", "w8:p2"),
            _row("wsB", "claude", "lane-y", "w8:p3"),
        ]
        self.assertEqual(_shared_coordinator_target(rows, "wsA", []), "")

    def test_own_slots_spanning_two_workspaces_fail_closed(self) -> None:
        rows = [
            _row("wsA", "claude", "default", "w2:p1"),
            _row("wsA", "codex", "default", "w3:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            _shared_coordinator_target(rows, "wsA", [])

    def test_own_adopted_spanning_two_workspaces_fail_closed(self) -> None:
        with self.assertRaises(HerdrSessionStartError):
            _shared_coordinator_target([], "wsA", ["w2:pA", "w3:pB"])

    def test_sibling_columns_spanning_two_workspaces_fail_closed(self) -> None:
        # Two DIFFERENT herdr workspaces each holding a project's coordinators is an
        # ambiguous shared space: refuse to guess which one is THE shared space.
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "claude", "default", "w6:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            _shared_coordinator_target(rows, "wsA", [])

    def test_malformed_and_locatorless_rows_ignored(self) -> None:
        rows = [
            {"name": "not a mzb1 name", "pane_id": "w9:p9"},
            {"name": encode_assigned_name("wsB", "claude", "default")},  # no locator
            _row("wsB", "codex", "default", "w5:p2"),
        ]
        self.assertEqual(_shared_coordinator_target(rows, "wsA", []), "w5")

    def test_stable_label_is_constant(self) -> None:
        # The shared-space label is a stable constant (cosmetic), not project-derived.
        self.assertEqual(SHARED_COORDINATOR_WORKSPACE_LABEL, "coordinators")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
