from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_RESOLVED,
    SOURCE_LIVE,
    SOURCE_RELOAD_REQUIRED,
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
    AgentObservation,
    build_unit_board,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSourcesConfig,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
    MAX_REMOTE_PATH_LENGTH,
    MAX_SOURCE_OUTPUT_BYTES,
    UntrustedJsonError,
    bounded_capture_run,
    loads_untrusted_json,
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
                # A ``str`` answer is already-rendered stdout, so a test can
                # hand over the shape a CLI really prints (a human-readable
                # record, a blank line, then the JSON outcome last).  A mapping
                # is the JSON-only shape.
                stdout = answer if isinstance(answer, str) else json.dumps(answer)
                return subprocess.CompletedProcess(argv, 0, stdout, "")
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
    def test_remote_source_is_asked_for_its_own_single_server_board(self) -> None:
        multi, runner = runtime()

        snapshot = multi.snapshot()

        self.assertEqual(runner.argvs[0][0], "ssh")
        self.assertIn("herdr unit-board show --json", runner.argvs[0][-1])
        # Without this the far host answers with ITS merged board.
        self.assertIn("--local-only", runner.argvs[0][-1])
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

    def test_unreadable_answer_is_reload_required_not_unreachable(self) -> None:
        # "did not answer" and "answered something unreadable" are different
        # source states; collapsing them mislabels a schema break as a
        # connection failure.
        def unreadable(argv, **kw):
            if "unit-board show" in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, "{not json", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps(WORKSPACE_PAYLOAD), "")

        multi = MultiSourceUnitBoardRuntime(
            REMOTE_CONFIG,
            local_runtime=FakeLocalRuntime(),
            runner=unreadable,
            clock=lambda: NOW,
        )

        states = {s.host_id: s for s in multi.snapshot().sources}
        self.assertEqual(states["devbox"].source_state, SOURCE_RELOAD_REQUIRED)
        self.assertFalse(states["devbox"].actionable)

    def test_non_zero_exit_stays_unreachable(self) -> None:
        def refused(argv, **kw):
            if "unit-board show" in argv[-1]:
                return subprocess.CompletedProcess(argv, 255, "", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps(WORKSPACE_PAYLOAD), "")

        multi = MultiSourceUnitBoardRuntime(
            REMOTE_CONFIG,
            local_runtime=FakeLocalRuntime(),
            runner=refused,
            clock=lambda: NOW,
        )

        states = {s.host_id: s for s in multi.snapshot().sources}
        self.assertEqual(states["devbox"].source_state, SOURCE_UNAVAILABLE)

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


class UntrustedJsonTests(unittest.TestCase):
    def test_a_duplicate_key_is_refused_rather_than_last_wins(self) -> None:
        # json.loads keeps the last value, so a source could put a rejected
        # value first and a canonical one second and have every later check see
        # only the second.
        with self.assertRaises(UntrustedJsonError):
            loads_untrusted_json('{"workspace_id": "bad", "workspace_id": "good"}')

    def test_a_duplicate_key_nested_in_an_object_is_refused(self) -> None:
        with self.assertRaises(UntrustedJsonError):
            loads_untrusted_json('{"a": {"b": 1, "b": 2}}')

    def test_a_duplicate_key_inside_an_array_element_is_refused(self) -> None:
        with self.assertRaises(UntrustedJsonError):
            loads_untrusted_json('{"units": [{"x": 1, "x": 2}]}')

    def test_ordinary_documents_still_decode(self) -> None:
        self.assertEqual(
            loads_untrusted_json('{"a": 1, "b": {"c": [1, 2]}}'),
            {"a": 1, "b": {"c": [1, 2]}},
        )

    def test_a_board_with_a_duplicated_identity_key_is_unreadable(self) -> None:
        duplicated = (
            '{"source_state":"live","observed_at":"' + STAMP + '",'
            '"unmanaged_agents":0,"detail":"","units":[{"unit_id":"unit-x",'
            '"workspace_id":"NOT-CANONICAL","workspace_id":"' + WORKSPACE_A + '",'
            '"lane_id":"default","project_label":"p","workflow_role":"c",'
            '"responsibility":"p","work_label":"w","authority_state":"resolved",'
            '"identity_state":"resolved","agents":[{"provider":"codex",'
            '"runtime_state":"idle","interactive_ready":true}]}]}'
        )

        def answering(argv, **kwargs):
            if "unit-board show" in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, duplicated, "")
            return subprocess.CompletedProcess(argv, 0, json.dumps(WORKSPACE_PAYLOAD), "")

        multi = MultiSourceUnitBoardRuntime(
            REMOTE_CONFIG, local_runtime=FakeLocalRuntime(), runner=answering,
            clock=lambda: NOW,
        )

        states = {s.host_id: s for s in multi.snapshot().sources}
        self.assertEqual(states["devbox"].source_state, SOURCE_RELOAD_REQUIRED)


class OutputBoundTests(unittest.TestCase):
    def test_output_over_the_ceiling_is_a_typed_failure(self) -> None:
        def oversized(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, "y" * (MAX_SOURCE_OUTPUT_BYTES + 1), ""
            )

        multi = MultiSourceUnitBoardRuntime(REMOTE_CONFIG, runner=oversized)

        self.assertIsNone(
            multi.run_source_command(REMOTE_CONFIG.by_id["devbox"], ("workspace", "list"))
        )

    def test_output_exactly_at_the_ceiling_is_accepted(self) -> None:
        def at_limit(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, "y" * MAX_SOURCE_OUTPUT_BYTES, ""
            )

        multi = MultiSourceUnitBoardRuntime(REMOTE_CONFIG, runner=at_limit)
        completed = multi.run_source_command(
            REMOTE_CONFIG.by_id["devbox"], ("workspace", "list")
        )

        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(len(completed.stdout), MAX_SOURCE_OUTPUT_BYTES)

    def test_the_production_runner_stops_reading_at_the_ceiling(self) -> None:
        # The bound has to live where the reading happens: capturing everything
        # first and measuring afterwards does not stop the allocation.
        import sys

        completed = bounded_capture_run(
            [sys.executable, "-c",
             f"import sys; sys.stdout.write('z' * {MAX_SOURCE_OUTPUT_BYTES * 2})"],
            timeout=60,
        )

        self.assertLessEqual(len(completed.stdout), MAX_SOURCE_OUTPUT_BYTES + 1)

    def test_the_production_runner_passes_ordinary_output_through(self) -> None:
        import sys

        completed = bounded_capture_run(
            [sys.executable, "-c", "print('hello')"], timeout=60
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "hello")


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

    def test_unit_whose_display_authority_did_not_resolve_is_not_addressable(self) -> None:
        for authority in ("missing", "invalid"):
            with self.subTest(authority=authority):
                payload = remote_board_payload()
                payload["units"][0]["authority_state"] = authority
                multi, _ = runtime(
                    {
                        REMOTE_BOARD_ARGS: payload,
                        REMOTE_WORKSPACE_ARGS: WORKSPACE_PAYLOAD,
                    }
                )
                unit_id = next(
                    unit.unit_id
                    for unit in multi.snapshot().units
                    if unit.host_id == "devbox"
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
    def test_workspace_resolves_the_git_root_only(self) -> None:
        multi, _ = runtime()

        workspace = multi.resolve_source_workspace(
            REMOTE_CONFIG.by_id["devbox"], WORKSPACE_A
        )

        self.assertIsNotNone(workspace)
        assert workspace is not None
        self.assertEqual(workspace.canonical_path, "/srv/checkouts/mozyo_bridge")
        # The registry project name is display metadata and a directory-name
        # default; it must not be reachable as a scope authority from here.
        self.assertFalse(hasattr(workspace, "project_name"))

    def test_a_registry_row_without_a_project_name_still_resolves(self) -> None:
        multi, _ = runtime(
            {
                REMOTE_BOARD_ARGS: remote_board_payload(),
                REMOTE_WORKSPACE_ARGS: {
                    "workspaces": [
                        {
                            "workspace_id": WORKSPACE_A,
                            "canonical_path": "/srv/checkouts/mozyo_bridge",
                        }
                    ]
                },
            }
        )

        self.assertIsNotNone(
            multi.resolve_source_workspace(REMOTE_CONFIG.by_id["devbox"], WORKSPACE_A)
        )

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

    def test_a_registry_path_with_a_control_character_is_rejected(self) -> None:
        # The path is untrusted input that becomes an argv element; checked here
        # rather than at the subprocess boundary, where the failure mode is an
        # exception instead of a refusal.
        for path in ("/srv/ok\x00evil", "/srv/tab\tx", "/" + "a" * (MAX_REMOTE_PATH_LENGTH + 1)):
            with self.subTest(path=path[:16]):
                multi, _ = runtime(
                    {
                        REMOTE_BOARD_ARGS: remote_board_payload(),
                        REMOTE_WORKSPACE_ARGS: {
                            "workspaces": [
                                {
                                    "workspace_id": WORKSPACE_A,
                                    "canonical_path": path,
                                    "project_name": "mozyo_bridge",
                                }
                            ]
                        },
                    }
                )

                self.assertIsNone(
                    multi.resolve_source_workspace(
                        REMOTE_CONFIG.by_id["devbox"], WORKSPACE_A
                    )
                )

    def test_control_characters_are_rejected_across_the_whole_category(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_multi_source_unit_board import (
            _usable_remote_path,
        )

        self.assertTrue(_usable_remote_path("/srv/checkouts/mozyo_bridge"))
        for name, char in (
            ("NUL", "\x00"), ("C0", "\x1f"), ("DEL", "\x7f"),
            # The C1 block is what an ASCII-range check silently passes.
            ("C1 lower", "\x80"), ("NEL", "\x85"), ("C1 upper", "\x9f"),
        ):
            with self.subTest(control=name):
                self.assertFalse(_usable_remote_path("/srv/" + char + "x"))

    def test_an_argv_the_subprocess_layer_refuses_is_a_typed_failure(self) -> None:
        def refusing(argv, **kw):
            raise ValueError("embedded null byte")

        multi = MultiSourceUnitBoardRuntime(
            REMOTE_CONFIG,
            local_runtime=FakeLocalRuntime(),
            runner=refusing,
            clock=lambda: NOW,
        )

        self.assertIsNone(
            multi.run_source_command(REMOTE_CONFIG.by_id["devbox"], ("workspace", "list"))
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
