"""Unit selector specifications (Redmine #15162 / #15163).

Acceptance: *a missing / ambiguous / foreign Unit selector is refused, typed*.
Each refusal is a distinct token because they tell the caller different things —
``unknown`` means no such Unit, ``mismatch`` means it exists but not as described,
and ``ambiguous`` means narrow the query. Collapsing them would send a caller
looking in the wrong place.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_180_llm_mcp_operation_entry.domain.unit_selector import (  # noqa: E402,E501
    DEFAULT_LANE,
    REQUIRED_SELECTOR_FIELDS,
    SELECTOR_AMBIGUOUS,
    SELECTOR_FOREIGN,
    SELECTOR_MISMATCH,
    SELECTOR_MISSING,
    SELECTOR_REFUSALS,
    SELECTOR_UNKNOWN,
    UnitRecord,
    UnitSelector,
    UnitSelectorError,
    parse_unit_selector,
    resolve_unit,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.grouped_read_model import (  # noqa: E402,E501
    ObservedUnit,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain import (  # noqa: E402,E501
    presentation_grouping,
)

WS = "mzb1-giken-3800"
OTHER_WS = "mzb1-other-repo"
PROJECT = "giken-3800-mozyo-bridge"

MAIN = UnitRecord(
    workspace_id=WS, lane_id=DEFAULT_LANE, project_id=PROJECT, repo_label="mozyo_bridge"
)
LANE_A = UnitRecord(
    workspace_id=WS, lane_id="issue_15151", project_id=PROJECT, repo_label="mozyo_bridge"
)
LANE_A_REMOTE = UnitRecord(
    workspace_id=WS,
    lane_id="issue_15151",
    project_id=PROJECT,
    host_id="build-host",
    repo_label="mozyo_bridge",
)
FOREIGN = UnitRecord(
    workspace_id=OTHER_WS, lane_id="issue_9999", project_id="other-project"
)

INDEX = (MAIN, LANE_A, LANE_A_REMOTE, FOREIGN)


def selector(**overrides) -> UnitSelector:
    values = {"workspace_id": WS, "lane_id": "issue_15151", "project_id": PROJECT}
    values.update(overrides)
    return UnitSelector(**values)


class ParseTests(unittest.TestCase):
    def test_a_complete_selector_parses(self) -> None:
        parsed = parse_unit_selector(
            {"unit": {"workspace_id": WS, "lane_id": "l", "project_id": PROJECT}}
        )
        self.assertEqual(parsed.workspace_id, WS)
        self.assertIsNone(parsed.host_id)

    def test_absent_unit_argument_is_missing(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            parse_unit_selector({})
        self.assertEqual(caught.exception.reason, SELECTOR_MISSING)

    def test_every_absent_required_field_is_named_at_once(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            parse_unit_selector({"unit": {"workspace_id": WS}})
        message = caught.exception.message
        self.assertEqual(caught.exception.reason, SELECTOR_MISSING)
        self.assertIn("lane_id", message)
        self.assertIn("project_id", message)

    def test_project_governance_context_is_required(self) -> None:
        """A workspace+lane pair alone does not identify a Unit."""
        self.assertIn("project_id", REQUIRED_SELECTOR_FIELDS)
        with self.assertRaises(UnitSelectorError) as caught:
            parse_unit_selector({"unit": {"workspace_id": WS, "lane_id": "l"}})
        self.assertEqual(caught.exception.reason, SELECTOR_MISSING)

    def test_whitespace_only_value_counts_as_absent(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            parse_unit_selector(
                {"unit": {"workspace_id": WS, "lane_id": "   ", "project_id": PROJECT}}
            )
        self.assertEqual(caught.exception.reason, SELECTOR_MISSING)

    def test_non_string_values_are_not_coerced_into_an_identity(self) -> None:
        for bad in (True, None, {"a": 1}, ["x"]):
            with self.assertRaises(UnitSelectorError):
                parse_unit_selector(
                    {"unit": {"workspace_id": bad, "lane_id": "l", "project_id": PROJECT}}
                )


class ResolveTests(unittest.TestCase):
    def test_an_exact_selector_resolves(self) -> None:
        resolved = resolve_unit(
            selector(host_id="local"), INDEX, authorized_workspace_ids=[WS]
        )
        self.assertEqual(resolved, LANE_A)

    def test_no_matching_unit_is_unknown(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(
                selector(lane_id="issue_00000"), INDEX, authorized_workspace_ids=[WS]
            )
        self.assertEqual(caught.exception.reason, SELECTOR_UNKNOWN)

    def test_two_matching_units_are_ambiguous_not_guessed(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(selector(), INDEX, authorized_workspace_ids=[WS])
        self.assertEqual(caught.exception.reason, SELECTOR_AMBIGUOUS)
        self.assertEqual(len(caught.exception.candidates), 2)

    def test_a_narrowing_field_resolves_an_ambiguous_selector(self) -> None:
        resolved = resolve_unit(
            selector(host_id="build-host"), INDEX, authorized_workspace_ids=[WS]
        )
        self.assertEqual(resolved, LANE_A_REMOTE)

    def test_a_contradicted_narrowing_field_is_mismatch_not_unknown(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(
                selector(repo_label="some_other_repo"),
                INDEX,
                authorized_workspace_ids=[WS],
            )
        self.assertEqual(caught.exception.reason, SELECTOR_MISMATCH)
        self.assertTrue(caught.exception.candidates)

    def test_a_unit_outside_the_authorized_scope_is_foreign(self) -> None:
        foreign_selector = UnitSelector(
            workspace_id=OTHER_WS, lane_id="issue_9999", project_id="other-project"
        )
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(foreign_selector, INDEX, authorized_workspace_ids=[WS])
        self.assertEqual(caught.exception.reason, SELECTOR_FOREIGN)

    def test_an_unresolved_scope_authorizes_nothing(self) -> None:
        """``None`` scope is a refusal, never a wildcard."""
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(
                selector(host_id="local"), INDEX, authorized_workspace_ids=None
            )
        self.assertEqual(caught.exception.reason, SELECTOR_FOREIGN)

    def test_an_empty_scope_authorizes_nothing(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(selector(host_id="local"), INDEX, authorized_workspace_ids=[])
        self.assertEqual(caught.exception.reason, SELECTOR_FOREIGN)

    def test_refusal_payload_is_structured_and_leaks_no_path(self) -> None:
        with self.assertRaises(UnitSelectorError) as caught:
            resolve_unit(selector(), INDEX, authorized_workspace_ids=[WS])
        payload = caught.exception.as_payload()
        self.assertEqual(payload["error"], "unit_selector")
        self.assertIn(payload["reason"], SELECTOR_REFUSALS)
        for candidate in payload["candidates"]:
            self.assertTrue(candidate.startswith("unit:"))
            self.assertNotIn("/", candidate)


class UnitIdentityTests(unittest.TestCase):
    def test_unit_id_matches_the_cockpit_read_model_key(self) -> None:
        """One Unit has one id across surfaces (``unit-target-model.md``)."""
        record = UnitRecord(workspace_id=WS, lane_id="issue_15151", project_id=PROJECT)
        row = ObservedUnit(workspace_id=WS, lane_id="issue_15151")
        self.assertEqual(record.unit_id(), row.unit_id())
        self.assertEqual(record.unit_id(), f"unit:local:{WS}:issue_15151")

    def test_default_lane_token_matches_the_presentation_layer(self) -> None:
        self.assertEqual(DEFAULT_LANE, presentation_grouping.DEFAULT_LANE)

    def test_payload_carries_no_routing_endpoint(self) -> None:
        """A Unit is not a delivery endpoint; the payload must expose none."""
        payload = LANE_A.as_payload()
        rendered = repr(payload).lower()
        for forbidden in ("pane", "tmux", "session", "worktree_path", "target"):
            self.assertNotIn(forbidden, rendered, forbidden)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
