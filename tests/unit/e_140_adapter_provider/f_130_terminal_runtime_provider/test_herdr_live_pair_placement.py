"""Safety and behavior tests for the identity-bound live pair placement rail."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mozyo_bridge.core.state.herdr_identity_attestation import VERDICT_PRESENT
from mozyo_bridge.core.state.herdr_launch_generation import (
    GENERATION_ATTESTED,
    LaunchGeneration,
)
from mozyo_bridge.core.state.lane_lifecycle import (
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    ProcessGenerationPin,
)
from mozyo_bridge.core.state.workspace_registry import WorkspaceRecord
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_live_pair_placement import (
    _plan_text,
    register_herdr_pair_placement_parser,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_live_pair_placement import (
    APPLY_APPLIED,
    APPLY_FAILED,
    APPLY_PARTIAL,
    APPLY_REFUSED,
    PLAN_MATCHED,
    PLAN_READY,
    REASON_GENERATION_UNVERIFIED,
    REASON_CONFIG_INVALID,
    REASON_LAYOUT_UNAVAILABLE,
    REASON_NOT_DEDICATED_PAIR,
    REASON_PAIR_INVALID,
    REASON_STALE,
    HerdrLivePairPlacement,
    PlacementPlan,
    PlacementTarget,
    _target_for,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_discovery import (
    HerdrCliAgentLister,
)
from tests.support.herdr_pane_tree import Leaf, PaneTreeHerdr, Split


WORKSPACE_ID = "workspace-live-placement"
LANE_ID = "default"
PROVIDERS = ("codex", "claude")


def _swap_leaf_ids(node: object, first: str, second: str) -> None:
    if isinstance(node, Leaf):
        if node.pane_id == first:
            node.pane_id = second
        elif node.pane_id == second:
            node.pane_id = first
        return
    if isinstance(node, Split):
        _swap_leaf_ids(node.first, first, second)
        _swap_leaf_ids(node.second, first, second)


class PairPlacementHerdr(PaneTreeHerdr):
    """Add the one swap command this test surface needs to the shared tree fake."""

    def __init__(self, workspace_id: str = "w1") -> None:
        super().__init__(workspace_id)
        self.swap_refused = False
        self.swap_unchanged = False
        self.swap_malformed = False
        self.third_pane_after_first_move = False
        self.reported_temp_tab = ""
        self.extra_layout_split = False

    def __call__(self, argv, capture_output=None, text=None, timeout=None, env=None, **kwargs):
        tail = list(argv[1:])
        if tail[:2] == ["pane", "layout"] and self.extra_layout_split:
            completed = super().__call__(
                argv,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                env=env,
                **kwargs,
            )
            payload = json.loads(completed.stdout)
            payload["result"]["layout"]["splits"].append(
                {
                    "id": "foreign-split",
                    "direction": "right",
                    "ratio": 0.5,
                    "rect": {"x": 0, "y": 0, "width": 54, "height": 23},
                }
            )
            return subprocess.CompletedProcess(
                completed.args,
                completed.returncode,
                stdout=json.dumps(payload),
                stderr=completed.stderr,
            )
        if tail[:2] == ["pane", "move"]:
            completed = super().__call__(
                argv,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                env=env,
                **kwargs,
            )
            if (
                completed.returncode == 0
                and "--new-tab" in tail
                and self.reported_temp_tab
            ):
                payload = json.loads(completed.stdout)
                payload["result"]["move_result"]["pane"][
                    "tab_id"
                ] = self.reported_temp_tab
                completed = subprocess.CompletedProcess(
                    completed.args,
                    completed.returncode,
                    stdout=json.dumps(payload),
                    stderr=completed.stderr,
                )
            if (
                completed.returncode == 0
                and self.third_pane_after_first_move
                and self._moves == 1
            ):
                moved = tail[2]
                for tab in self.tabs.values():
                    panes = tab.panes()
                    if moved not in panes and len(panes) == 1:
                        self.split_pane(tab, panes[0], "right")
                        break
                self.third_pane_after_first_move = False
            return completed
        if tail[:2] != ["pane", "swap"]:
            return super().__call__(
                argv,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                env=env,
                **kwargs,
            )
        self.calls.append(tail)
        if self.swap_refused:
            return self._failed(argv, "swap refused")
        if self.swap_unchanged:
            return self._done(
                argv,
                {
                    "result": {
                        "type": "pane_swap",
                        "swap": {"changed": False},
                    }
                },
            )
        first = tail[tail.index("--source-pane") + 1]
        second = tail[tail.index("--target-pane") + 1]
        tab = self.tab_of(first)
        if tab is None or tab is not self.tab_of(second):
            return self._failed(argv, "swap target is not one live pair")
        _swap_leaf_ids(tab.root, first, second)
        if self.swap_malformed:
            return self._done(argv, {"result": {"type": "pane_swap"}})
        return self._done(
            argv,
            {
                "result": {
                    "type": "pane_swap",
                    "swap": {"changed": True},
                }
            },
        )


class FakeGenerationStore:
    def __init__(self, rows: dict[str, LaunchGeneration]) -> None:
        self.rows = rows
        self.reads = 0
        self.replace_after_reads = 0
        self.startup_completed = True

    def read(self, assigned_name: str):
        self.reads += 1
        row = self.rows.get(assigned_name)
        if row is None:
            return None
        if self.replace_after_reads and self.reads > self.replace_after_reads:
            return LaunchGeneration(
                assigned_name=row.assigned_name,
                startup_action_id="replacement-generation",
                phase=row.phase,
                workspace_id=row.workspace_id,
                role=row.role,
                lane_id=row.lane_id,
                locator=row.locator,
                verdict=row.verdict,
                observed_at=row.observed_at,
                reserved_at=row.reserved_at,
                attested_at=row.attested_at,
            )
        return row

    def verified(self, _home, **expected):
        row = self.read(expected["assigned_name"])
        if not self.startup_completed or row is None or any((
            row.phase != GENERATION_ATTESTED,
            row.verdict != VERDICT_PRESENT,
            row.workspace_id != expected["workspace_id"],
            row.role != expected["role"],
            row.lane_id != expected["lane_id"],
            row.locator != expected["locator"],
            row.terminal_id != expected["live_terminal_id"],
        )):
            return ""
        return row.startup_action_id


class HerdrLivePairPlacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.record = WorkspaceRecord(
            workspace_id=WORKSPACE_ID,
            canonical_path=str(root),
            display_path=str(root),
            project_name="synthetic-project",
            canonical_session="synthetic-session",
            preset="redmine-governed",
            preset_version="1",
            created_at="now",
            updated_at="now",
            last_seen=None,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(
        self,
        *,
        split: str = "down",
        order: tuple[str, str] = PROVIDERS,
        ratio: float = 0.5,
        third_pane: bool = False,
    ):
        herdr = PairPlacementHerdr("w1")
        root = Path(self.record.canonical_path)
        herdr.cwd_by_workspace[WORKSPACE_ID] = str(root)
        tab = herdr.new_tab()
        first_name = encode_assigned_name(WORKSPACE_ID, order[0], LANE_ID)
        second_name = encode_assigned_name(WORKSPACE_ID, order[1], LANE_ID)
        first_pane = herdr.seed_pane(tab, first_name)
        second_pane = herdr.split_pane(tab, first_pane, split, second_name)
        if isinstance(tab.root, Split):
            tab.root.ratio = ratio
        if third_pane:
            herdr.split_pane(tab, second_pane, "right")

        pane_by_provider = {order[0]: first_pane, order[1]: second_pane}
        rows = {
            encode_assigned_name(WORKSPACE_ID, provider, LANE_ID): LaunchGeneration(
                assigned_name=encode_assigned_name(WORKSPACE_ID, provider, LANE_ID),
                startup_action_id=f"generation-{provider}",
                phase=GENERATION_ATTESTED,
                workspace_id=WORKSPACE_ID,
                role=provider,
                lane_id=LANE_ID,
                locator=pane_by_provider[provider],
                terminal_id=f"terminal:{pane_by_provider[provider]}",
                verdict=VERDICT_PRESENT,
                observed_at="now",
                reserved_at="now",
                attested_at="now",
            )
            for provider in PROVIDERS
        }
        generations = FakeGenerationStore(rows)
        service = HerdrLivePairPlacement(
            "herdr",
            runner=herdr,
            lister=HerdrCliAgentLister("herdr", runner=herdr),
            generation_store=generations,
            generation_verifier=generations.verified,
            workspace_loader=lambda workspace_id: (
                self.record if workspace_id == WORKSPACE_ID else None
            ),
            workspace_resolver=lambda cwd: (
                WORKSPACE_ID if cwd == str(root) else ""
            ),
        )
        return service, herdr, generations, pane_by_provider

    @staticmethod
    def _mutations(herdr: PairPlacementHerdr) -> list[list[str]]:
        return [
            call
            for call in herdr.calls
            if call[:2] in (["pane", "move"], ["pane", "swap"], ["pane", "resize"])
        ]

    def test_preview_reports_matched_without_exposing_locator_or_generation(self) -> None:
        service, herdr, _, panes = self._build()
        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.status, PLAN_MATCHED)
        payload = json.dumps(plan.as_payload(), sort_keys=True)
        for private_runtime_value in (*panes.values(), "generation-codex", "generation-claude"):
            self.assertNotIn(private_runtime_value, payload)
        self.assertEqual(self._mutations(herdr), [])

    def test_explicit_empty_lane_refuses_preview_and_apply_before_herdr_io(self) -> None:
        service, herdr, _, _ = self._build(split="right")

        for lane_id in ("", " ", "\t"):
            with self.subTest(lane_id=repr(lane_id)):
                herdr.calls.clear()
                preview = service.preview(WORKSPACE_ID, lane_id)
                result = service.apply(WORKSPACE_ID, lane_id)

                self.assertEqual(preview.reason, REASON_CONFIG_INVALID)
                self.assertEqual(result.status, APPLY_REFUSED)
                self.assertEqual(result.reason, REASON_CONFIG_INVALID)
                self.assertEqual(herdr.calls, [])

    def test_preview_refuses_a_tab_with_a_foreign_third_pane_without_mutation(self) -> None:
        service, herdr, _, _ = self._build(third_pane=True)

        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.reason, REASON_NOT_DEDICATED_PAIR)
        self.assertEqual(self._mutations(herdr), [])

    def test_apply_refuses_when_generation_changes_between_observations(self) -> None:
        service, herdr, generations, _ = self._build(split="right")
        generations.replace_after_reads = 2

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_REFUSED)
        self.assertEqual(result.reason, REASON_STALE)
        self.assertEqual(self._mutations(herdr), [])

    def test_apply_changes_split_with_two_typed_moves_and_remeasures(self) -> None:
        service, herdr, _, _ = self._build(split="right")
        self.assertEqual(service.preview(WORKSPACE_ID).status, PLAN_READY)
        herdr.calls.clear()

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_APPLIED, result)
        self.assertEqual(result.after.status, PLAN_MATCHED)
        moves = [call for call in self._mutations(herdr) if call[:2] == ["pane", "move"]]
        self.assertEqual(len(moves), 2)
        self.assertTrue(all("--no-focus" in call for call in moves))

    def test_apply_swaps_provider_order_and_remeasures(self) -> None:
        service, herdr, _, _ = self._build(order=("claude", "codex"))

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_APPLIED, result)
        self.assertEqual(result.after.status, PLAN_MATCHED)
        swaps = [call for call in self._mutations(herdr) if call[:2] == ["pane", "swap"]]
        self.assertEqual(len(swaps), 1)

    def test_swap_command_failure_is_conservatively_partial(self) -> None:
        service, herdr, _, _ = self._build(order=("claude", "codex"))
        herdr.swap_refused = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        self.assertFalse(result.as_payload()["retryable"])

    def test_swap_changed_false_is_known_failed_without_mutation(self) -> None:
        service, herdr, _, _ = self._build(order=("claude", "codex"))
        herdr.swap_unchanged = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_FAILED)
        self.assertTrue(result.as_payload()["retryable"])

    def test_swap_malformed_effect_is_conservatively_partial(self) -> None:
        service, herdr, _, _ = self._build(order=("claude", "codex"))
        herdr.swap_malformed = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        self.assertFalse(result.as_payload()["retryable"])

    def test_apply_resizes_ratio_from_the_second_pane_and_remeasures(self) -> None:
        service, herdr, _, panes = self._build(ratio=0.7)

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_APPLIED, result)
        self.assertEqual(result.after.status, PLAN_MATCHED)
        self.assertEqual(1, len(herdr.resizes))
        resize = herdr.resizes[0]
        self.assertEqual(panes["claude"], resize[resize.index("--pane") + 1])
        self.assertEqual("up", resize[resize.index("--direction") + 1])

    def test_resize_changed_false_is_known_failed_without_mutation(self) -> None:
        service, herdr, _, _ = self._build(ratio=0.7)
        herdr.resize_unchanged = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_FAILED)
        self.assertTrue(result.as_payload()["retryable"])

    def test_resize_malformed_effect_is_conservatively_partial(self) -> None:
        service, herdr, _, _ = self._build(ratio=0.7)
        herdr.resize_malformed = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        self.assertFalse(result.as_payload()["retryable"])

    def test_zero_exit_without_changed_move_stops_before_second_move(self) -> None:
        service, herdr, _, panes = self._build(split="right")
        herdr.move_unchanged.add(panes["claude"])

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_FAILED)
        moves = [call for call in self._mutations(herdr) if call[:2] == ["pane", "move"]]
        self.assertEqual(len(moves), 1)

    def test_second_move_failure_reports_partial_state_and_safe_recovery(self) -> None:
        service, herdr, _, panes = self._build(split="right")
        herdr.refuse_from_move = 2

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        self.assertTrue(result.recovery)
        payload = json.dumps(result.as_payload(), sort_keys=True)
        self.assertNotIn(panes["codex"], payload)
        self.assertNotIn(panes["claude"], payload)

    def test_foreign_pane_appearing_while_detached_stops_before_return_move(self) -> None:
        service, herdr, _, _ = self._build(split="right")
        herdr.third_pane_after_first_move = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        moves = [call for call in self._mutations(herdr) if call[:2] == ["pane", "move"]]
        self.assertEqual(len(moves), 1)

    def test_invalid_reported_temporary_tab_stops_before_return_move(self) -> None:
        service, herdr, _, _ = self._build(split="right")
        herdr.reported_temp_tab = "--synthetic-option"

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        moves = [call for call in self._mutations(herdr) if call[:2] == ["pane", "move"]]
        self.assertEqual(len(moves), 1)

    def test_generation_change_after_return_move_is_partial(self) -> None:
        service, herdr, generations, _ = self._build(split="right")
        generations.replace_after_reads = 6

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        moves = [call for call in self._mutations(herdr) if call[:2] == ["pane", "move"]]
        self.assertEqual(len(moves), 2)

    def test_resize_then_unreadable_layout_is_partial_not_retryable(self) -> None:
        service, herdr, _, _ = self._build(ratio=0.7)
        herdr.layout_unreadable_after_resize = True

        result = service.apply(WORKSPACE_ID)

        self.assertEqual(result.status, APPLY_PARTIAL)
        self.assertTrue(herdr.resizes)
        self.assertFalse(result.as_payload()["retryable"])

    def test_unverified_generation_refuses_before_layout_or_mutation(self) -> None:
        service, herdr, generations, _ = self._build(split="right")
        name = encode_assigned_name(WORKSPACE_ID, "codex", LANE_ID)
        row = generations.rows[name]
        generations.rows[name] = LaunchGeneration(
            **{**row.as_payload(), "verdict": "missing"}
        )

        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.reason, REASON_GENERATION_UNVERIFIED)
        self.assertEqual(self._mutations(herdr), [])
        self.assertFalse(any(call[:2] == ["pane", "layout"] for call in herdr.calls))

    def test_unsettled_startup_transaction_refuses_before_mutation(self) -> None:
        service, herdr, generations, _ = self._build(split="right")
        generations.startup_completed = False
        plan = service.preview(WORKSPACE_ID)
        self.assertEqual(plan.reason, REASON_GENERATION_UNVERIFIED)
        self.assertEqual(self._mutations(herdr), [])

    def test_provider_mismatch_and_shell_residue_refuse_before_mutation(self) -> None:
        for mode in ("provider", "stale"):
            with self.subTest(mode=mode):
                service, herdr, _, panes = self._build(split="right")
                if mode == "provider":
                    herdr.detected_override[panes["codex"]] = "claude"
                else:
                    herdr.stale_panes.add(panes["codex"])

                plan = service.preview(WORKSPACE_ID)

                self.assertEqual(plan.reason, REASON_PAIR_INVALID)
                self.assertEqual(self._mutations(herdr), [])

    def test_malformed_inventory_row_refuses_before_layout(self) -> None:
        service, herdr, _, _ = self._build(split="right")
        herdr.extra_rows.append("not-a-mapping")

        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.reason, REASON_PAIR_INVALID)
        self.assertFalse(any(call[:2] == ["pane", "layout"] for call in herdr.calls))

    def test_foreign_inventory_row_cannot_duplicate_target_locator(self) -> None:
        service, herdr, _, panes = self._build(split="right")
        herdr.extra_rows.append(
            {
                "name": "foreign-agent",
                "pane_id": panes["codex"],
                "agent": "codex",
                "cwd": self.record.canonical_path,
            }
        )

        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.reason, REASON_PAIR_INVALID)
        self.assertEqual(self._mutations(herdr), [])

    def test_extra_layout_split_refuses_before_mutation(self) -> None:
        service, herdr, _, _ = self._build(split="right")
        herdr.extra_layout_split = True

        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.reason, "geometry_unsupported")
        self.assertEqual(self._mutations(herdr), [])

    def test_invalid_tab_locator_refuses_before_mutation(self) -> None:
        service, herdr, _, _ = self._build(split="right")
        next(iter(herdr.tabs.values())).tab_id = "--synthetic-option"

        plan = service.preview(WORKSPACE_ID)

        self.assertEqual(plan.reason, REASON_LAYOUT_UNAVAILABLE)
        self.assertEqual(self._mutations(herdr), [])

    def test_public_payload_and_text_remove_controls_and_credentials(self) -> None:
        plan = PlacementPlan(
            status="refused",
            reason="pair_invalid",
            detail="fixed detail",
            workspace_id="workspace\nforged",
            lane_id="token=synthetic-material-123456",
        )

        payload = json.dumps(plan.as_payload(), ensure_ascii=False)
        rendered = _plan_text(plan)

        for public in (payload, rendered):
            self.assertNotIn("workspace\nforged", public)
            self.assertNotIn("synthetic-material-123456", public)
            self.assertIn("[redacted]", public)

    def test_sublane_default_order_comes_from_declared_pair_not_current_binding(self) -> None:
        gateway = ProcessGenerationPin(
            role="gateway",
            provider="claude",
            assigned_name=encode_assigned_name(WORKSPACE_ID, "claude", "issue_14608"),
            locator="w1:p1",
        )
        worker = ProcessGenerationPin(
            role="worker",
            provider="codex",
            assigned_name=encode_assigned_name(WORKSPACE_ID, "codex", "issue_14608"),
            locator="w1:p2",
        )
        lifecycle = SimpleNamespace(
            lane_disposition=DISPOSITION_ACTIVE,
            lane_kind="implementation",
            declared_pins=(gateway, worker),
        )
        reader_path = (
            "mozyo_bridge.e_140_adapter_provider."
            "f_130_terminal_runtime_provider.application."
            "herdr_live_pair_placement.LaneLifecycleReader"
        )

        with patch(reader_path) as reader:
            reader.return_value.get.return_value = lifecycle
            target = _target_for(self.record, "issue_14608")

        self.assertEqual(target.order, ("claude", "codex"))
        self.assertEqual(target.declared_pins, (gateway, worker))

    def test_sublane_hibernated_lifecycle_is_not_a_placement_target(self) -> None:
        lifecycle = SimpleNamespace(
            lane_disposition=DISPOSITION_HIBERNATED,
            lane_kind="implementation",
            declared_pins=(),
        )
        reader_path = (
            "mozyo_bridge.e_140_adapter_provider."
            "f_130_terminal_runtime_provider.application."
            "herdr_live_pair_placement.LaneLifecycleReader"
        )

        with patch(reader_path) as reader:
            reader.return_value.get.return_value = lifecycle
            with self.assertRaises(ValueError):
                _target_for(self.record, "issue_14608")

    def test_declared_generation_mismatch_refuses_before_mutation(self) -> None:
        service, herdr, _, panes = self._build(split="right")
        rows = tuple(herdr._rows())
        gateway = ProcessGenerationPin(
            role="gateway",
            provider="codex",
            assigned_name=encode_assigned_name(WORKSPACE_ID, "codex", LANE_ID),
            locator=panes["codex"],
        )
        worker = ProcessGenerationPin(
            role="worker",
            provider="claude",
            assigned_name=encode_assigned_name(WORKSPACE_ID, "claude", LANE_ID),
            locator=panes["claude"],
        )
        mismatches = (
            replace(gateway, assigned_name=encode_assigned_name(WORKSPACE_ID, "codex", "foreign")),
            replace(gateway, locator="w1:p999"),
            replace(gateway, provider="other"),
        )
        for mismatched in mismatches:
            with self.subTest(pin=mismatched.match_key):
                target = PlacementTarget(
                    "down", PROVIDERS, 0.5, (mismatched, worker)
                )
                slots, reason, _ = service._resolve_slots(
                    workspace_id=WORKSPACE_ID,
                    lane_id=LANE_ID,
                    target=target,
                    rows=rows,
                )
                self.assertIsNone(slots)
                self.assertEqual(reason, REASON_PAIR_INVALID)
        revision_rows = tuple(
            {**row, "runtime_revision": "live-r2"}
            if row.get("name") == gateway.assigned_name
            else row
            for row in rows
        )
        target = PlacementTarget(
            "down",
            PROVIDERS,
            0.5,
            (replace(gateway, runtime_revision="declared-r1"), worker),
        )
        slots, reason, _ = service._resolve_slots(
            workspace_id=WORKSPACE_ID,
            lane_id=LANE_ID,
            target=target,
            rows=revision_rows,
        )
        self.assertIsNone(slots)
        self.assertEqual(reason, REASON_PAIR_INVALID)
        self.assertEqual(self._mutations(herdr), [])

    def test_cli_surface_has_no_pane_id_input(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)

        def add_repo_option(command) -> None:
            command.add_argument("--repo", default=None)

        register_herdr_pair_placement_parser(sub, add_repo_option=add_repo_option)
        help_text = parser.format_help()
        pair_parser = next(
            action.choices["pair-placement"]
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        preview_help = pair_parser._subparsers._group_actions[0].choices[
            "preview"
        ].format_help()

        self.assertNotIn("--pane", help_text + preview_help)

    def test_cli_explicit_empty_lane_never_targets_the_default_pair(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)

        def add_repo_option(command) -> None:
            command.add_argument("--repo", default=None)

        register_herdr_pair_placement_parser(sub, add_repo_option=add_repo_option)
        service, herdr, _, _ = self._build(split="right")

        for command in ("preview", "apply"):
            for lane_id in ("", "   "):
                with self.subTest(command=command, lane_id=repr(lane_id)):
                    args = parser.parse_args(
                        ["pair-placement", command, "--lane", lane_id]
                    )
                    herdr.calls.clear()
                    with patch(
                        "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_live_pair_placement._workspace_id",
                        return_value=WORKSPACE_ID,
                    ), patch(
                        "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_live_pair_placement.production_live_pair_placement",
                        return_value=service,
                    ), patch("builtins.print"):
                        result = args.func(args)

                    self.assertEqual(result, 1)
                    self.assertEqual(herdr.calls, [])


if __name__ == "__main__":
    unittest.main()
