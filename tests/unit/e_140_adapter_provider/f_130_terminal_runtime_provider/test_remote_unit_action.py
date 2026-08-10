from __future__ import annotations

import dataclasses
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
    REASON_CONNECTION_VALUE_DISCLOSED,
    REASON_PREVIEW_MISMATCH,
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


def delivery_record(**outcome) -> str:
    """The shape a handoff CLI actually prints.

    ``record_format`` defaults to ``both``: a human-readable record, a blank
    line, then the single-line JSON outcome last.  The R3 fixture returned only
    the bare JSON, which is why a reader that parsed the whole stdout as one
    document passed its tests and failed against the real CLI (review j#101891
    finding_1).
    """
    payload = {"status": "sent", "reason": "ok"}
    payload.update(outcome)
    return (
        "Delivery result — sent\n"
        "\n"
        "- Receiver: `codex`\n"
        "- Source: `redmine`\n"
        f"- Status: `{payload['status']}` (reason: `{payload['reason']}`)\n"
        "\n"
        + json.dumps(payload)
    )


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
        GATEWAY_ARGS: delivery_record(),
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
        "target_project": "giken-3800-mozyo-bridge",
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

    def test_a_non_canonical_redmine_anchor_refuses(self) -> None:
        # str.isdigit() is true for full-width digits, and the preview projects
        # them to ASCII while the delivery would send the raw string — so the
        # operator would confirm one anchor and another would be delivered.
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        before = len(runner.argvs)

        for name, override in (
            ("full-width issue", {"issue": "１５１３８"}),
            ("full-width journal", {"journal": "１０１９８１"}),
            ("leading zero", {"issue": "0015138"}),
            ("over the canonical width", {"issue": "1" * 120}),
            ("zero", {"issue": "0"}),
        ):
            with self.subTest(case=name):
                preview = action.preview(request(unit_id, **override))

                self.assertEqual(preview.reason, REASON_INVALID_REQUEST)
        self.assertEqual(len(runner.argvs), before)

    def test_the_anchor_bound_matches_the_repository_wide_contract(self) -> None:
        # The shape is already defined; a narrower local rule rejected ids the
        # rest of the repository accepts.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (
            MAX_CANONICAL_DECIMAL_VALUE,
            is_canonical_positive_decimal,
        )

        for value in ("1", "999999999", "1000000000", str(MAX_CANONICAL_DECIMAL_VALUE)):
            with self.subTest(value=value):
                self.assertTrue(is_canonical_positive_decimal(value))
                accepted = RemoteUnitActionRequest(
                    unit_id="unit-x", issue=value, journal="1", summary="s",
                    target_project="scope",
                ).validated() is None

                self.assertTrue(accepted)

        for value in (str(MAX_CANONICAL_DECIMAL_VALUE + 1), "0015138", "１５１３８"):
            with self.subTest(value=value):
                self.assertFalse(is_canonical_positive_decimal(value))

    def test_a_wide_canonical_anchor_is_shown_and_delivered_byte_identical(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.marker_value_contract import (
            MAX_CANONICAL_DECIMAL_VALUE,
        )

        widest = str(MAX_CANONICAL_DECIMAL_VALUE)
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)

        preview = action.preview(request(unit_id, issue=widest, journal=widest))
        action.apply(preview)

        self.assertEqual(preview.as_payload()["issue"], widest)
        command = next(
            argv[-1] for argv in runner.argvs if "project-gateway" in argv[-1]
        )
        self.assertIn(f"--issue {widest}", command)
        self.assertIn(f"--journal {widest}", command)

    def test_a_canonical_anchor_is_shown_and_delivered_byte_identical(self) -> None:
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)

        preview = action.preview(request(unit_id, issue="999999999", journal="1"))
        action.apply(preview)

        self.assertEqual(preview.as_payload()["issue"], "999999999")
        command = next(
            argv[-1] for argv in runner.argvs if "project-gateway" in argv[-1]
        )
        self.assertIn("--issue 999999999", command)
        self.assertIn("--journal 1", command)

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
            {"target_project": ""},
            {"target_project": "   "},
            {"summary": "see /workspace/project-alpha for context"},
            {"summary": "token=DROP-TOKEN-SENTINEL"},
            {"kind": "close"},
        ):
            with self.subTest(overrides=overrides):
                preview = action.preview(request(unit_id, **overrides))

                self.assertEqual(preview.reason, REASON_INVALID_REQUEST)
        self.assertEqual(len(runner.argvs), before)


    def test_the_rendered_preview_shows_only_projected_values(self) -> None:
        # The text surface renders from the payload, so a value the projection
        # would redact cannot print verbatim on the terminal while the JSON
        # surface hides it.
        action, runtime, _ = rail()
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        payload = preview.as_payload()

        lines = "\n".join(render_preview(preview))

        for key in ("host_label", "project_label", "lane_id", "summary", "target_project"):
            self.assertIn(str(payload[key]), lines)

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
    def test_declared_project_scope_is_what_gets_delivered(self) -> None:
        # The registry project name is display metadata; the scope authority is
        # the operator's declaration and nothing derived from the board.
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)

        preview = action.preview(request(unit_id, target_project="scope-alpha"))
        action.apply(preview)

        self.assertEqual(preview.as_payload()["target_project"], "scope-alpha")
        command = next(
            argv[-1] for argv in runner.argvs if "project-gateway" in argv[-1]
        )
        self.assertIn("--target-project scope-alpha", command)
        self.assertNotIn("--target-project mozyo_bridge", command)


class ConnectionValueDisclosureTests(unittest.TestCase):
    def test_a_summary_repeating_a_connection_value_refuses(self) -> None:
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        before = len(runner.argvs)

        preview = action.preview(
            request(unit_id, summary="ping SSH-DESTINATION-SENTINEL first")
        )

        self.assertEqual(preview.reason, REASON_CONNECTION_VALUE_DISCLOSED)
        self.assertNotIn("SSH-DESTINATION-SENTINEL", json.dumps(preview.as_payload()))
        self.assertEqual(len(runner.argvs), before)

    def test_a_project_scope_repeating_a_connection_value_refuses(self) -> None:
        action, runtime, _ = rail()
        unit_id = remote_unit_id(runtime)

        preview = action.preview(
            request(unit_id, target_project="ssh-destination-sentinel")
        )

        self.assertEqual(preview.reason, REASON_CONNECTION_VALUE_DISCLOSED)


class PreviewSubstitutionTests(unittest.TestCase):
    """apply re-proves the request; it does not read the preview handed to it.

    A preview is a public object this package exports, so ``apply`` receiving
    one is not evidence that ``preview`` produced it.
    """

    SUBSTITUTIONS = {
        "another canonical anchor": {"issue": "999999999", "journal": "888888888"},
        "another permitted kind": {"kind": "review_request"},
        "another summary": {"summary": "a completely different instruction"},
        "another project scope": {"target_project": "some-other-scope"},
        # Values that would never have passed validation in the first place.
        "an anchor with a leading zero": {"issue": "0015138"},
        "a kind outside the vocabulary": {"kind": "close"},
        "a credential-shaped summary": {"summary": "token=DROP-TOKEN-SENTINEL"},
        "a configured connection value": {"summary": "ping SSH-DESTINATION-SENTINEL"},
        # Display-only fields the operator confirmed.
        "a different displayed host": {"host_label": "somewhere else"},
        "a different displayed lane": {"lane_id": "issue_99999"},
    }

    def test_a_substituted_preview_delivers_nothing(self) -> None:
        for name, changes in self.SUBSTITUTIONS.items():
            with self.subTest(substitution=name):
                action, runtime, runner = rail()
                unit_id = remote_unit_id(runtime)
                preview = action.preview(request(unit_id))

                result = action.apply(dataclasses.replace(preview, **changes))

                self.assertEqual(result.state, ACTION_REFUSED)
                self.assertEqual(result.reason, REASON_PREVIEW_MISMATCH)
                self.assertFalse(
                    [argv for argv in runner.argvs if "project-gateway" in argv[-1]]
                )

    def test_substituting_the_evidence_request_is_revalidated(self) -> None:
        # The second layer: even reaching past the comparison, the request that
        # travels with the evidence is checked again before anything is built
        # from it.
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        tampered = dataclasses.replace(
            preview,
            evidence=dataclasses.replace(
                preview.evidence,
                request=dataclasses.replace(
                    preview.evidence.request,
                    summary="ping SSH-DESTINATION-SENTINEL",
                ),
            ),
        )

        result = action.apply(tampered)

        self.assertEqual(result.reason, REASON_CONNECTION_VALUE_DISCLOSED)
        self.assertFalse(
            [argv for argv in runner.argvs if "project-gateway" in argv[-1]]
        )

    def test_the_delivered_argv_comes_from_the_validated_request(self) -> None:
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id, issue="15138", summary="board pointer"))

        result = action.apply(preview)

        self.assertEqual(result.state, ACTION_DELIVERED)
        command = next(
            argv[-1] for argv in runner.argvs if "project-gateway" in argv[-1]
        )
        self.assertIn("--issue 15138", command)
        self.assertIn("--summary 'board pointer'", command)


class PreviewReprTests(unittest.TestCase):
    """Safe to render is not the same as safe to print."""

    def test_no_connection_value_or_remote_path_appears_in_a_repr(self) -> None:
        action, runtime, _ = rail()
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))
        result = action.apply(preview)

        for name, rendered in (
            ("preview", repr(preview)),
            ("result", repr(result)),
            ("evidence", repr(preview.evidence)),
            ("source", repr(preview.evidence.target.source)),
            ("workspace", repr(preview.evidence.workspace)),
        ):
            with self.subTest(object=name):
                self.assertNotIn("SSH-DESTINATION-SENTINEL", rendered)
                self.assertNotIn("/srv/checkouts", rendered)

    def test_the_public_projection_still_carries_the_display_values(self) -> None:
        action, runtime, _ = rail()
        unit_id = remote_unit_id(runtime)
        preview = action.preview(request(unit_id))

        self.assertEqual(preview.as_payload()["host_label"], "dev host")


class ApplyDeliveryTests(unittest.TestCase):
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
        self.assertIn("--target-project giken-3800-mozyo-bridge", command)
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

    def test_a_zero_exit_with_a_non_delivered_outcome_is_not_delivered(self) -> None:
        # rc 0 is not proof of delivery: a parked composer and a
        # marker-unobserved queue-enter both exit 0 without reaching a receiver.
        for label, outcome in (
            ("blocked", delivery_record(status="blocked", reason="turn_start_unconfirmed")),
            ("pending_input", delivery_record(status="pending_input", reason="ok")),
            ("queue_enter", delivery_record(status="sent", reason="queue_enter")),
            ("empty object", {}),
            ("record with no JSON line", "Delivery result — sent\n\n- Status: `sent`"),
        ):
            with self.subTest(outcome=label):
                action, runtime, _ = rail(answers({GATEWAY_ARGS: outcome}))
                unit_id = remote_unit_id(runtime)

                result = action.apply(action.preview(request(unit_id)))

                self.assertEqual(result.state, ACTION_REFUSED)
                self.assertEqual(result.reason, REASON_DELIVERY_FAILED)

    def test_an_unreadable_gateway_answer_is_not_delivered(self) -> None:
        class Unreadable(RecordingRunner):
            def __call__(self, argv, **kwargs):
                if "project-gateway" in argv[-1]:
                    self.argvs.append(list(argv))
                    return subprocess.CompletedProcess(argv, 0, "not json", "")
                return super().__call__(argv, **kwargs)

        runner = Unreadable(answers())
        runtime = MultiSourceUnitBoardRuntime(
            REMOTE_CONFIG, local_runtime=FakeLocalRuntime(), runner=runner, clock=MovableClock()
        )
        action = RemoteUnitActionRail(runtime, clock=MovableClock())
        unit_id = next(
            unit.unit_id for unit in runtime.snapshot().units if unit.host_id == "devbox"
        )

        result = action.apply(action.preview(request(unit_id)))

        self.assertEqual(result.reason, REASON_DELIVERY_FAILED)

    def test_a_confirmed_submission_is_delivered_without_echoing_the_record(self) -> None:
        action, runtime, runner = rail(
            answers(
                {
                    GATEWAY_ARGS: delivery_record(
                        target="%1075", repo_root="/srv/checkouts/mozyo_bridge"
                    )
                }
            )
        )
        unit_id = remote_unit_id(runtime)

        result = action.apply(action.preview(request(unit_id)))

        self.assertEqual(result.state, ACTION_DELIVERED)
        rendered = json.dumps(result.as_payload())
        self.assertNotIn("%1075", rendered)
        self.assertNotIn("/srv/checkouts", rendered)

    def test_output_after_the_outcome_line_is_not_ignored(self) -> None:
        # The contract picks the LAST line; scanning past it for something
        # parseable would let a stale success survive whatever followed it.
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.remote_unit_action import (
            _gateway_confirmed_submission,
        )

        good = '{"status": "sent", "reason": "ok"}'
        self.assertTrue(_gateway_confirmed_submission(good))
        self.assertTrue(_gateway_confirmed_submission(delivery_record()))
        for name, tail in (
            ("another object", '{"receipt": "later"}'),
            ("malformed", "{bad}"),
            ("array", "[]"),
            ("null", "null"),
            ("prose", "done."),
        ):
            with self.subTest(trailing=name):
                self.assertFalse(_gateway_confirmed_submission(good + "\n" + tail))

    def test_the_gateway_is_asked_for_a_deterministic_output_shape(self) -> None:
        # The gateway's own --json only shapes a fail-closed resolution; without
        # this the success path returns the markdown-plus-JSON default.
        action, runtime, runner = rail()
        unit_id = remote_unit_id(runtime)

        action.apply(action.preview(request(unit_id)))

        command = next(
            argv[-1] for argv in runner.argvs if "project-gateway" in argv[-1]
        )
        self.assertIn("--record-format json", command)

    def test_a_unit_whose_identity_the_client_recomputed_as_ambiguous_refuses(self) -> None:
        payload = remote_board_payload()
        payload["units"][0]["agents"] = [
            {"provider": "codex", "runtime_state": "idle", "interactive_ready": True},
            {"provider": "codex", "runtime_state": "idle", "interactive_ready": True},
        ]
        action, runtime, runner = rail(answers({REMOTE_BOARD_ARGS: payload}))
        unit_id = next(
            unit.unit_id for unit in runtime.snapshot().units if unit.host_id == "devbox"
        )

        preview = action.preview(request(unit_id))

        self.assertEqual(preview.reason, REASON_UNIT_UNRESOLVED)
        self.assertFalse(
            [argv for argv in runner.argvs if "project-gateway" in argv[-1]]
        )

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



if __name__ == "__main__":
    unittest.main()
