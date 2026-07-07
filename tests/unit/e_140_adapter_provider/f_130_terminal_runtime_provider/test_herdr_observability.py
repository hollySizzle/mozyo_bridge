"""Specs for the shared herdr operator-observability read model (#13355).

Pure-projection and fail-closed inventory-read behaviour, exercised with fake
listers / temp repos only — no test here spawns a live herdr binary (the #13359
lesson: an observability surface must never leak a live read into the suite).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_observability import (  # noqa: E501
    HerdrInventoryView,
    project_observed_agents,
    herdr_backend_selected_for,
    read_herdr_inventory,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (
    RUNTIME_BUSY,
    RUNTIME_TURN_ENDED,
    RUNTIME_UNKNOWN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    REASON_BINARY_UNCONFIGURED,
    REASON_TRANSPORT_ERROR,
    TerminalTransportError,
)


def _herdr_repo(tmp: str) -> Path:
    repo = Path(tmp) / "repo"
    (repo / ".mozyo-bridge").mkdir(parents=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text(
        "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    return repo


class FakeLister:
    def __init__(self, rows=None, error: TerminalTransportError | None = None):
        self._rows = rows or []
        self._error = error
        self.calls = 0

    def list_agent_rows(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._rows


class ProjectObservedAgentsTest(unittest.TestCase):
    def test_managed_row_decodes_slot_state_and_locator(self) -> None:
        name = encode_assigned_name("ws-a", "claude", "")
        rows = [{"name": name, "agent_status": "working", "pane_id": "%7"}]

        (agent,) = project_observed_agents(rows)

        self.assertTrue(agent.managed)
        self.assertEqual("ws-a", agent.workspace_id)
        self.assertEqual("default", agent.lane_id)
        self.assertEqual("claude", agent.role)
        self.assertEqual(RUNTIME_BUSY, agent.runtime_state)
        self.assertEqual("working", agent.raw_status)
        self.assertEqual("%7", agent.locator)
        self.assertIsNone(agent.decode_reason)

    def test_done_maps_to_turn_ended_not_workflow_done(self) -> None:
        name = encode_assigned_name("ws-a", "codex", "")
        (agent,) = project_observed_agents([{"name": name, "agent_status": "done"}])
        self.assertEqual(RUNTIME_TURN_ENDED, agent.runtime_state)

    def test_foreign_name_is_kept_as_unmanaged_with_reason(self) -> None:
        (agent,) = project_observed_agents(
            [{"name": "random-agent", "status": "idle", "pane": "%3"}]
        )
        self.assertFalse(agent.managed)
        self.assertEqual("random-agent", agent.name)
        self.assertIsNotNone(agent.decode_reason)
        self.assertEqual("%3", agent.locator)

    def test_missing_status_fails_closed_to_unknown(self) -> None:
        name = encode_assigned_name("ws-a", "claude", "")
        (agent,) = project_observed_agents([{"name": name}])
        self.assertEqual(RUNTIME_UNKNOWN, agent.runtime_state)
        self.assertEqual("", agent.raw_status)

    def test_non_mapping_rows_are_skipped(self) -> None:
        self.assertEqual((), project_observed_agents(["junk", 42, None]))

    def test_to_record_is_json_shaped(self) -> None:
        name = encode_assigned_name("ws-a", "claude", "")
        (agent,) = project_observed_agents([{"name": name, "agent_status": "idle"}])
        record = agent.to_record()
        self.assertEqual(name, record["name"])
        self.assertTrue(record["managed"])
        self.assertEqual("awaiting_input", record["runtime_state"])


class ReadHerdrInventoryTest(unittest.TestCase):
    def test_unselected_backend_reads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            lister = FakeLister()

            view = read_herdr_inventory(repo, lister=lister)

        self.assertFalse(view.backend_selected)
        self.assertEqual(0, lister.calls)
        self.assertFalse(herdr_backend_selected_for(repo))

    def test_selected_backend_with_fake_lister_projects_rows(self) -> None:
        name = encode_assigned_name("ws-a", "claude", "")
        with tempfile.TemporaryDirectory() as tmp:
            repo = _herdr_repo(tmp)
            view = read_herdr_inventory(
                repo,
                lister=FakeLister(rows=[{"name": name, "agent_status": "working"}]),
            )

        self.assertTrue(view.backend_selected)
        self.assertTrue(view.ok)
        self.assertEqual(1, len(view.managed_agents))
        # A standalone temp checkout has no registry anchor: the segment is
        # empty and workspace_agents() is therefore empty (identity gap, not a
        # crash).
        self.assertEqual("", view.workspace_segment)
        self.assertEqual((), view.workspace_agents())

    def test_transport_failure_is_a_structured_fail_not_a_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _herdr_repo(tmp)
            view = read_herdr_inventory(
                repo,
                lister=FakeLister(
                    error=TerminalTransportError(
                        "herdr agent list timed out", reason=REASON_TRANSPORT_ERROR
                    )
                ),
            )

        self.assertTrue(view.backend_selected)
        self.assertFalse(view.ok)
        self.assertEqual(REASON_TRANSPORT_ERROR, view.reason)
        self.assertIn("timed out", view.detail)

    def test_unconfigured_binary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _herdr_repo(tmp)
            # No injected lister and an empty trusted env: the discovery
            # resolver must fail closed (binary_unconfigured), carried as a
            # structured failure view.
            view = read_herdr_inventory(repo, env={})

        self.assertTrue(view.backend_selected)
        self.assertFalse(view.ok)
        self.assertEqual(REASON_BINARY_UNCONFIGURED, view.reason)

    def test_workspace_agents_filters_on_segment(self) -> None:
        ours = encode_assigned_name("ws-a", "claude", "")
        other = encode_assigned_name("ws-b", "codex", "")
        view = HerdrInventoryView(
            backend_selected=True,
            ok=True,
            workspace_segment="ws-a",
            agents=project_observed_agents(
                [
                    {"name": ours, "agent_status": "idle"},
                    {"name": other, "agent_status": "working"},
                    {"name": "foreign", "agent_status": "idle"},
                ]
            ),
        )
        self.assertEqual(2, len(view.managed_agents))
        self.assertEqual(1, len(view.unmanaged_agents))
        (workspace_agent,) = view.workspace_agents()
        self.assertEqual("ws-a", workspace_agent.workspace_id)


if __name__ == "__main__":
    unittest.main()
