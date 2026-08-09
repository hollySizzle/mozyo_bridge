from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_RESOLVED,
    SOURCE_LIVE,
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
    AgentObservation,
    build_unit_board,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSourcesConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
    REMOTE_BOARD_ARGS,
    REMOTE_WORKSPACE_ARGS,
    MultiSourceUnitBoardRuntime,
)


WORKSPACE_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat(timespec="seconds")

REMOTE_CONFIG = UnitBoardSourcesConfig.from_record(
    {
        "version": 1,
        "sources": [
            {
                "host_id": "devbox",
                "kind": "ssh",
                "ssh_target": "SSH-DESTINATION-SENTINEL",
                "label": "dev host",
            }
        ],
    }
)


def local_snapshot(lane_id: str = "default", observed_at: str = STAMP):
    return build_unit_board(
        (
            AgentObservation(
                workspace_id=WORKSPACE_A,
                lane_id=lane_id,
                provider="codex",
                pane_id="w1:p1",
                runtime_state="idle",
                interactive_ready=True,
                project_label="mozyo_bridge",
                workflow_role="coordinator",
                responsibility="mozyo_bridge",
                work_label="default lane",
                authority_state=AUTHORITY_RESOLVED,
            ),
        ),
        observed_at=observed_at,
    )


class FakeLocalRuntime:
    def __init__(self, snapshot=None, error: BaseException | None = None) -> None:
        self._snapshot = snapshot if snapshot is not None else local_snapshot()
        self._error = error
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._snapshot


def remote_board_payload(unit_id: str = "unit-deadbeef", lane_id: str = "default"):
    return {
        "source_state": SOURCE_LIVE,
        "observed_at": STAMP,
        "unmanaged_agents": 0,
        "detail": "",
        "units": [
            {
                "unit_id": unit_id,
                "workspace_id": WORKSPACE_A,
                "lane_id": lane_id,
                "project_label": "mozyo_bridge",
                "workflow_role": "coordinator",
                "responsibility": "mozyo_bridge",
                "work_label": "default lane",
                "authority_state": AUTHORITY_RESOLVED,
                "identity_state": "resolved",
                "agents": [
                    {
                        "provider": "codex",
                        "runtime_state": "idle",
                        "interactive_ready": True,
                    }
                ],
            }
        ],
    }


WORKSPACE_PAYLOAD = {
    "workspaces": [
        {
            "workspace_id": WORKSPACE_A,
            "canonical_path": "/srv/checkouts/mozyo_bridge",
            "project_name": "mozyo_bridge",
        }
    ]
}


class RecordingRunner:
    """Answer remote invocations by the mozyo-bridge arguments they carry."""

    def __init__(self, answers: dict[tuple[str, ...], object]) -> None:
        self._answers = answers
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        remote_command = argv[-1]
        for args, answer in self._answers.items():
            if all(f" {token}" in f" {remote_command}" for token in args):
                if isinstance(answer, BaseException):
                    raise answer
                if answer is None:
                    return subprocess.CompletedProcess(argv, 1, "", "")
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(answer), ""
                )
        return subprocess.CompletedProcess(argv, 127, "", "")


def runtime(
    answers: dict[tuple[str, ...], object] | None = None,
    *,
    config: UnitBoardSourcesConfig = REMOTE_CONFIG,
    local=None,
    clock=lambda: NOW,
):
    runner = RecordingRunner(
        answers
        if answers is not None
        else {
            REMOTE_BOARD_ARGS: remote_board_payload(),
            REMOTE_WORKSPACE_ARGS: WORKSPACE_PAYLOAD,
        }
    )
    return (
        MultiSourceUnitBoardRuntime(
            config,
            local_runtime=local if local is not None else FakeLocalRuntime(),
            runner=runner,
            clock=clock,
        ),
        runner,
    )


class LocalOnlyTests(unittest.TestCase):
    def test_local_only_snapshot_is_the_unchanged_local_board(self) -> None:
        local = FakeLocalRuntime()
        multi = MultiSourceUnitBoardRuntime(
            UnitBoardSourcesConfig.default(), local_runtime=local
        )

        snapshot = multi.snapshot()

        self.assertEqual(snapshot.as_payload(), local_snapshot().as_payload())
        self.assertNotIn("sources", snapshot.as_payload())


class ObservationTests(unittest.TestCase):
    def test_remote_source_is_asked_for_its_own_public_safe_board(self) -> None:
        multi, runner = runtime()

        snapshot = multi.snapshot()

        self.assertEqual(runner.argvs[0][0], "ssh")
        self.assertIn("herdr unit-board show --json", runner.argvs[0][-1])
        self.assertEqual(len(snapshot.units), 2)
        self.assertEqual(
            {unit.host_id for unit in snapshot.units}, {"local", "devbox"}
        )

    def test_unreachable_remote_stays_visible_and_unactionable(self) -> None:
        multi, _ = runtime({REMOTE_BOARD_ARGS: OSError("no route")})

        snapshot = multi.snapshot()

        self.assertTrue(snapshot.ok)
        states = {status.host_id: status for status in snapshot.sources}
        self.assertEqual(states["devbox"].source_state, SOURCE_UNAVAILABLE)
        self.assertFalse(states["devbox"].actionable)

    def test_remote_timeout_degrades_only_that_source(self) -> None:
        multi, _ = runtime(
            {REMOTE_BOARD_ARGS: subprocess.TimeoutExpired(["ssh"], 30)}
        )

        snapshot = multi.snapshot()

        states = {status.host_id: status for status in snapshot.sources}
        self.assertTrue(states["local"].actionable)
        self.assertFalse(states["devbox"].actionable)

    def test_failing_local_runtime_becomes_a_visible_source_row(self) -> None:
        multi, _ = runtime(local=FakeLocalRuntime(error=RuntimeError("boom")))

        snapshot = multi.snapshot()

        states = {status.host_id: status for status in snapshot.sources}
        self.assertFalse(states["local"].actionable)
        self.assertTrue(states["devbox"].actionable)

    def test_slow_fanout_marks_the_first_observation_stale(self) -> None:
        moments = iter(
            [
                NOW,  # remote observation stamp
                NOW + timedelta(seconds=600),  # freshness evaluation
                NOW + timedelta(seconds=600),  # aggregate stamp
            ]
        )
        multi, _ = runtime(clock=lambda: next(moments))

        snapshot = multi.snapshot()

        states = {status.host_id: status for status in snapshot.sources}
        self.assertEqual(states["devbox"].source_state, SOURCE_STALE)
        self.assertFalse(states["devbox"].actionable)


class UnitResolutionTests(unittest.TestCase):
    def _remote_unit_id(self, multi) -> str:
        return next(
            unit.unit_id for unit in multi.snapshot().units if unit.host_id == "devbox"
        )

    def test_remote_unit_resolves_to_its_own_source_and_remote_key(self) -> None:
        multi, _ = runtime()
        unit_id = self._remote_unit_id(multi)

        target = multi.resolve_unit_target(unit_id)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.source.host_id, "devbox")
        self.assertEqual(target.remote_unit_id, "unit-deadbeef")
        self.assertEqual(target.workspace_id, WORKSPACE_A)

    def test_unknown_unit_key_resolves_to_nothing(self) -> None:
        multi, _ = runtime()

        self.assertIsNone(multi.resolve_unit_target("unit-absent"))
        self.assertIsNone(multi.resolve_unit_target(""))

    def test_unit_on_a_degraded_source_is_not_addressable(self) -> None:
        multi, _ = runtime()
        unit_id = self._remote_unit_id(multi)
        degraded, _ = runtime({REMOTE_BOARD_ARGS: OSError("no route")})

        self.assertIsNone(degraded.resolve_unit_target(unit_id))

    def test_ambiguous_unit_is_not_addressable(self) -> None:
        payload = remote_board_payload()
        payload["units"][0]["identity_state"] = "ambiguous"
        multi, _ = runtime(
            {REMOTE_BOARD_ARGS: payload, REMOTE_WORKSPACE_ARGS: WORKSPACE_PAYLOAD}
        )
        unit_id = next(
            unit.unit_id for unit in multi.snapshot().units if unit.host_id == "devbox"
        )

        self.assertIsNone(multi.resolve_unit_target(unit_id))

    def test_display_shaped_workspace_id_is_never_an_action_input(self) -> None:
        payload = remote_board_payload()
        payload["units"][0]["workspace_id"] = "workspace-a"
        multi, _ = runtime(
            {REMOTE_BOARD_ARGS: payload, REMOTE_WORKSPACE_ARGS: WORKSPACE_PAYLOAD}
        )
        unit_id = next(
            unit.unit_id for unit in multi.snapshot().units if unit.host_id == "devbox"
        )

        self.assertIsNone(multi.resolve_unit_target(unit_id))


class SourceWorkspaceTests(unittest.TestCase):
    def test_workspace_resolves_from_the_source_registry(self) -> None:
        multi, _ = runtime()

        workspace = multi.resolve_source_workspace(
            REMOTE_CONFIG.by_id["devbox"], WORKSPACE_A
        )

        self.assertIsNotNone(workspace)
        assert workspace is not None
        self.assertEqual(workspace.canonical_path, "/srv/checkouts/mozyo_bridge")
        self.assertEqual(workspace.project_name, "mozyo_bridge")

    def test_ambiguous_or_missing_registry_row_fails_closed(self) -> None:
        duplicated = {
            "workspaces": [
                WORKSPACE_PAYLOAD["workspaces"][0],
                WORKSPACE_PAYLOAD["workspaces"][0],
            ]
        }
        for payload in ({"workspaces": []}, duplicated, {"workspaces": "x"}, None):
            with self.subTest(payload=payload):
                multi, _ = runtime(
                    {
                        REMOTE_BOARD_ARGS: remote_board_payload(),
                        REMOTE_WORKSPACE_ARGS: payload,
                    }
                )

                self.assertIsNone(
                    multi.resolve_source_workspace(
                        REMOTE_CONFIG.by_id["devbox"], WORKSPACE_A
                    )
                )

    def test_relative_canonical_path_is_rejected(self) -> None:
        multi, _ = runtime(
            {
                REMOTE_BOARD_ARGS: remote_board_payload(),
                REMOTE_WORKSPACE_ARGS: {
                    "workspaces": [
                        {
                            "workspace_id": WORKSPACE_A,
                            "canonical_path": "relative/path",
                            "project_name": "mozyo_bridge",
                        }
                    ]
                },
            }
        )

        self.assertIsNone(
            multi.resolve_source_workspace(REMOTE_CONFIG.by_id["devbox"], WORKSPACE_A)
        )


if __name__ == "__main__":
    unittest.main()
