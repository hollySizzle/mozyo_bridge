"""Redmine #15475 — the remote Unit action carries the sender repo context.

The delivered ``project-gateway handoff`` runs on the target host in a
non-interactive shell whose cwd is $HOME, where no repo-local
``terminal_transport`` config exists. Without an explicit sender repo the
transport wiring resolved the sender to the tmux default, refused the
sender/target cross-backend join in under a second with a plain-text die, and
the client could only classify the unreadable outcome as ``uncertain_partial``
— every rc4 live apply (#15470 j#105118 / j#105223 / j#105249) was in fact a
zero-send typed refusal presented as an unretriable uncertainty.

The fix threads the root ``--repo <workspace.canonical_path>`` through both the
capability probe and the delivery argv: for a cross-host Unit action the sender
ON the target host IS the target environment, so the sender repo is the same
checkout the delivery targets. The probe carries the option so a remote whose
root parser predates it dies in argparse as a typed zero-send refusal, never
mid-delivery.
"""

from __future__ import annotations

import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.remote_unit_action import (  # noqa: E402,E501
    ACTION_DELIVERED,
)
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_remote_unit_action import (  # noqa: E402,E501
    rail,
    remote_unit_id,
    request,
)

#: The canonical checkout the fixture workspace declares on the remote host.
CANONICAL_PATH = "/srv/checkouts/mozyo_bridge"


def _remote_commands(runner) -> list[list[str]]:
    """Each remote invocation's command tokens (the shell-quoted last argv)."""
    return [
        shlex.split(argv[-1])
        for argv in runner.argvs
        if "project-gateway" in argv[-1]
    ]


def _assert_root_repo_precedes_subcommand(case, tokens: list[str]) -> None:
    """``--repo <canonical>`` must sit in root-option position."""
    case.assertIn("--repo", tokens)
    repo_at = tokens.index("--repo")
    case.assertEqual(tokens[repo_at + 1], CANONICAL_PATH)
    case.assertLess(repo_at, tokens.index("project-gateway"))


class SenderRepoContextTest(unittest.TestCase):
    def test_the_delivery_carries_the_root_repo_context(self) -> None:
        action, runtime, runner = rail()
        result = action.apply(action.preview(request(remote_unit_id(runtime))))

        self.assertEqual(result.state, ACTION_DELIVERED)
        deliveries = [
            tokens
            for tokens in _remote_commands(runner)
            if "--help" not in tokens
        ]
        self.assertEqual(len(deliveries), 1)
        _assert_root_repo_precedes_subcommand(self, deliveries[0])

    def test_the_capability_probe_carries_the_same_root_repo(self) -> None:
        action, runtime, runner = rail()
        action.apply(action.preview(request(remote_unit_id(runtime))))

        probes = [
            tokens
            for tokens in _remote_commands(runner)
            if "--help" in tokens
        ]
        self.assertEqual(len(probes), 1)
        _assert_root_repo_precedes_subcommand(self, probes[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
