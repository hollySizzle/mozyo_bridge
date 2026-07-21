"""CLI-level leaf-admission parity: plan vs dry-run vs execute (Redmine #14224 / j#84882).

The #14224 leaf-lane admission fence was first wired into ONE of the two request
construction sites. ``sublane_lifecycle_command._build_create_request`` (the plan-only
surface) carried ``leaf_standalone``; ``sublane_actuator.cmd_sublane_start`` (the
``--dry-run`` / ``--execute`` surface) did not — so the SAME argv
(``--work-unit leaf_issue --leaf-standalone``) planned successfully but then blocked on
``work_unit_leaf_decision_required`` at actuation. An exact-installed black-box run
caught it (j#84882); the original test suite did not, because every #14224 test built a
``SublaneCreateRequest`` **directly** and so never exercised the argv -> request mapping
where the field was dropped.

These tests close exactly that layer: they drive the REAL parser (``cli.build_parser``)
so a future construction site that forgets the field fails here rather than only in an
installed artifact. They assert the *admission verdict*, never a live actuation — the
handler is stopped at its existing provider-preflight seam (the same hermetic technique
``test_provider_consumer_r3_corrections`` uses), and the pure decision is read off the
request the parser produced.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_lifecycle_command import (  # noqa: E402,E501
    _build_create_request,
    resolve_work_unit_request_fields,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.work_unit_granularity import (  # noqa: E402,E501
    WORK_UNIT_LEAF_DECISION_RECORDED,
    WORK_UNIT_LEAF_DECISION_REQUIRED,
    WORK_UNIT_LEAF_STANDALONE,
)

_BASE_ARGV = [
    "sublane",
    "create",
    "--issue",
    "14224",
    "--lane-label",
    "issue_14224_x",
    "--branch",
    "b",
    "--worktree",
    "/wt/14224",
    "--journal",
    "9",
]


def _parse(extra_argv, *, dry_run=False):
    """Parse the same user-facing argv through the REAL parser.

    ``dry_run`` appends the flag that routes ``sublane create`` down the actuation
    handler instead of the plan-only surface — the two surfaces j#84882 found disagreeing
    on identical leaf flags.
    """
    import mozyo_bridge.application.cli as cli

    argv = [*_BASE_ARGV, *extra_argv]
    if dry_run:
        argv.append("--dry-run")
    return cli.build_parser().parse_args(argv)


def _plan_decision(extra_argv, repo_root):
    """The admission verdict the PLAN-only surface derives from this argv."""
    args = _parse(extra_argv)
    work_unit, anchor = resolve_work_unit_request_fields(args, repo_root)
    request = _build_create_request(
        args, work_unit=work_unit, work_unit_decision_anchor=anchor
    )
    return request.work_unit_decision()


class _StopBeforeActuation(Exception):
    """Unwinds the real handler once its request is captured (no side effect runs)."""


def _actuation_decision(extra_argv, repo_root):
    """The admission verdict the REAL ``cmd_sublane_start`` derives from the same args.

    Drives the actual handler and captures the request IT built, rather than mirroring
    its construction here — a mirror would let the production site drop a field again
    while this test kept passing against its own copy, which is precisely how j#84882
    escaped the original suite. The handler is intercepted at its use-case boundary, so
    the request is real but nothing is actuated: no ops port runs, no worktree or pane is
    touched, and the handler unwinds before any side effect.
    """
    import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator as act  # noqa: E501

    args = _parse(extra_argv, dry_run=True)
    captured = {}

    class _CapturingUseCase:
        def __init__(self, *a, **kw):
            pass

        def run(self, request, **kw):
            captured["request"] = request
            raise _StopBeforeActuation()

    args.repo = str(repo_root)
    orig_use_case = act.SublaneActuateUseCase
    act.SublaneActuateUseCase = _CapturingUseCase
    try:
        act.cmd_sublane_start(args)
    except _StopBeforeActuation:
        pass
    finally:
        act.SublaneActuateUseCase = orig_use_case
    if "request" not in captured:
        raise AssertionError(
            "cmd_sublane_start returned before building its request; the admission "
            "input could not be observed"
        )
    return captured["request"].work_unit_decision()


class LeafAdmissionCliParityTest(unittest.TestCase):
    """The same argv must reach the same leaf admission verdict on every surface."""

    def setUp(self) -> None:
        # A repo root with no .mozyo-bridge/config.yaml -> the user_story default, so the
        # verdicts below are driven purely by the parsed flags.
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)

    def test_parser_exposes_leaf_standalone_on_the_namespace(self) -> None:
        # The flag must exist and default False (fail-closed) -- if the parser stopped
        # emitting it, every getattr(..., False) fallback would silently block standalone
        # leaves instead of failing loudly.
        self.assertFalse(_parse([]).leaf_standalone)
        self.assertTrue(_parse(["--leaf-standalone"]).leaf_standalone)

    def test_standalone_leaf_is_allowed_on_both_surfaces(self) -> None:
        # j#84882's exact reproduction: this argv planned OK but blocked at actuation.
        extra = ["--work-unit", "leaf_issue", "--leaf-standalone"]
        plan = _plan_decision(extra, self.repo_root)
        actuation = _actuation_decision(extra, self.repo_root)
        self.assertTrue(plan.is_allowed)
        self.assertTrue(actuation.is_allowed)
        self.assertEqual(plan.diagnostic, WORK_UNIT_LEAF_STANDALONE)
        self.assertEqual(actuation.diagnostic, WORK_UNIT_LEAF_STANDALONE)

    def test_child_leaf_without_decision_blocks_on_both_surfaces(self) -> None:
        extra = ["--work-unit", "leaf_issue"]
        plan = _plan_decision(extra, self.repo_root)
        actuation = _actuation_decision(extra, self.repo_root)
        self.assertFalse(plan.is_allowed)
        self.assertFalse(actuation.is_allowed)
        self.assertEqual(plan.diagnostic, WORK_UNIT_LEAF_DECISION_REQUIRED)
        self.assertEqual(actuation.diagnostic, WORK_UNIT_LEAF_DECISION_REQUIRED)

    def test_child_leaf_with_decision_anchor_is_allowed_on_both_surfaces(self) -> None:
        extra = ["--work-unit", "leaf_issue", "--work-unit-decision-journal", "70719"]
        plan = _plan_decision(extra, self.repo_root)
        actuation = _actuation_decision(extra, self.repo_root)
        self.assertTrue(plan.is_allowed)
        self.assertTrue(actuation.is_allowed)
        self.assertEqual(plan.diagnostic, WORK_UNIT_LEAF_DECISION_RECORDED)
        self.assertEqual(actuation.diagnostic, WORK_UNIT_LEAF_DECISION_RECORDED)

    def test_every_leaf_flag_combination_agrees_across_surfaces(self) -> None:
        # The general invariant, not just the three cases above: whatever the flags, the
        # two surfaces must never disagree. A third construction site that forgets the
        # field would fail here even if nobody wrote a case for its exact flag mix.
        combos = [
            [],
            ["--leaf-standalone"],
            ["--work-unit", "leaf_issue"],
            ["--work-unit", "leaf_issue", "--leaf-standalone"],
            ["--work-unit", "leaf_issue", "--work-unit-decision-journal", "70719"],
            [
                "--work-unit",
                "leaf_issue",
                "--leaf-standalone",
                "--work-unit-decision-journal",
                "70719",
            ],
            ["--work-unit", "user_story"],
            ["--work-unit", "epic", "--work-unit-decision-journal", "70719"],
        ]
        for extra in combos:
            with self.subTest(argv=" ".join(extra) or "<none>"):
                plan = _plan_decision(extra, self.repo_root)
                actuation = _actuation_decision(extra, self.repo_root)
                self.assertEqual(plan.status, actuation.status)
                self.assertEqual(plan.diagnostic, actuation.diagnostic)
                self.assertEqual(plan.decision_anchor, actuation.decision_anchor)


if __name__ == "__main__":
    unittest.main()
