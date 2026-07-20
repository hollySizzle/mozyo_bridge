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
    _parse_workspace_list,
    _shared_coordinator_own_target,
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

    def test_direct_construction_rejects_unsupported_version(self) -> None:
        # Redmine #14139 F3 / Design Answer j#83385 Decision 3: a directly-built
        # config must fail closed on an unsupported version exactly like a record,
        # honouring the __post_init__ "no dataclass back door" contract.
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig(version=2)

    def test_direct_construction_rejects_bool_version(self) -> None:
        with self.assertRaises(CoordinatorPlacementError):
            CoordinatorPlacementConfig(version=True)

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

    def test_duplicate_key_fails_closed(self) -> None:
        # Redmine #14139 F3 / Design Answer j#83385 Decision 3: a duplicate key is a
        # conflicting value — rejected, never resolved last-wins by yaml.safe_load.
        home = self._home()
        coordinator_placement_path(home).write_text(
            "mode: per_project_space\nmode: shared_space\n", encoding="utf-8"
        )
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


SHARED = SHARED_COORDINATOR_WORKSPACE_LABEL  # "coordinators"


class SharedCoordinatorTargetTest(unittest.TestCase):
    """`_shared_coordinator_target`: the shared_space default-lane join rules.

    Redmine #14139 review j#83383 F1 / Design Answer j#83385 Decision 1: the shared
    space is identified by its stable LABEL (backend-readable authority), never a
    locator-prefix guess. `workspace_labels` is `{herdr_workspace_id: label}` (None =
    unreadable). A per-project coordinator workspace is NOT the shared space unless it
    carries the shared label.
    """

    def _target(self, rows, ws, adopted, labels):
        return _shared_coordinator_target(rows, ws, adopted, labels, SHARED)

    def test_own_default_slots_pin_the_target(self) -> None:
        # A heal rejoins its own live column — own identity pins it, no label needed
        # (labels=None must NOT fail closed when own pins exist).
        rows = [
            _row("wsA", "claude", "default", "w3:p1"),
            _row("wsA", "codex", "default", "w3:p2"),
        ]
        self.assertEqual(self._target(rows, "wsA", [], None), "w3")

    def test_own_adopted_locator_pins_the_target(self) -> None:
        # This run's adopted (same-project, same-lane) slot pins the target too.
        self.assertEqual(self._target([], "wsA", ["w7:pX"], None), "w7")

    def test_adopts_labelled_shared_space_of_other_project(self) -> None:
        # No own pins: adopt the space another project occupies ONLY because it carries
        # the shared label (crossing the workspace_id boundary, now gated on the label).
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsB", "codex", "default", "w5:p2"),
        ]
        self.assertEqual(self._target(rows, "wsA", [], {"w5": SHARED}), "w5")

    def test_unlabelled_per_project_workspace_is_not_promoted(self) -> None:
        # The R1 defect: a single foreign per-project coordinator pair (unlabelled)
        # must NOT be adopted as the shared space — fail closed, no implicit promotion.
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsB", "codex", "default", "w5:p2"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {"w5": ""})
        # A differently-labelled foreign workspace is likewise not the shared space.
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {"w5": "projectB"})

    def test_idempotent_adopt_is_stable_across_projects(self) -> None:
        # Three projects' coordinators already in the LABELLED shared space w5; a
        # fourth and fifth each resolve to the SAME w5 (idempotent adopt).
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "claude", "default", "w5:p3"),
            _row("wsD", "codex", "default", "w5:p4"),
        ]
        self.assertEqual(self._target(rows, "wsE", [], {"w5": SHARED}), "w5")
        self.assertEqual(self._target(rows, "wsF", [], {"w5": SHARED}), "w5")

    def test_own_pins_win_over_labelled_sibling(self) -> None:
        # This project already has a coordinator slot in w2 while the shared space is
        # w5: the own pin wins (a heal rejoins its own pair).
        rows = [
            _row("wsA", "claude", "default", "w2:p1"),
            _row("wsB", "claude", "default", "w5:p1"),
        ]
        self.assertEqual(self._target(rows, "wsA", [], {"w5": SHARED}), "w2")

    def test_clean_slate_creates(self) -> None:
        # No foreign coordinator pair live at all -> "" -> caller creates the shared
        # space. Only sublane / foreign non-default rows are present.
        rows = [_row("wsB", "claude", "lane-x", "w9:p1")]
        self.assertEqual(self._target(rows, "wsA", [], {}), "")

    def test_unreadable_labels_fail_closed(self) -> None:
        # workspace list unreadable (None) on a join/create decision -> fail closed.
        rows = [_row("wsB", "claude", "default", "w5:p1")]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], None)

    def test_multiple_labelled_candidates_fail_closed(self) -> None:
        # Two live workspaces both carry the shared label -> ambiguous -> fail closed.
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "claude", "default", "w6:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {"w5": SHARED, "w6": SHARED})

    def test_foreign_pairs_without_shared_label_fail_closed(self) -> None:
        # Mode-transition guard: per-project pairs live, none labelled shared -> fail
        # closed rather than joining or silently creating alongside.
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "claude", "default", "w6:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {"w5": "", "w6": "projectC"})

    def test_sublane_slots_never_pin_coordinator_space(self) -> None:
        # Only DEFAULT-lane slots are consulted; a live sublane pair is the untouched
        # #13380/#13411 axis. With no foreign coordinator pair -> clean-slate create.
        rows = [
            _row("wsA", "claude", "lane-x", "w8:p1"),
            _row("wsA", "codex", "lane-x", "w8:p2"),
            _row("wsB", "claude", "lane-y", "w8:p3"),
        ]
        self.assertEqual(self._target(rows, "wsA", [], {"w8": SHARED}), "")

    def test_own_slots_spanning_two_workspaces_fail_closed(self) -> None:
        rows = [
            _row("wsA", "claude", "default", "w2:p1"),
            _row("wsA", "codex", "default", "w3:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {})

    def test_own_adopted_spanning_two_workspaces_fail_closed(self) -> None:
        with self.assertRaises(HerdrSessionStartError):
            self._target([], "wsA", ["w2:pA", "w3:pB"], {})

    def test_malformed_and_locatorless_rows_ignored(self) -> None:
        rows = [
            {"name": "not a mzb1 name", "pane_id": "w9:p9"},
            {"name": encode_assigned_name("wsB", "claude", "default")},  # no locator
            _row("wsB", "codex", "default", "w5:p2"),
        ]
        self.assertEqual(self._target(rows, "wsA", [], {"w5": SHARED}), "w5")

    def test_stable_label_is_constant(self) -> None:
        self.assertEqual(SHARED_COORDINATOR_WORKSPACE_LABEL, "coordinators")

    def test_padded_label_is_not_adopted(self) -> None:
        # Redmine #14139 R4 review j#83473 F1: the label authority is an EXACT match,
        # so a leading/trailing-whitespace-padded label is a DIFFERENT label — never
        # the shared space. Foreign pair present -> mode-transition guard fail-closed.
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsB", "codex", "default", "w5:p2"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {"w5": "  coordinators  "})

    def test_case_variant_label_is_not_adopted(self) -> None:
        rows = [_row("wsB", "claude", "default", "w5:p1")]
        with self.assertRaises(HerdrSessionStartError):
            self._target(rows, "wsA", [], {"w5": "Coordinators"})

    def test_exact_label_is_adopted(self) -> None:
        rows = [_row("wsB", "claude", "default", "w5:p1")]
        self.assertEqual(self._target(rows, "wsA", [], {"w5": "coordinators"}), "w5")


class SharedCoordinatorOwnTargetTest(unittest.TestCase):
    """`_shared_coordinator_own_target`: own-pin resolution WITHOUT a label read.

    Redmine #14139 R4 review j#83473 F2: split out so an own-pin heal can resolve
    before (and instead of) reading the workspace labels — a heal must not depend on
    the `workspace list` command.
    """

    def test_own_row_pins_without_labels(self) -> None:
        rows = [_row("wsA", "claude", "default", "w3:p1")]
        self.assertEqual(_shared_coordinator_own_target(rows, "wsA", []), "w3")

    def test_own_adopted_locator_pins(self) -> None:
        self.assertEqual(_shared_coordinator_own_target([], "wsA", ["w7:pX"]), "w7")

    def test_no_own_pin_is_empty(self) -> None:
        # Only foreign / sublane rows -> no own pin (caller then reads labels).
        rows = [
            _row("wsB", "claude", "default", "w9:p1"),
            _row("wsA", "claude", "lane-x", "w8:p1"),
        ]
        self.assertEqual(_shared_coordinator_own_target(rows, "wsA", []), "")

    def test_own_spanning_two_workspaces_fails_closed(self) -> None:
        rows = [
            _row("wsA", "claude", "default", "w2:p1"),
            _row("wsA", "codex", "default", "w3:p1"),
        ]
        with self.assertRaises(HerdrSessionStartError):
            _shared_coordinator_own_target(rows, "wsA", [])


class SharedCoordinatorDeterministicOrderTest(unittest.TestCase):
    """Redmine #14139 F2 / Design Answer j#83385 Decision 2: deterministic append.

    The resolver's decision must be independent of the order rows appear in the live
    inventory (a stable append order, not an arbitrary live reorder), and it never
    emits any pane move / reorder — it only returns a target workspace.
    """

    def test_decision_independent_of_inventory_row_order(self) -> None:
        base = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "codex", "default", "w5:p2"),
            _row("wsD", "claude", "default", "w5:p3"),
        ]
        labels = {"w5": SHARED}
        forward = _shared_coordinator_target(base, "wsE", [], labels, SHARED)
        reverse = _shared_coordinator_target(
            list(reversed(base)), "wsE", [], labels, SHARED
        )
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, "w5")

    def test_multiple_labelled_ambiguity_is_order_independent(self) -> None:
        # Two labelled candidates fail closed regardless of iteration order (a stable,
        # sorted decision — never "whichever row came first wins").
        rows = [
            _row("wsB", "claude", "default", "w5:p1"),
            _row("wsC", "claude", "default", "w6:p1"),
        ]
        labels = {"w5": SHARED, "w6": SHARED}
        with self.assertRaises(HerdrSessionStartError):
            _shared_coordinator_target(rows, "wsA", [], labels, SHARED)
        with self.assertRaises(HerdrSessionStartError):
            _shared_coordinator_target(list(reversed(rows)), "wsA", [], labels, SHARED)


class ParseWorkspaceListTest(unittest.TestCase):
    """`_parse_workspace_list`: fail-closed herdr `workspace list` label parser."""

    def test_envelope_shape(self) -> None:
        import json

        payload = json.dumps(
            {
                "result": {
                    "type": "workspace_list",
                    "workspaces": [
                        {"workspace_id": "w1", "label": "coordinators"},
                        {"workspace_id": "w2", "label": ""},
                    ],
                }
            }
        )
        self.assertEqual(
            _parse_workspace_list(payload), {"w1": "coordinators", "w2": ""}
        )

    def test_bare_list_and_missing_label(self) -> None:
        import json

        payload = json.dumps([{"workspace_id": "w1"}, {"workspace_id": "w2", "label": 3}])
        # A missing / non-string label is the empty string (present, unlabelled).
        self.assertEqual(_parse_workspace_list(payload), {"w1": "", "w2": ""})

    def test_empty_list_is_readable(self) -> None:
        import json

        self.assertEqual(_parse_workspace_list(json.dumps({"workspaces": []})), {})

    def test_entry_without_workspace_id_skipped(self) -> None:
        import json

        payload = json.dumps({"workspaces": [{"label": "coordinators"}, {"workspace_id": "w1", "label": "x"}]})
        self.assertEqual(_parse_workspace_list(payload), {"w1": "x"})

    def test_unrecognised_payload_is_none(self) -> None:
        self.assertIsNone(_parse_workspace_list("not json"))
        self.assertIsNone(_parse_workspace_list('{"result": {"type": "other"}}'))
        self.assertIsNone(_parse_workspace_list("42"))

    def test_conflicting_label_duplicate_fails_closed_order_independent(self) -> None:
        # Redmine #14139 R2 review j#83425 F1: a repeated workspace_id is an identity
        # conflict — the label authority must not depend on iteration order, so the
        # whole payload is unreadable (None) BOTH ways round, never last-wins.
        import json

        fwd = json.dumps(
            {"workspaces": [
                {"workspace_id": "w5", "label": "coordinators"},
                {"workspace_id": "w5", "label": ""},
            ]}
        )
        rev = json.dumps(
            {"workspaces": [
                {"workspace_id": "w5", "label": ""},
                {"workspace_id": "w5", "label": "coordinators"},
            ]}
        )
        self.assertIsNone(_parse_workspace_list(fwd))
        self.assertIsNone(_parse_workspace_list(rev))

    def test_same_label_duplicate_also_fails_closed(self) -> None:
        # Redmine #14139 R3 review j#83450 F2 / Design Answer j#83433 acceptance 4: a
        # duplicate workspace_id fails closed REGARDLESS of whether the repeated
        # labels agree — the identity conflict is the workspace_id repeating, not the
        # label mismatching. Pins the label-MATCH half so a future "only reject on
        # label mismatch" regression is caught.
        import json

        same = json.dumps(
            {"workspaces": [
                {"workspace_id": "w5", "label": "coordinators"},
                {"workspace_id": "w5", "label": "coordinators"},
            ]}
        )
        self.assertIsNone(_parse_workspace_list(same))

    def test_label_is_kept_verbatim_not_trimmed(self) -> None:
        # Redmine #14139 R4 review j#83473 F1: labels are kept raw (no strip / case
        # fold) so the resolver's EXACT match is not loosened by the parser.
        import json

        payload = json.dumps(
            {"workspaces": [
                {"workspace_id": "w1", "label": "  coordinators  "},
                {"workspace_id": "w2", "label": "Coordinators"},
            ]}
        )
        self.assertEqual(
            _parse_workspace_list(payload),
            {"w1": "  coordinators  ", "w2": "Coordinators"},
        )

    def test_skipped_no_id_entry_does_not_trip_duplicate_detection(self) -> None:
        # An entry without a workspace_id is skipped, so it can never be mistaken for
        # a duplicate of a real id — two distinct ids plus a no-id entry parse fine.
        import json

        payload = json.dumps(
            {"workspaces": [
                {"workspace_id": "w5", "label": "coordinators"},
                {"label": "orphan"},
                {"workspace_id": "w6", "label": ""},
            ]}
        )
        self.assertEqual(
            _parse_workspace_list(payload), {"w5": "coordinators", "w6": ""}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
