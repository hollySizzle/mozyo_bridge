from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_RESOLVED,
    DUPLICATE_SCOPE_CROSS_SOURCE,
    DUPLICATE_SCOPE_NONE,
    SOURCE_LIVE,
    SOURCE_RELOAD_REQUIRED,
    SOURCE_STALE,
    SOURCE_UNAVAILABLE,
    AgentObservation,
    _unit_public_id,
    build_unit_board,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_aggregate import (
    DEFAULT_SOURCE_FRESHNESS_SECONDS,
    MAX_REMOTE_CLOCK_SKEW_SECONDS,
    MAX_REMOTE_PAYLOAD_AGE_SECONDS,
    MAX_SOURCE_UNITS,
    remote_payload_freshness,
    actionable_workspace_id,
    aggregate_sources,
    format_multi_source_board,
    freshness_state,
    local_source_observation,
    mark_stale,
    parse_remote_board_payload,
    remote_unit_public_id,
    unavailable_source_observation,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    UnitBoardSource,
)


WORKSPACE_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
STAMP = NOW.isoformat(timespec="seconds")

LOCAL = UnitBoardSource.local_default()
REMOTE = UnitBoardSource.from_record(
    {"host_id": "devbox", "kind": "ssh", "ssh_target": "devbox", "label": "dev host"}
)


def local_snapshot(lane_id: str = "default", workspace_id: str = WORKSPACE_A):
    return build_unit_board(
        (
            AgentObservation(
                workspace_id=workspace_id,
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
        observed_at=STAMP,
    )


def remote_payload(
    lane_id: str = "default",
    workspace_id: str = WORKSPACE_A,
    unit_id: str = "unit-deadbeef",
):
    return {
        "source_state": SOURCE_LIVE,
        "observed_at": STAMP,
        "unmanaged_agents": 0,
        "detail": "",
        "units": [
            {
                "unit_id": unit_id,
                "workspace_id": workspace_id,
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


def remote_payload_at(observed_at: str):
    payload = remote_payload()
    payload["observed_at"] = observed_at
    return payload


class RemotePayloadTests(unittest.TestCase):
    def test_remote_rows_are_tagged_with_their_source(self) -> None:
        observation = parse_remote_board_payload(
            remote_payload(), source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_LIVE)
        self.assertEqual(observation.status.host_label, "dev host")
        self.assertEqual(len(observation.rows), 1)
        self.assertEqual(observation.rows[0].host_id, "devbox")
        self.assertEqual(observation.rows[0].host_label, "dev host")

    def test_remote_unit_key_is_derived_from_the_remote_opaque_key(self) -> None:
        observation = parse_remote_board_payload(
            remote_payload(), source=REMOTE, observed_at=STAMP, now=NOW
        )
        unit_id = observation.rows[0].unit_id

        self.assertEqual(unit_id, remote_unit_public_id("devbox", "unit-deadbeef"))
        self.assertEqual(observation.remote_unit_ids[unit_id], "unit-deadbeef")

    def test_remote_unit_key_differs_per_source_for_the_same_remote_key(self) -> None:
        other = UnitBoardSource.from_record(
            {"host_id": "buildbox", "kind": "ssh", "ssh_target": "buildbox"}
        )

        self.assertNotEqual(
            remote_unit_public_id("devbox", "unit-deadbeef"),
            remote_unit_public_id(other.host_id, "unit-deadbeef"),
        )

    def test_remote_key_can_never_collide_with_a_local_unit_key(self) -> None:
        # The host-qualified digest is domain-separated from the local shape, so
        # no source can mint a key that lands in the local key space and
        # relabels a local pane's `mozyo_unit` metadata.
        local_key = local_snapshot().units[0].unit_id

        self.assertNotEqual(remote_unit_public_id("devbox", local_key), local_key)
        self.assertNotEqual(
            _unit_public_id(WORKSPACE_A, "default", "devbox"), local_key
        )

    def test_remote_pane_locators_are_never_adopted(self) -> None:
        payload = remote_payload()
        payload["units"][0]["agents"][0]["pane_id"] = "w9:p9"

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.rows[0].agents[0].pane_id, "")

    def test_unreadable_payload_fails_closed_without_rows(self) -> None:
        for payload in (None, {}, {"source_state": SOURCE_LIVE}, {"source_state": SOURCE_LIVE, "units": {}}):
            with self.subTest(payload=payload):
                observation = parse_remote_board_payload(
                    payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

                self.assertEqual(
                    observation.status.source_state, SOURCE_RELOAD_REQUIRED
                )
                self.assertEqual(observation.rows, ())
                self.assertFalse(observation.status.actionable)

    def test_unit_row_missing_identity_fails_the_whole_answer(self) -> None:
        payload = remote_payload()
        del payload["units"][0]["workspace_id"]

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_RELOAD_REQUIRED)
        self.assertEqual(observation.rows, ())

    def test_duplicate_remote_unit_keys_fail_closed(self) -> None:
        payload = remote_payload()
        payload["units"].append(dict(payload["units"][0]))

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_RELOAD_REQUIRED)

    def test_unbounded_remote_answers_are_rejected(self) -> None:
        payload = remote_payload()
        payload["units"] = [
            dict(payload["units"][0], unit_id=f"unit-{index:08x}")
            for index in range(MAX_SOURCE_UNITS + 1)
        ]

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_RELOAD_REQUIRED)

    def test_remote_text_is_re_projected_public_safe(self) -> None:
        payload = remote_payload()
        payload["units"][0]["project_label"] = "/workspace/project-alpha"
        payload["units"][0]["work_label"] = "token=DROP-TOKEN-SENTINEL"

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.rows[0].project_label, "[redacted]")
        self.assertEqual(observation.rows[0].work_label, "[redacted]")

    def test_non_live_remote_state_is_kept_as_a_visible_unavailable_source(self) -> None:
        observation = parse_remote_board_payload(
            {"source_state": SOURCE_UNAVAILABLE, "units": []},
            source=REMOTE,
            observed_at=STAMP,
            now=NOW,
        )

        self.assertEqual(observation.status.source_state, SOURCE_UNAVAILABLE)
        self.assertFalse(observation.status.actionable)


class NestedAnswerTests(unittest.TestCase):
    def test_a_merged_answer_is_rejected_rather_than_re_tagged(self) -> None:
        # A source must answer for its own server only.  A merged answer holds
        # rows from servers this client never asked about, and adopting them
        # would attribute another host's Units to this source.
        payload = remote_payload()
        payload["sources"] = [
            {
                "host_id": "third",
                "host_label": "third host",
                "host_kind": "ssh",
                "source_state": SOURCE_LIVE,
                "observed_at": STAMP,
                "unit_count": 1,
                "unmanaged_agents": 0,
                "actionable": True,
                "detail": "",
            }
        ]

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_RELOAD_REQUIRED)
        self.assertEqual(observation.rows, ())
        self.assertFalse(observation.status.actionable)

    def test_an_empty_sources_envelope_is_still_a_merged_answer(self) -> None:
        payload = remote_payload()
        payload["sources"] = []

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_RELOAD_REQUIRED)


class RemotePayloadFreshnessTests(unittest.TestCase):
    def test_a_payload_from_another_era_is_not_action_authority(self) -> None:
        observation = parse_remote_board_payload(
            remote_payload_at("2000-01-01T00:00:00+00:00"),
            source=REMOTE,
            observed_at=STAMP,
            now=NOW,
        )

        self.assertEqual(observation.status.source_state, SOURCE_STALE)
        self.assertFalse(observation.status.actionable)
        self.assertEqual(len(observation.rows), 1)

    def test_an_undated_or_unparsable_payload_is_not_action_authority(self) -> None:
        for stamp in ("", "not-a-time", "2026-08-09T12:00:00"):
            with self.subTest(stamp=stamp):
                observation = parse_remote_board_payload(
                    remote_payload_at(stamp),
                    source=REMOTE,
                    observed_at=STAMP,
                    now=NOW,
                )

                self.assertEqual(observation.status.source_state, SOURCE_STALE)

    def test_ordinary_clock_skew_does_not_disable_a_source(self) -> None:
        ahead = (NOW + timedelta(seconds=MAX_REMOTE_CLOCK_SKEW_SECONDS - 5)).isoformat(
            timespec="seconds"
        )

        observation = parse_remote_board_payload(
            remote_payload_at(ahead), source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_LIVE)

    def test_the_clock_is_required_so_the_parser_cannot_fail_open(self) -> None:
        # An optional clock would mean a mode in which an undated remote answer
        # reads as live; a fail-open default at a trust boundary is the defect.
        with self.assertRaises(TypeError):
            parse_remote_board_payload(
                remote_payload(), source=REMOTE, observed_at=STAMP
            )

    def test_future_beyond_the_skew_allowance_is_stale(self) -> None:
        beyond = (NOW + timedelta(seconds=MAX_REMOTE_CLOCK_SKEW_SECONDS + 30)).isoformat(
            timespec="seconds"
        )

        observation = parse_remote_board_payload(
            remote_payload_at(beyond), source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_STALE)

    def test_the_client_side_dimension_still_rejects_every_future_stamp(self) -> None:
        # One clock measures the round trip, so a future stamp there is a
        # contradiction rather than skew — a deliberately different rule.
        self.assertEqual(
            freshness_state(STAMP, NOW - timedelta(seconds=5)), SOURCE_STALE
        )

    def test_the_bound_is_looser_than_the_client_side_one(self) -> None:
        # The client times its own round trip; this dimension compares two
        # machines' clocks, so it must tolerate more before failing closed.
        self.assertGreater(
            MAX_REMOTE_PAYLOAD_AGE_SECONDS, DEFAULT_SOURCE_FRESHNESS_SECONDS
        )
        self.assertEqual(
            remote_payload_freshness(STAMP, NOW + timedelta(seconds=60)), SOURCE_LIVE
        )
        self.assertEqual(
            remote_payload_freshness(
                STAMP, NOW + timedelta(seconds=MAX_REMOTE_PAYLOAD_AGE_SECONDS + 60)
            ),
            SOURCE_STALE,
        )


class RemoteIdentityRecomputationTests(unittest.TestCase):
    def test_a_row_declaring_resolved_with_a_duplicate_provider_degrades(self) -> None:
        # The local producer calls this contradiction ambiguous; a remote row
        # must not walk past the action gate on its own say-so.
        payload = remote_payload()
        payload["units"][0]["agents"] = [
            {"provider": "codex", "runtime_state": "idle", "interactive_ready": True},
            {"provider": "codex", "runtime_state": "idle", "interactive_ready": True},
        ]

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.rows[0].identity_state, "ambiguous")

    def test_a_row_with_no_agents_degrades_to_ambiguous(self) -> None:
        # A Unit groups at least one observed agent; the local producer cannot
        # emit an empty one, so the row stays visible but unactionable.
        payload = remote_payload()
        payload["units"][0]["agents"] = []

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.status.source_state, SOURCE_LIVE)
        self.assertEqual(observation.rows[0].identity_state, "ambiguous")

    def test_shape_violations_degrade_the_whole_source(self) -> None:
        # The other half of the split: a field of the wrong type or an empty
        # identity field means the answer cannot be interpreted at all.
        cases = {
            "empty provider": {"agents": [
                {"provider": "", "runtime_state": "idle", "interactive_ready": True}
            ]},
            "empty lane": {"lane_id": ""},
            "empty workspace": {"workspace_id": ""},
            # JSON carries "false" as a string and bool("false") is True, so a
            # truthy read would display the opposite of what the source said.
            "string readiness": {"agents": [
                {"provider": "codex", "runtime_state": "idle", "interactive_ready": "false"}
            ]},
        }
        for label, override in cases.items():
            with self.subTest(case=label):
                payload = remote_payload()
                payload["units"][0].update(override)

                observation = parse_remote_board_payload(
                    payload, source=REMOTE, observed_at=STAMP, now=NOW
                )

                self.assertEqual(
                    observation.status.source_state, SOURCE_RELOAD_REQUIRED
                )
                self.assertEqual(observation.rows, ())

    def test_an_exact_boolean_readiness_is_carried_through(self) -> None:
        payload = remote_payload()
        payload["units"][0]["agents"][0]["interactive_ready"] = False

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertIs(observation.rows[0].agents[0].interactive_ready, False)

    def test_a_consistent_row_keeps_its_declared_state(self) -> None:
        observation = parse_remote_board_payload(
            remote_payload(), source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.rows[0].identity_state, "resolved")

    def test_a_declared_ambiguous_row_is_never_upgraded(self) -> None:
        payload = remote_payload()
        payload["units"][0]["identity_state"] = "ambiguous"

        observation = parse_remote_board_payload(
            payload, source=REMOTE, observed_at=STAMP, now=NOW
        )

        self.assertEqual(observation.rows[0].identity_state, "ambiguous")


class FreshnessTests(unittest.TestCase):
    def test_recent_observation_is_live(self) -> None:
        self.assertEqual(freshness_state(STAMP, NOW + timedelta(seconds=5)), SOURCE_LIVE)

    def test_old_observation_is_stale(self) -> None:
        self.assertEqual(
            freshness_state(STAMP, NOW + timedelta(seconds=120)), SOURCE_STALE
        )

    def test_undated_or_future_observation_is_stale(self) -> None:
        self.assertEqual(freshness_state("", NOW), SOURCE_STALE)
        self.assertEqual(freshness_state("not-a-time", NOW), SOURCE_STALE)
        self.assertEqual(
            freshness_state(STAMP, NOW - timedelta(seconds=120)), SOURCE_STALE
        )

    def test_naive_timestamp_is_stale_rather_than_compared(self) -> None:
        self.assertEqual(freshness_state("2026-08-09T12:00:00", NOW), SOURCE_STALE)

    def test_mark_stale_keeps_rows_but_removes_action_authority(self) -> None:
        observation = parse_remote_board_payload(
            remote_payload(), source=REMOTE, observed_at=STAMP, now=NOW
        )

        stale = mark_stale(observation, NOW + timedelta(seconds=120))

        self.assertEqual(stale.status.source_state, SOURCE_STALE)
        self.assertFalse(stale.status.actionable)
        self.assertEqual(len(stale.rows), 1)


class AggregateTests(unittest.TestCase):
    def test_same_workspace_and_lane_on_two_sources_stay_distinct(self) -> None:
        snapshot = aggregate_sources(
            (
                local_source_observation(local_snapshot(), source=LOCAL),
                parse_remote_board_payload(
                    remote_payload(), source=REMOTE, observed_at=STAMP, now=NOW
        ),
            ),
            observed_at=STAMP,
        )

        self.assertEqual(len(snapshot.units), 2)
        self.assertEqual(
            {unit.host_id for unit in snapshot.units}, {"local", "devbox"}
        )
        self.assertEqual(len({unit.unit_id for unit in snapshot.units}), 2)
        for unit in snapshot.units:
            self.assertEqual(unit.duplicate_scope, DUPLICATE_SCOPE_CROSS_SOURCE)

    def test_distinct_identities_are_not_marked_duplicate(self) -> None:
        snapshot = aggregate_sources(
            (
                local_source_observation(local_snapshot(), source=LOCAL),
                parse_remote_board_payload(
                    remote_payload(lane_id="issue_15138"),
                    source=REMOTE,
                    observed_at=STAMP,
                    now=NOW,
                ),
            ),
            observed_at=STAMP,
        )

        for unit in snapshot.units:
            self.assertEqual(unit.duplicate_scope, DUPLICATE_SCOPE_NONE)

    def test_one_unreachable_source_degrades_only_itself(self) -> None:
        snapshot = aggregate_sources(
            (
                local_source_observation(local_snapshot(), source=LOCAL),
                unavailable_source_observation(REMOTE, observed_at=STAMP),
            ),
            observed_at=STAMP,
        )

        self.assertTrue(snapshot.ok)
        self.assertEqual(len(snapshot.units), 1)
        states = {status.host_id: status for status in snapshot.sources}
        self.assertTrue(states["local"].actionable)
        self.assertFalse(states["devbox"].actionable)
        self.assertIn("dev host=unavailable", snapshot.detail)

    def test_every_source_failing_makes_the_board_unavailable(self) -> None:
        snapshot = aggregate_sources(
            (
                unavailable_source_observation(LOCAL, observed_at=STAMP),
                unavailable_source_observation(REMOTE, observed_at=STAMP),
            ),
            observed_at=STAMP,
        )

        self.assertFalse(snapshot.ok)
        self.assertEqual(snapshot.source_state, SOURCE_UNAVAILABLE)

    def test_payload_carries_source_status_without_connection_values(self) -> None:
        snapshot = aggregate_sources(
            (
                local_source_observation(local_snapshot(), source=LOCAL),
                unavailable_source_observation(REMOTE, observed_at=STAMP),
            ),
            observed_at=STAMP,
        )

        payload = snapshot.as_payload()

        self.assertEqual(len(payload["sources"]), 2)
        self.assertNotIn("ssh_target", str(payload))
        self.assertNotIn("devbox", str(payload["sources"][1]["host_label"]))

    def test_local_only_snapshot_payload_has_no_source_envelope(self) -> None:
        self.assertNotIn("sources", local_snapshot().as_payload())

    def test_merged_rows_carry_host_identity(self) -> None:
        snapshot = aggregate_sources(
            (
                local_source_observation(local_snapshot(), source=LOCAL),
                parse_remote_board_payload(
                    remote_payload(), source=REMOTE, observed_at=STAMP, now=NOW
                ),
            ),
            observed_at=STAMP,
        )

        for row in snapshot.as_payload()["units"]:
            self.assertIn("host_id", row)
            self.assertIn("host_label", row)
            self.assertIn("duplicate_scope", row)


class ActionInputTests(unittest.TestCase):
    def test_registry_shaped_workspace_id_is_an_action_input(self) -> None:
        row = local_snapshot().units[0]

        self.assertEqual(actionable_workspace_id(row), WORKSPACE_A)

    def test_display_shaped_workspace_id_is_not_an_action_input(self) -> None:
        row = local_snapshot(workspace_id="workspace-a").units[0]

        self.assertIsNone(actionable_workspace_id(row))

    def test_truncated_workspace_id_is_not_an_action_input(self) -> None:
        row = local_snapshot(workspace_id="x" * 200).units[0]

        self.assertIsNone(actionable_workspace_id(row))


class MultiSourceRenderingTests(unittest.TestCase):
    def test_every_source_and_unit_names_its_host(self) -> None:
        snapshot = aggregate_sources(
            (
                local_source_observation(local_snapshot(), source=LOCAL),
                unavailable_source_observation(REMOTE, observed_at=STAMP),
            ),
            observed_at=STAMP,
        )

        text = format_multi_source_board(snapshot, width=120)

        self.assertIn("source local [local] live", text)
        self.assertIn("! source dev host [ssh] unavailable", text)
        self.assertIn("[local] mozyo_bridge", text)

    def test_rendered_lines_respect_the_requested_width(self) -> None:
        snapshot = aggregate_sources(
            (local_source_observation(local_snapshot(), source=LOCAL),),
            observed_at=STAMP,
        )

        for line in format_multi_source_board(snapshot, width=40).splitlines():
            self.assertLessEqual(len(line), 40)


if __name__ == "__main__":
    unittest.main()
