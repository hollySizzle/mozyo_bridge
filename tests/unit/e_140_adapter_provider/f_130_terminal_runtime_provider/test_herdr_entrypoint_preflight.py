"""Specs for the shared herdr-backend entrypoint preflight helper (Redmine #13446).

Pure / config-only behaviour of the helper the standard entrypoints share: herdr backend
detection (fail-open to tmux on a broken/absent config), the herdr-native lane-identity env
snapshot (looked at *first*, before any tmux `%pane` read), and the standard-dispatch
guidance vocabulary. No test here spawns a live herdr binary or reads the process env
implicitly — env is always injected.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
    herdr_entrypoint_preflight as pre,
)


class LaneEnvSnapshotTest(unittest.TestCase):
    def test_keys_are_the_four_herdr_native_identity_vars_in_order(self):
        self.assertEqual(
            pre.HERDR_LANE_ENV_KEYS,
            (
                "HERDR_PANE_ID",
                "MOZYO_WORKSPACE_ID",
                "MOZYO_AGENT_ROLE",
                "MOZYO_LANE_ID",
            ),
        )

    def test_snapshot_marks_non_empty_values_present(self):
        env = {
            "HERDR_PANE_ID": "p-1",
            "MOZYO_WORKSPACE_ID": "ws",
            "MOZYO_AGENT_ROLE": "",  # empty -> absent
            # MOZYO_LANE_ID unset -> absent
        }
        snap = pre.herdr_lane_env_snapshot(env)
        self.assertEqual(
            snap,
            {
                "HERDR_PANE_ID": True,
                "MOZYO_WORKSPACE_ID": True,
                "MOZYO_AGENT_ROLE": False,
                "MOZYO_LANE_ID": False,
            },
        )

    def test_whitespace_only_value_is_absent(self):
        snap = pre.herdr_lane_env_snapshot({"HERDR_PANE_ID": "   "})
        self.assertFalse(snap["HERDR_PANE_ID"])

    def test_detail_renders_present_absent_and_never_leaks_values(self):
        detail = pre.herdr_lane_env_detail(
            {"HERDR_PANE_ID": "secret-pane", "MOZYO_LANE_ID": "lane-x"}
        )
        self.assertIn("HERDR_PANE_ID=present", detail)
        self.assertIn("MOZYO_AGENT_ROLE=absent", detail)
        self.assertIn("MOZYO_LANE_ID=present", detail)
        # Only presence is reported; no env value is printed.
        self.assertNotIn("secret-pane", detail)
        self.assertNotIn("lane-x", detail)


class GuidanceTest(unittest.TestCase):
    def test_guidance_leads_with_the_active_marker(self):
        text = pre.herdr_backend_guidance()
        self.assertTrue(text.startswith(pre.HERDR_BACKEND_ACTIVE_MARKER))

    def test_guidance_names_standard_dispatch_and_demotes_tmux_primitives(self):
        text = pre.herdr_backend_guidance()
        self.assertIn("sublane create --execute", text)
        self.assertIn("sublane start --execute", text)
        # The tmux-era surfaces are explicitly demoted, not offered as the entrypoint.
        self.assertIn("primitive/debug/compat", text)
        self.assertIn("agents targets", text)
        self.assertIn("handoff send", text)


class BackendActiveTest(unittest.TestCase):
    def test_delegates_to_herdr_backend_selected_for(self):
        with patch.object(pre, "herdr_backend_selected_for", return_value=True) as m:
            self.assertTrue(pre.herdr_backend_active(Path("/repo")))
        m.assert_called_once()

    def test_broken_config_resolves_to_tmux_default(self):
        with patch.object(
            pre, "herdr_backend_selected_for", side_effect=ValueError("boom")
        ):
            self.assertFalse(pre.herdr_backend_active(Path("/repo")))

    def test_tmux_backend_repo_without_herdr_config_is_not_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A bare directory with no `.mozyo-bridge/config.yaml` resolves to the tmux
            # default through the real config reader (fail-open), never herdr.
            self.assertFalse(pre.herdr_backend_active(Path(tmp)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
