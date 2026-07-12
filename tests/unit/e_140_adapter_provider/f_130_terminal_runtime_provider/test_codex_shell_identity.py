from __future__ import annotations

import json
import unittest

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.codex_shell_identity import (
    CodexShellIdentity,
)


class CodexShellIdentityTest(unittest.TestCase):
    def test_renders_only_the_attested_identity_as_codex_overrides(self) -> None:
        argv = CodexShellIdentity(
            workspace_id="workspace-1",
            lane_id='lane-1"quoted',
        ).launch_argv()

        self.assertEqual(argv[::2], ("-c", "-c", "-c"))
        rendered = dict(item.split("=", 1) for item in argv[1::2])
        self.assertEqual(
            {
                key.removeprefix("shell_environment_policy.set."): json.loads(value)
                for key, value in rendered.items()
            },
            {
                "MOZYO_WORKSPACE_ID": "workspace-1",
                "MOZYO_AGENT_ROLE": "codex",
                "MOZYO_LANE_ID": 'lane-1"quoted',
            },
        )
        self.assertFalse(any("inherit" in item for item in argv))


if __name__ == "__main__":
    unittest.main()
