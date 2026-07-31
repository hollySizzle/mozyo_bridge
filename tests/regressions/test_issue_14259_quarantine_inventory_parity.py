"""Redmine #14259 — `quarantine-inspect` must read the live inventory `status` already reads.

Live evidence (issue description, 2026-07-22, isolated launcher `0.12.3a1`, one workspace):
``mozyo-bridge status --repo .`` enumerated 10 managed agents and named the #14187 / #14219
workers by locator, while ``sublane quarantine-inspect --issue <id> --lane <exact> --role claude``
answered ``classification=inventory_unreadable`` / ``receiver_present=null`` /
``approval_ready=false`` for both lanes — unchanged when ``--repo`` pointed at the lane worktree.
No raw Herdr / tmux / SQLite was involved on either side.

Cause (base `523cd76e`): :class:`SublaneQuarantineInspectUseCase` declared
``env: Optional[Mapping[str, str]] = field(default=None)`` and handed ``self.env`` straight to
``list_herdr_agent_rows``, whose first act is ``source_env.get(HERDR_BINARY_ENV)``. The live CLI
never passes ``env``, so **every** invocation raised ``AttributeError`` before Herdr was
contacted — and the use case's blanket ``except Exception`` reported that as an unreadable
inventory. It was therefore unconditional: independent of ``--repo``, of herdr reachability, and
of what was actually running. It was the only such record in the repo — every sibling live-ops
record either defaults ``env`` to the process environment or normalises ``None`` before use.

Why the pre-existing suite could not see it: every case in
``tests/unit/.../test_sublane_quarantine_inspect.py`` injects ``rows_reader``, which replaces the
exact seam that was broken. These tests drive the use case at its **default** construction and
fake the collaborator one level further out, at ``list_herdr_agent_rows``.

The second pin is diagnosability. The read collapsed every exception into the literal detail
``"inventory_unreadable"`` — the same three words for a misconfigured environment and for a
defect in this tool — which is why a live dogfood on two lanes could not localise it. A failed
read must now name its root cause from a closed vocabulary. Both defects were fixed by #14259,
so R3-c places both sets of pins in this one file.

Scope of this file (review j#94358 / verdict j#94361): every test below asserts the RECURRENCE
of one of those two symptoms. The module's public contract — which trusted environment an
explicitly-supplied ``env`` uses, the classifier's output shape, its ``__cause__`` walk, and the
standing value-non-exposure invariant — is asserted in
``tests/unit/.../test_sublane_quarantine_inspect.py`` instead, because R3-b makes ``regressions``
a FILE-level type whose every test must be recurrence detection.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import unittest
from pathlib import Path
from typing import Mapping
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    sublane_quarantine_inspect as inspect_module,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine_inspect import (  # noqa: E501
    INVENTORY_READ_INTERNAL_ERROR,
    INVENTORY_READ_PROVIDER_COMMAND_FAILED,
    INVENTORY_READ_REASONS,
    QuarantineInspectRequest,
    SublaneQuarantineInspectUseCase,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.quarantine_approval import (  # noqa: E501
    APPROVAL_INVENTORY_UNREADABLE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    TerminalTransportError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
    HERDR_BINARY_ENV,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
ISSUE = "14259"
LANE = "issue_14259_quarantine_inventory_parity"
ROLE = "claude"
NAME = encode_assigned_name(WS, ROLE, LANE)
LOCATOR = "w4B:p4J"
REVISION = 2

#: Stands for the operator-private material a raised read carries in its message: `_invoke`
#: re-raises a non-zero `herdr agent list` with that process's raw stderr, and a failed binary
#: resolution names the absolute path it tried.
SECRET_MESSAGE = "SECRET-STDERR /Users/private/path/herdr exited 3"


def _live_row() -> dict:
    return {"name": NAME, "pane_id": LOCATOR, "revision": REVISION}


class _Base(unittest.TestCase):
    """Drives the use case at its LIVE default: nothing is injected into the read seam."""

    def _run_with_inventory(self, rows_or_exc):
        """Fake the inventory at ``list_herdr_agent_rows`` — one level OUT from the broken seam.

        Faking further in (``rows_reader``) is what let the defect survive: that argument
        replaces the very call whose argument was wrong. Nothing is passed for ``env`` either,
        so the use case is always exercised at the construction the live CLI performs.

        The fake is deliberately **as strict about its argument as the real reader**: its first
        act is the same ``env.get(...)`` that ``resolve_herdr_binary`` performs. A lenient fake
        that shrugs at ``env=None`` would let every behavioural assertion below pass on the
        broken tree — which is precisely how a tolerant test double hid this defect through a
        live dogfood.
        """
        seen: dict = {}

        def _fake_list_rows(env):
            seen["env"] = env
            env.get(HERDR_BINARY_ENV)  # the real reader's first act; None fails here
            if isinstance(rows_or_exc, BaseException):
                raise rows_or_exc
            return rows_or_exc

        use_case = SublaneQuarantineInspectUseCase(repo_root=Path("/tmp/repo"))
        with mock.patch.object(inspect_module, "list_herdr_agent_rows", _fake_list_rows), \
                mock.patch.object(inspect_module, "repo_scope_workspace_id", lambda _root: WS):
            outcome = use_case.run(
                QuarantineInspectRequest(issue=ISSUE, lane=LANE, role=ROLE)
            )
        self.seen_env = seen.get("env", "<never called>")
        return outcome


class InventoryReadReachesTheBackendTest(_Base):
    """The read must actually happen — the #14259 failure was upstream of Herdr entirely."""

    def test_default_construction_hands_a_real_mapping_to_the_reader(self):
        # The exact defect: `None` reached `list_herdr_agent_rows`, whose first act is
        # `source_env.get(...)`. Pinning the ARGUMENT (not just the outcome) keeps this true
        # even if the surrounding refusal vocabulary is later reshaped.
        self._run_with_inventory([_live_row()])
        self.assertIsNotNone(self.seen_env)
        self.assertIsInstance(self.seen_env, Mapping)

    def test_default_env_is_the_process_environment(self):
        # Parity with `status` is only real if BOTH resolve the herdr binary from the same
        # trusted environment. A different-but-non-None default would still be a parity break.
        self._run_with_inventory([_live_row()])
        self.assertEqual(dict(self.seen_env), dict(os.environ))

    def test_env_field_cannot_default_to_none_again(self):
        field = {f.name: f for f in dataclasses.fields(SublaneQuarantineInspectUseCase)}["env"]
        self.assertIs(field.default, dataclasses.MISSING)
        self.assertIsNot(field.default_factory, dataclasses.MISSING)
        self.assertIsInstance(field.default_factory(), Mapping)

    def test_an_enumerable_lane_resolves_its_exact_receiver(self):
        # The end-to-end statement of the close condition: when the inventory carries the row
        # `status` shows, the public surface returns the exact tokens `--execute` demands.
        outcome = self._run_with_inventory([_live_row()])
        self.assertNotEqual(outcome.approval_reason, APPROVAL_INVENTORY_UNREADABLE)
        self.assertIs(outcome.receiver_present, True)
        self.assertEqual(outcome.facts.assigned_name, NAME)
        self.assertEqual(outcome.facts.locator, LOCATOR)
        self.assertEqual(outcome.facts.agent_revision, REVISION)
        self.assertEqual(
            outcome.facts.action_generation, f"quarantine:{LANE}:{ROLE}:{LOCATOR}"
        )


class UnreadableInventoryNamesItsCauseTest(_Base):
    """A collapsed reason is why two live lanes could not localise the fault."""

    def _detail_for(self, exc):
        outcome = self._run_with_inventory(exc)
        # The refusal itself is unchanged: unreadable stays fail-closed (close condition 4).
        self.assertEqual(outcome.approval_reason, APPROVAL_INVENTORY_UNREADABLE)
        self.assertTrue(outcome.is_blocked)
        self.assertEqual(outcome.approval_template, "")
        self.assertIn(outcome.inspection_detail, INVENTORY_READ_REASONS)
        return outcome

    def test_config_mismatch_is_named(self):
        exc = HerdrSessionStartError("no binary")
        exc.__cause__ = TerminalTransportError("x", reason=REASON_BINARY_UNCONFIGURED)
        self.assertEqual(
            self._detail_for(exc).inspection_detail, REASON_BINARY_UNCONFIGURED
        )

    def test_launcher_mismatch_is_named(self):
        exc = HerdrSessionStartError("missing")
        exc.__cause__ = TerminalTransportError("x", reason=REASON_BINARY_NOT_FOUND)
        self.assertEqual(self._detail_for(exc).inspection_detail, REASON_BINARY_NOT_FOUND)

    def test_provider_command_failure_is_named(self):
        outcome = self._detail_for(HerdrSessionStartError(SECRET_MESSAGE))
        self.assertEqual(
            outcome.inspection_detail, INVENTORY_READ_PROVIDER_COMMAND_FAILED
        )

    def test_a_defect_in_this_tool_is_not_reported_as_an_unreadable_environment(self):
        # THE #14259 pin. The original fault raised `AttributeError`; had it been distinguished
        # from an environmental failure, the dogfood would have localised it immediately
        # instead of reading as "herdr is unreachable" on both lanes.
        outcome = self._detail_for(AttributeError("'NoneType' object has no attribute 'get'"))
        self.assertEqual(outcome.inspection_detail, INVENTORY_READ_INTERNAL_ERROR)
        self.assertNotIn(
            outcome.inspection_detail,
            {REASON_BINARY_UNCONFIGURED, REASON_BINARY_NOT_FOUND,
             INVENTORY_READ_PROVIDER_COMMAND_FAILED},
        )

    def test_detail_no_longer_merely_repeats_the_refusal(self):
        # Pre-#14259 the detail was the literal string "inventory_unreadable", carrying no
        # information beyond `approval_reason`, which already said exactly that.
        for exc in (
            AttributeError("boom"),
            HerdrSessionStartError(SECRET_MESSAGE),
        ):
            with self.subTest(exc=type(exc).__name__):
                outcome = self._detail_for(exc)
                self.assertNotEqual(
                    outcome.inspection_detail, outcome.approval_reason
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
