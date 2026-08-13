"""Redmine #15420 — the unit-board remote receiver follows the target's binding.

The remote Unit action rail composed ``project-gateway handoff --to codex`` on
the wire, while #15414 taught the receiving gateway to verify the requested
receiver against its OWN scope's ``provider_binding``. A claude-bound target
environment therefore refused every remote action on a constant. The client
cannot read the target's binding (privacy boundary, #15138), so the fix moves
the receiver decision to the target itself and makes the contract provable:

1. ``project-gateway handoff`` accepts an omitted ``--to`` and resolves the
   receiver from the scope's ``provider_binding`` (claude-bound and codex-bound
   alike); an explicit ``--to`` keeps the exact-match refusal;
2. the CLI advertises ``mozyo_gateway_handoff_capability=binding_receiver_v1``
   in its ``--help`` epilog, machine-readably (#13847's contract shape);
3. the remote action delivers with NO ``--to`` after probing that capability
   read-only, and an old remote without the advertisement is a typed zero-send
   refusal — never a silent fallback to a receiver pin.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (  # noqa: E402,E501
    cli_project_gateway,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_handoff_capability import (  # noqa: E402,E501
    CAPABILITY_BINDING_RECEIVER_V1,
    GATEWAY_HANDOFF_CAPABILITY_PREFIX,
    build_gateway_handoff_capability_epilog,
    gateway_handoff_capabilities,
    supports_binding_receiver,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.injection_stage import (  # noqa: E402,E501
    STAGE_NOT_SENT,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.remote_unit_action import (  # noqa: E402,E501
    ACTION_DELIVERED,
    ACTION_REFUSED,
    REASON_REMOTE_GATEWAY_CONTRACT_UNSUPPORTED,
)
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_remote_unit_action import (  # noqa: E402,E501
    GATEWAY_PROBE_ARGS,
    answers,
    gateway_probe_help,
    rail,
    remote_unit_id,
    request,
)

PROJECT = "giken-3800-mozyo-bridge"
REPO = "/repo/root"


def _handoff_args(to):
    return argparse.Namespace(
        to=to,
        target_repo=REPO,
        target_project=PROJECT,
        target=None,
        gateway_session=None,
        as_json=False,
    )


class OmittedReceiverResolvesTheBindingTest(unittest.TestCase):
    """Close conditions 1-3: omitted --to follows provider_binding; the
    explicit mismatch refusal is retained."""

    def _run_to_resolution(self, args, *, provider):
        with patch.object(
            cli_project_gateway, "_route_provider", return_value=provider
        ), patch.object(
            cli_project_gateway, "_route_from_args", return_value=object()
        ), patch.object(
            cli_project_gateway, "_discover_candidates", return_value=object()
        ), patch.object(
            cli_project_gateway,
            "resolve_project_gateway",
            return_value=SimpleNamespace(status="missing", selected=None),
        ), patch.object(
            cli_project_gateway, "render_gateway_resolution", return_value=0
        ):
            return cli_project_gateway.cmd_project_gateway_handoff(args)

    def test_omitted_to_resolves_the_claude_bound_gateway(self) -> None:
        args = _handoff_args(None)
        self._run_to_resolution(args, provider="claude")
        self.assertEqual(args.to, "claude")

    def test_omitted_to_resolves_the_codex_bound_gateway(self) -> None:
        args = _handoff_args(None)
        self._run_to_resolution(args, provider="codex")
        self.assertEqual(args.to, "codex")

    def test_an_explicitly_mismatched_receiver_still_dies(self) -> None:
        args = _handoff_args("codex")
        with patch.object(
            cli_project_gateway, "_route_provider", return_value="claude"
        ):
            with self.assertRaises(SystemExit) as caught:
                cli_project_gateway.cmd_project_gateway_handoff(args)
        message = str(caught.exception)
        self.assertIn("provider_binding", message)
        self.assertIn("`--to codex` is not allowed", message)
        self.assertIn("omit --to", message)


class CapabilityAdvertisementTest(unittest.TestCase):
    """Close condition 4 (producer half): the capability is advertised
    machine-readably, and the strict reader accepts exactly that shape."""

    def _help_text(self, *argv):
        from mozyo_bridge.application.cli import build_parser

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                build_parser().parse_args([*argv, "--help"])
        return out.getvalue()

    def test_handoff_help_advertises_the_binding_receiver_contract(self) -> None:
        text = self._help_text("project-gateway", "handoff")
        self.assertIn(build_gateway_handoff_capability_epilog(), text)
        # The REAL help output round-trips through the strict reader the
        # remote client uses — advertisement and probe cannot drift.
        self.assertTrue(supports_binding_receiver(text))

    def test_malformed_advertisements_prove_nothing(self) -> None:
        prefix = GATEWAY_HANDOFF_CAPABILITY_PREFIX
        for output in (
            None,
            b"bytes",
            "",
            prefix,  # empty token
            f"{prefix}Binding_Receiver_V1",  # not a canonical token
            f"{prefix}binding receiver v1",  # embedded whitespace
            f"note: {prefix}{CAPABILITY_BINDING_RECEIVER_V1}",  # not line-exact
        ):
            with self.subTest(output=output):
                self.assertFalse(supports_binding_receiver(output))
        self.assertEqual(
            gateway_handoff_capabilities(
                f"usage: x\n\n  {prefix}{CAPABILITY_BINDING_RECEIVER_V1}\n"
            ),
            frozenset({CAPABILITY_BINDING_RECEIVER_V1}),
        )


class RemoteActionVersionSkewTest(unittest.TestCase):
    """Close conditions 1, 4, 5 (client half): a capable remote gets a
    --to-free delivery; an old or unreadable remote is a typed zero-send."""

    def _deliveries(self, runner):
        return [
            argv
            for argv in runner.argvs
            if "project-gateway" in argv[-1] and "--help" not in argv[-1]
        ]

    def test_a_capable_remote_gets_a_receiver_free_delivery(self) -> None:
        action, runtime, runner = rail()
        result = action.apply(action.preview(request(remote_unit_id(runtime))))

        self.assertEqual(result.state, ACTION_DELIVERED)
        deliveries = self._deliveries(runner)
        self.assertEqual(len(deliveries), 1)
        self.assertNotIn("--to", deliveries[0][-1])

    def test_an_old_remote_is_refused_typed_with_nothing_sent(self) -> None:
        action, runtime, runner = rail(
            answers({GATEWAY_PROBE_ARGS: gateway_probe_help(capable=False)})
        )
        result = action.apply(action.preview(request(remote_unit_id(runtime))))

        self.assertEqual(result.state, ACTION_REFUSED)
        self.assertEqual(
            result.reason, REASON_REMOTE_GATEWAY_CONTRACT_UNSUPPORTED
        )
        self.assertEqual(result.injection_stage, STAGE_NOT_SENT)
        self.assertEqual(self._deliveries(runner), [])

    def test_an_unreadable_probe_is_refused_typed_with_nothing_sent(self) -> None:
        action, runtime, runner = rail(
            answers({GATEWAY_PROBE_ARGS: OSError("no route")})
        )
        result = action.apply(action.preview(request(remote_unit_id(runtime))))

        self.assertEqual(result.state, ACTION_REFUSED)
        self.assertEqual(
            result.reason, REASON_REMOTE_GATEWAY_CONTRACT_UNSUPPORTED
        )
        self.assertEqual(result.injection_stage, STAGE_NOT_SENT)
        self.assertEqual(self._deliveries(runner), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
