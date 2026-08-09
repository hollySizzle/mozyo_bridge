from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSourcesConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.remote_unit_action import (
    ACTION_DELIVERED,
    ACTION_REFUSED,
    REASON_DELIVERY_FAILED,
    REASON_IDENTITY_CHANGED,
    REASON_INVALID_REQUEST,
    REASON_LOCAL_SOURCE,
    REASON_PREVIEW_STALE,
    REASON_UNIT_UNRESOLVED,
    REASON_WORKSPACE_UNRESOLVED,
    RemoteUnitActionRail,
    RemoteUnitActionRequest,
    render_preview,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
    REMOTE_BOARD_ARGS,
    REMOTE_WORKSPACE_ARGS,
    MultiSourceUnitBoardRuntime,
)

from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_multi_source_unit_board import (
    NOW,
    REMOTE_CONFIG,
    WORKSPACE_A,
    WORKSPACE_PAYLOAD,
    FakeLocalRuntime,
    RecordingRunner,
    remote_board_payload,
)


GATEWAY_ARGS = ("project-gateway", "handoff")


class MovableClock:
    """A clock the test advances explicitly, shared by runtime and rail."""

    def __init__(self, moment=NOW) -> None:
        self.moment = moment

    def __call__(self):
        return self.moment


def answers(overrides=None):
    base = {
        REMOTE_BOARD_ARGS: remote_board_payload(),
        REMOTE_WORKSPACE_ARGS: WORKSPACE_PAYLOAD,
        GATEWAY_ARGS: {"result": "sent"},
    }
    base.update(overrides or {})
    return base


def rail(answer_map=None, *, config=REMOTE_CONFIG, clock=None):
    clock = clock if clock is not None else MovableClock()
    runner = RecordingRunner(answer_map if answer_map is not None else answers())
    runtime = MultiSourceUnitBoardRuntime(
        config, local_runtime=FakeLocalRuntime(), runner=runner, clock=clock
    )
    return RemoteUnitActionRail(runtime, clock=clock), runtime, runner


def remote_unit_id(runtime) -> str:
    return next(
        unit.unit_id for unit in runtime.snapshot().units if unit.host_id == "devbox"
    )


def request(unit_id: str, **overrides) -> RemoteUnitActionRequest:
    values = {
        "unit_id": unit_id,
        "issue": "15138",
        "journal": "101633",
        "summary": "board pointer",
        "kind": "design_consultation",
    }
    values.update(overrides)
    return RemoteUnitActionRequest(**values)


class PreviewTests(unittest.TestCase):
    def test_preview_explains_the_route_without_a_connection_value(self) -> None:
        action, runtime, _ = rail()
        unit_id = remote_unit_id(runtime)

        preview = action.preview(request(unit_id))

        self.assertTrue(preview.applicable)
        payload = preview.as_payload()
        self.assertEqual(payload["host_label"], "dev host")
        self.assertEqual(payload["receiver"], "codex")
        self.assertFalse(payload["direct_worker_send"])
        rendered = json.dumps(payload)
        self.assertNotIn("SSH-DESTINATION-SENTINEL", rendered)
        self.assertNotIn("/srv/checkouts", rendered)

    def test_rendered_preview_hides_the_remote_repository_path(self) -> None:
        action, runtime, _ = rail()
        unit_id = remote_unit_id(runtime)

        lines = "\n".join(render_preview(action.preview(request(unit_id))))

        self.assertIn("dev host [ssh]", lines)
        self.assertNotIn("/srv/checkouts", lines)

    def test_local_unit_is_not_routed_through_the_cross_source_rail(self) -> None:
        action, runtime, _ = rail()
        local_unit = next(
            unit.unit_id for unit in runtime.snapshot().units if unit.host_id == "local"
        )

        preview = action.preview(request(local_unit))

        self.assertEqual(preview.state, ACTION_REFUSED)
        self.assertEqual(preview.reason, REASON_LOCAL_SOURCE)

    def test_unresolvable_unit_refuses(self) -> None:
        action, _, _ = rail()

        preview = action.preview(request("unit-absent"))

        self.assertEqual(preview.reason, REASON_UNIT_UNRESOLVED)

    def test_unresolvable_workspace_refuses(self) -> None:
        action, runtime, _ = rail(answers({REMOTE_WORKSPACE_ARGS: {"workspaces": []}}))
        unit_id = remote_unit_id(runtime)

        preview = action.preview(request(unit_id))

        self.assertEqual(preview.reason, REASON_WORKSPACE_UNRESOLVED)

    def test_malformed_requests_refuse_before_any_observation(self) -> None:
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        before = len(runner.argvs)

        for overrides in (
            {"issue": "not-a-number"},
            {"journal": ""},
            {"summary": "   "},
            {"summary": "x" * 5000},
            {"summary": "line\nbreak"},
            {"summary": "see /workspace/project-alpha for context"},
            {"summary": "token=DROP-TOKEN-SENTINEL"},
            {"kind": "close"},
        ):
            with self.subTest(overrides=overrides):
                preview = action.preview(request(unit_id, **overrides))

                self.assertEqual(preview.reason, REASON_INVALID_REQUEST)
        self.assertEqual(len(runner.argvs), before)


    def test_previewed_summary_is_byte_identical_to_the_delivered_one(self) -> None:
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        summary = "pointer to the durable record"

        preview = action.preview(request(unit_id, summary=summary))
        action.apply(preview)

        self.assertEqual(preview.as_payload()["summary"], summary)
        command = next(
            argv[-1] for argv in runner.argvs if "project-gateway" in argv[-1]
        )
        self.assertIn(f"--summary '{summary}'", command)


class ApplyTests(unittest.TestCase):
    def test_apply_delivers_through_the_source_project_gateway(self) -> None:
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))

        result = action.apply(preview)

        self.assertEqual(result.state, ACTION_DELIVERED)
        gateway = [argv for argv in runner.argvs if "project-gateway" in argv[-1]]
        self.assertEqual(len(gateway), 1)
        command = gateway[0][-1]
        self.assertIn("project-gateway handoff", command)
        self.assertIn("--to codex", command)
        self.assertIn("--target-repo /srv/checkouts/mozyo_bridge", command)
        self.assertIn("--target-project mozyo_bridge", command)
        self.assertIn("--issue 15138", command)
        self.assertNotIn("--to claude", command)
        self.assertNotIn("--target %", command)

    def test_apply_requires_an_applicable_preview(self) -> None:
        action, _, runner = rail()
        preview = action.preview(request("unit-absent"))
        before = len(runner.argvs)

        result = action.apply(preview)

        self.assertEqual(result.state, ACTION_REFUSED)
        self.assertEqual(result.reason, REASON_INVALID_REQUEST)
        self.assertEqual(len(runner.argvs), before)

    def test_stale_preview_refuses_without_a_round_trip(self) -> None:
        clock = MovableClock()
        action, runtime, runner = rail(clock=clock)
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        before = len(runner.argvs)
        clock.moment = NOW + timedelta(seconds=600)

        result = action.apply(preview)

        self.assertEqual(result.reason, REASON_PREVIEW_STALE)
        self.assertEqual(len(runner.argvs), before)

    def test_unit_that_moved_between_preview_and_apply_refuses(self) -> None:
        answer_map = answers()
        action, runtime, runner = rail(answer_map)
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        # The same board key now describes a different lane on that host.
        answer_map[REMOTE_BOARD_ARGS] = remote_board_payload(lane_id="issue_15138")

        result = action.apply(preview)

        self.assertEqual(result.reason, REASON_IDENTITY_CHANGED)
        self.assertFalse(
            [argv for argv in runner.argvs if "project-gateway" in argv[-1]]
        )

    def test_repository_identity_change_between_preview_and_apply_refuses(self) -> None:
        answer_map = answers()
        action, runtime, runner = rail(answer_map)
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        answer_map[REMOTE_WORKSPACE_ARGS] = {
            "workspaces": [
                {
                    "workspace_id": WORKSPACE_A,
                    "canonical_path": "/srv/checkouts/other",
                    "project_name": "mozyo_bridge",
                }
            ]
        }

        result = action.apply(preview)

        self.assertEqual(result.reason, REASON_IDENTITY_CHANGED)
        self.assertFalse(
            [argv for argv in runner.argvs if "project-gateway" in argv[-1]]
        )

    def test_source_that_stopped_answering_between_preview_and_apply_refuses(self) -> None:
        answer_map = answers()
        action, runtime, _ = rail(answer_map)
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        answer_map[REMOTE_BOARD_ARGS] = OSError("no route")

        result = action.apply(preview)

        self.assertEqual(result.reason, REASON_UNIT_UNRESOLVED)

    def test_gateway_refusal_is_reported_without_echoing_its_record(self) -> None:
        action, runtime, _ = rail(answers({GATEWAY_ARGS: None}))
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))

        result = action.apply(preview)

        self.assertEqual(result.state, ACTION_REFUSED)
        self.assertEqual(result.reason, REASON_DELIVERY_FAILED)

    def test_gateway_spawn_failure_is_a_typed_refusal(self) -> None:
        action, runtime, _ = rail(
            answers({GATEWAY_ARGS: subprocess.TimeoutExpired(["ssh"], 30)})
        )
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))

        result = action.apply(preview)

        self.assertEqual(result.reason, REASON_DELIVERY_FAILED)

    def test_delivery_detail_reflects_only_the_result_token(self) -> None:
        action, runtime, _ = rail(
            answers(
                {
                    GATEWAY_ARGS: {
                        "result": "sent",
                        "target": "%1075",
                        "repo_root": "/srv/checkouts/mozyo_bridge",
                    }
                }
            )
        )
        unit_id = remote_unit_id(runtime)

        result = action.apply(action.preview(request(unit_id)))

        self.assertIn("result=sent", result.detail)
        self.assertNotIn("%1075", result.detail)
        self.assertNotIn("/srv/checkouts", json.dumps(result.as_payload()))


if __name__ == "__main__":
    unittest.main()
