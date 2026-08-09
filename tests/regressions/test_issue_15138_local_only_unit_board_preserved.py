"""Regression pin: multi-source observation must not move the local-only board.

Redmine #15138 added host-aware Unit identity so local, remote-host, and Dev
Container Units can share one view.  The one thing that change must not do is
alter what an operator who never configures a remote source already sees.  The
opaque Unit key is written into live Herdr display metadata (``mozyo_unit``), so
a shifted key would silently relabel every managed pane on the local server —
and the rendered board is what an operator reads to tell Units apart.

These pins hold the local-only surface still: the opaque key (pinned to the
values the pre-#15138 digest produced), the rendered text, the payload envelope,
and the fact that Herdr metadata sync stays a purely local writer.
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_RESOLVED,
    AgentObservation,
    _unit_public_id,
    build_unit_board,
    format_board,
    metadata_for_unit,
)
from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    LOCAL_HOST_ID,
    UnitBoardSourcesConfig,
)  # noqa: F401  (LOCAL_HOST_ID pins the key the local digest branch depends on)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_unit_board_runtime import (
    METADATA_TOKEN_KEYS,
)


WORKSPACE_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
STAMP = "2026-08-09T12:00:00+00:00"

#: Values produced by the pre-#15138 two-component digest.  Recomputed by hand
#: from the historical algorithm, not copied from the current implementation.
PINNED_DEFAULT_LANE_UNIT_ID = "unit-f5bab7c512f3d490f8a1809d88b36b20"
PINNED_ISSUE_LANE_UNIT_ID = "unit-475fc0a512c35213cc4e7cf49a68c16e"

#: The row payload keys emitted by parent commit 1e11b537, transcribed from that
#: commit's ``UnitBoardRow.as_payload``.  Pinning the parent's key set — rather
#: than asserting the fields this issue added — is what makes this a
#: re-occurrence detector instead of a description of the new behaviour
#: (Redmine #15138 review j#101787 f5).
PARENT_ROW_PAYLOAD_KEYS = (
    "agents",
    "authority_state",
    "identity_state",
    "lane_id",
    "project_label",
    "responsibility",
    "unit_id",
    "work_label",
    "workflow_role",
    "workspace_id",
)


def local_board(lane_id: str = "default"):
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
            AgentObservation(
                workspace_id=WORKSPACE_A,
                lane_id=lane_id,
                provider="claude",
                pane_id="w1:p2",
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


class LocalOnlyBoardPreservedTests(unittest.TestCase):
    def test_local_unit_keys_are_unchanged(self) -> None:
        self.assertEqual(
            _unit_public_id(WORKSPACE_A, "default"), PINNED_DEFAULT_LANE_UNIT_ID
        )
        self.assertEqual(
            _unit_public_id(WORKSPACE_A, "issue_15138_remote_unit_board"),
            PINNED_ISSUE_LANE_UNIT_ID,
        )
        self.assertEqual(local_board().units[0].unit_id, PINNED_DEFAULT_LANE_UNIT_ID)

    def test_passing_the_local_host_id_explicitly_is_the_same_key(self) -> None:
        self.assertEqual(
            _unit_public_id(WORKSPACE_A, "default", LOCAL_HOST_ID),
            PINNED_DEFAULT_LANE_UNIT_ID,
        )

    def test_local_board_payload_matches_the_parent_shape(self) -> None:
        payload = local_board().as_payload()

        self.assertNotIn("sources", payload)
        self.assertEqual(
            sorted(payload["units"][0]), sorted(PARENT_ROW_PAYLOAD_KEYS)
        )
        self.assertEqual(
            sorted(payload["units"][0]["agents"][0]),
            ["interactive_ready", "provider", "runtime_state"],
        )

    def test_local_board_text_keeps_its_columns(self) -> None:
        text = format_board(local_board(), width=120)

        self.assertIn("PROJECT", text)
        self.assertIn("RESPONSIBILITY", text)
        self.assertIn("AGENTS", text)
        self.assertNotIn("source ", text)
        self.assertNotIn("[local]", text)

    def test_herdr_display_metadata_gains_no_new_token(self) -> None:
        # Metadata sync stays a local-only writer: the client never writes to
        # another server's panes, so the token set must not grow a host key.
        tokens, _ = metadata_for_unit(local_board().units[0])

        self.assertEqual(sorted(tokens), sorted(METADATA_TOKEN_KEYS))
        self.assertEqual(tokens["mozyo_unit"], PINNED_DEFAULT_LANE_UNIT_ID)

    def test_default_source_configuration_is_local_only(self) -> None:
        self.assertTrue(UnitBoardSourcesConfig.default().is_local_only)


if __name__ == "__main__":
    unittest.main()
