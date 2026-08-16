"""Redmine #15201 — `sublane declare-project-gateway` stays on the selected backend.

The defect as filed: with `terminal_transport.backend: herdr`, the declaration read
its live pair from the Herdr `agent list` but matched its route through an ARGLESS
`_discover_candidates()`, whose compatibility branch walks the tmux candidate
discovery — so on a Herdr-only host (no tmux server) the dry-run could not even
finish, and the two halves of one declaration consumed two different inventories.

The crash path was closed as a side effect of #15414 (`01e3b26a`): `resolve_route`
now feeds `repo_root` / `project_scope` / `provider` to the discovery, which routes
to the backend-aware `discover_project_gateway_inventory`. What #15201 still owed —
and what this suite pins — is the issue's own completion conditions, none of which
any existing test asserted:

1. the declare route NEVER reaches the tmux compatibility branch (an argless-call
   regression is observable here, not just on a tmux-less host);
2. route matching receives the same project scope and the same resolved gateway
   provider the use case feeds to the declaration write — one backend, one scope;
3. the argless compatibility seam itself keeps its legacy tmux behaviour for the
   callers that predate backend-aware discovery;
4. no live gateway candidate resolves to an owner-unbound zero-write, never a
   guess.

The Herdr-only live smoke (condition 5) is runtime evidence, recorded on the
issue journal rather than simulated here: a real `mozyo-bridge sublane
declare-project-gateway` dry-run with tmux absent from PATH completed with a
typed zero-write refusal instead of dying in tmux discovery.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_project_gateway_declare import (  # noqa: E402,E501
    LiveProjectGatewayDeclareOps,
)


def _resolve_route(ops, scope="proj-scope", provider="codex"):
    return ops.resolve_route(scope, provider)


class DeclareRouteStaysOnTheSelectedBackendTest(unittest.TestCase):
    """Conditions 1 + 2: one backend, one scope, and no tmux branch — observably."""

    def _run_with_recorders(self, discovered):
        ops = LiveProjectGatewayDeclareOps(repo_root=ROOT)
        with patch(
            "mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution."
            "application.cli_project_gateway_resolve.discover_project_gateway_inventory",
            return_value=discovered,
        ) as backend_discovery:
            with patch(
                "mozyo_bridge.e_110_execution_platform."
                "f_120_agent_discovery_pane_resolution.application."
                "cli_project_gateway_resolve._agents_target_candidates"
            ) as tmux_branch:
                result = _resolve_route(ops)
        return result, backend_discovery, tmux_branch

    def test_the_tmux_compatibility_branch_is_never_reached(self) -> None:
        # The regression this issue was filed about: an argless call would skip
        # the backend-aware discovery and land in the tmux branch. Both sides
        # are asserted, so EITHER symptom of the argless form turns this red —
        # the tmux walk happening, or the backend discovery not happening.
        _result, backend_discovery, tmux_branch = self._run_with_recorders([])

        self.assertEqual(0, tmux_branch.call_count)
        self.assertEqual(1, backend_discovery.call_count)

    def test_route_matching_gets_the_declares_own_scope_and_provider(self) -> None:
        # Condition 2: the route half consumes the SAME project scope the
        # declaration owns and the SAME provider the use case resolved — not a
        # default, not a second read.
        _result, backend_discovery, _tmux = self._run_with_recorders([])

        kwargs = backend_discovery.call_args.kwargs
        self.assertEqual("proj-scope", kwargs["project_scope"])
        self.assertEqual("codex", kwargs["provider"])
        self.assertEqual(str(ROOT), kwargs["repo_root"])

    def test_no_live_candidate_is_an_owner_unbound_route_not_a_guess(self) -> None:
        # Condition 4 (no-candidate half): an empty inventory yields no observed
        # route; the declaration then fails closed owner-unbound instead of
        # adopting something that was never matched.
        (expected_root, _path, observed), _d, _t = self._run_with_recorders([])

        self.assertIsNone(observed)
        # The DECLARED identity is still resolved — the refusal names what was
        # looked for, not a blank.
        self.assertEqual(str(ROOT), expected_root)


class ArglessSeamKeepsLegacyBehaviourTest(unittest.TestCase):
    """Condition 3: the compatibility seam predating backend-aware discovery."""

    def test_an_argless_call_still_routes_to_the_legacy_discovery(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import (  # noqa: E501
            cli_project_gateway_resolve as resolve_module,
        )

        with patch.object(
            resolve_module, "_agents_target_candidates", return_value=[]
        ) as legacy:
            result = resolve_module._discover_candidates()

        self.assertEqual([], result)
        self.assertEqual(1, legacy.call_count)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
