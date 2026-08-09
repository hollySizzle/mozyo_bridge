"""Redmine #15147 — a routine resume from a known stop is four high-level steps.

The defect (#15147 description, owner intent j#101691): in #15140 the operator cleared a
known stop and said "continue". Before reaching the one high-level entrypoint that was
actually needed, the agent re-read the central preset body, the skill documents and repo
source, and collected several ``--help`` outputs. Nothing about that raised safety — the
stop cause was already known and every input the decision needed was on the durable
record. It only bought latency and context spend.

Causing commit: none. The standard was *absent* rather than regressed — the resume path
was never written down, so no commit removed it. The defect's provenance is the observed
operation history in #15140 j#101676, which this file pins as ``OBSERVED_15140_RESUME``.

The ruling lives in exactly one place, ``## 既知停止からの通常再開`` in the distributed
skill body (``skills/mozyo-bridge-agent/references/workflow.md`` plus its plugin mirror).
The same caution is deliberately NOT repeated into the central preset, the repo-local
rules or the role-profile templates — see ``## Workflow docs の正本境界`` (one rule, one
home) and the issue's implementation policy.

These tests are derivation-based, in the style of
``tests/regressions/test_issue_14665_gate_heading_canonical_literal.py``. The policy is
never re-listed here as free-standing prose:

* the ordered normal path is parsed out of the doc's own numbered step list, so the doc
  decides both the steps AND their order;
* the escalation conditions are parsed out of the doc's own numbered condition list, so
  the "detailed investigation is allowed only under these four" rule widens or narrows
  with the doc rather than with this file;
* the operations barred from the normal path are parsed out of the doc's own bullet list.

:class:`RoutineResumeOperationHistoryTest` is the representative operation-history test
the acceptance criteria ask for. It runs :func:`evaluate_resume_turn` — whose whole policy
input is the parsed doc — over representative resume turns, including the operation
history actually observed in #15140, and pins which ones the standard admits.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]

CANONICAL_WORKFLOW = (
    "skills",
    "mozyo-bridge-agent",
    "references",
    "workflow.md",
)
MIRROR_WORKFLOW = (
    "plugins",
    "mozyo-bridge-agent",
    "skills",
    "mozyo-bridge-agent",
    "references",
    "workflow.md",
)

SECTION_HEADING = "## 既知停止からの通常再開"
STEPS_HEADING = "### 通常再開の 4 step"
FORBIDDEN_HEADING = "### 通常再開で行わないこと"
ESCALATION_HEADING = "### 詳細調査へ移れる条件"
BOUNDARY_HEADING = "### 本標準が緩めない境界"


# --------------------------------------------------------------------------------------
# Doc parsing. Every policy input below is derived from the section body.
# --------------------------------------------------------------------------------------


def _section(body: str, heading: str) -> str:
    """Return the body of the top-level ``##`` section named ``heading``."""

    marker = f"\n{heading}\n"
    start = body.find(marker)
    if start < 0:
        raise AssertionError(f"workflow body is missing section {heading!r}")
    rest = body[start + len(marker) :]
    nxt = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _subsection(section: str, heading: str) -> str:
    marker = f"{heading}\n"
    start = section.find(marker)
    if start < 0:
        raise AssertionError(f"section is missing subsection {heading!r}")
    rest = section[start + len(marker) :]
    nxt = re.search(r"^#{3,4} ", rest, flags=re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _numbered_items(subsection: str) -> tuple[str, ...]:
    return tuple(
        m.group(1).strip()
        for m in re.finditer(r"^\d+\. (.+)$", subsection, flags=re.MULTILINE)
    )


def _bullet_items(subsection: str) -> tuple[str, ...]:
    return tuple(
        m.group(1).strip()
        for m in re.finditer(r"^- (.+)$", subsection, flags=re.MULTILINE)
    )


# Machine tokens are bound to the doc by a verbatim anchor phrase. A doc rewrite that
# renames a step, a condition or a barred operation fails the binding assertions below
# rather than silently detaching the evaluator from the standard.
STEP_ANCHORS: Mapping[str, str] = {
    "ticket_read": "最新の durable journal を読む",
    "high_level_status": "高レベルの状態確認を 1 回行う",
    "journal_add": "新しい判断を journal に記録する",
    "high_level_action": "高レベル入口から操作を 1 回実行する",
}

ESCALATION_ANCHORS: Mapping[str, str] = {
    "durable_state_conflict_or_unreadable": "durable state が競合・欠落・読取不能",
    "lane_unknown_or_ambiguous": "lane が不明または曖昧",
    "high_level_cli_unexpected_error": "高レベル CLI が想定外のエラーを返す",
    "destructive_privilege_or_secret": "破壊的操作、権限変更、秘密情報の利用が必要",
}

# Barred-operation anchors. These are the doc's *named* deviations. They are not the
# definition of what the normal path permits — the doc states the four steps are a closed
# set, so the evaluator default-denies anything else and uses these names only to produce
# a more specific violation label (#15147 review j#101748 finding 1).
FORBIDDEN_ANCHORS: Mapping[str, str] = {
    "rule_full_reread": "既読の central preset / skill reference の全文再読",
    "source_full_scan": "source 全文検索と文書全文 dump",
    "doc_full_dump": "source 全文検索と文書全文 dump",
    "help_lookup": "`--help` の収集",
    "raw_herdr_or_tmux": "raw Herdr / tmux 操作と低レベル",
    "low_level_pane_io": "raw Herdr / tmux 操作と低レベル",
    "duplicate_action_resend": "同一操作の再送",
}

CLOSED_SET_ANCHOR = "通常経路に属する操作は上の 4 step が **閉じた集合** である"
NAMED_LIST_IS_NOT_RESIDUE_ANCHOR = (
    "名指しの有無にかかわらず、4 step 以外の操作は通常経路では **選ばない**"
)
ESCALATION_RECORD_ANCHOR = (
    "**詳細調査の最初の操作より前に** durable record へ記録する。事後の記録は本条件を"
    "満たさない"
)
# The escalation record is a structured decision, not any journal that happens to be in
# the turn (#15147 review j#101761 finding 1).
ESCALATION_RECORD_CONTENT_ANCHOR = (
    "記録は **該当条件を名指しし**、何を確認するために何を読むかを含む"
)
ESCALATION_RECORD_NOT_GENERIC_ANCHOR = "通常の再開判断 journal はこの記録を兼ねない"
ESCALATION_KEEPS_STEP_ORDER_ANCHOR = (
    "**詳細調査の解禁は 4 step の順序を解除しない。**"
)
NO_FULL_REREAD_ANCHOR = (
    "同一 session の通常再開は、既に読んだ central preset / skill reference の全文再読を"
    " **要求しない**"
)


@dataclass(frozen=True)
class ResumePolicy:
    """The standard, as parsed out of the canonical section body."""

    normal_path: tuple[str, ...]
    escalation_conditions: frozenset[str]
    forbidden_kinds: frozenset[str]
    # True when the doc declares the four steps a closed set. The evaluator then
    # default-denies every other operation on the normal path instead of consulting
    # ``forbidden_kinds`` as an allow-open deny-list.
    normal_path_is_closed: bool
    # True when the doc requires the escalation decision to be recorded BEFORE the first
    # detailed-investigation operation, not merely somewhere in the turn.
    escalation_record_precedes_investigation: bool
    # True when the doc requires that record to name the applicable condition and the
    # read scope, so a generic resume journal cannot stand in for it.
    escalation_record_is_structured: bool
    # True when the doc says unlocking detailed investigation does NOT dissolve the
    # ordering among the four steps (no action before the decision is recorded).
    escalation_preserves_step_order: bool

    @classmethod
    def from_workflow_body(cls, body: str) -> "ResumePolicy":
        section = _section(body, SECTION_HEADING)

        steps = _numbered_items(_subsection(section, STEPS_HEADING))
        normal_path = tuple(_resolve(text, STEP_ANCHORS, "step") for text in steps)

        conditions = _numbered_items(_subsection(section, ESCALATION_HEADING))
        escalation = frozenset(
            _resolve(text, ESCALATION_ANCHORS, "escalation condition")
            for text in conditions
        )

        barred = _bullet_items(_subsection(section, FORBIDDEN_HEADING))
        forbidden: set[str] = set()
        for text in barred:
            forbidden.update(
                kind for kind, anchor in FORBIDDEN_ANCHORS.items() if anchor in text
            )

        return cls(
            normal_path=normal_path,
            escalation_conditions=escalation,
            forbidden_kinds=frozenset(forbidden),
            normal_path_is_closed=(
                CLOSED_SET_ANCHOR in section
                and NAMED_LIST_IS_NOT_RESIDUE_ANCHOR in section
            ),
            escalation_record_precedes_investigation=(
                ESCALATION_RECORD_ANCHOR in section
            ),
            escalation_record_is_structured=(
                ESCALATION_RECORD_CONTENT_ANCHOR in section
                and ESCALATION_RECORD_NOT_GENERIC_ANCHOR in section
            ),
            escalation_preserves_step_order=(
                ESCALATION_KEEPS_STEP_ORDER_ANCHOR in section
            ),
        )

    def pre_decision_steps(self) -> tuple[str, ...]:
        """The steps that may legitimately precede the decision record.

        Derived from the doc's own step order: everything before ``journal_add``. Every
        later operation — the high-level action AND any detailed investigation — is
        gated behind the record.
        """

        return self.normal_path[: self.normal_path.index("journal_add")]


def _resolve(text: str, anchors: Mapping[str, str], label: str) -> str:
    matched = [token for token, anchor in anchors.items() if anchor in text]
    if len(matched) != 1:
        raise AssertionError(
            f"{label} {text!r} matched {matched!r}; expected exactly one anchor. "
            "Either the doc renamed it or the anchor table needs an intentional "
            "update in the same commit."
        )
    return matched[0]


# --------------------------------------------------------------------------------------
# Operation-history evaluator. Its entire policy input is the parsed ResumePolicy.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumeOp:
    """One operation an agent performed during a resume turn.

    ``target`` identifies *what* a high-level action acted on, so a re-send of an
    already-delivered dispatch is distinguishable from a second, different action.

    ``declares_condition`` / ``read_scope`` are only meaningful on a ``journal_add``. They
    carry the two things the doc requires an escalation decision to state — which of the
    four conditions applies, and what will be read to check what. A journal that leaves
    them blank is an ordinary resume decision, not permission to investigate
    (#15147 review j#101761 finding 1).
    """

    kind: str
    target: str = ""
    declares_condition: str = ""
    read_scope: str = ""


@dataclass(frozen=True)
class ResumeVerdict:
    conforms: bool
    violations: tuple[str, ...] = field(default=())


def evaluate_resume_turn(
    history: Sequence[ResumeOp],
    *,
    policy: ResumePolicy,
    escalation: str | None = None,
) -> ResumeVerdict:
    """Classify one resume turn against the routine-resume standard.

    ``escalation`` is the condition the agent recorded before leaving the routine path.
    ``None`` means the turn claimed to be a routine resume. An unrecognised token fails
    closed: it does not unlock detailed investigation.
    """

    violations: list[str] = []

    escalated = escalation is not None and escalation in policy.escalation_conditions
    if escalation is not None and not escalated:
        violations.append(f"unknown_escalation_condition:{escalation}")

    # A blind re-send of an already-delivered operation is barred unconditionally: the
    # doc routes uncertain delivery to a fail-closed stop, not to a retry.
    seen_actions: set[str] = set()
    for op in history:
        if op.kind != "high_level_action":
            continue
        if op.target in seen_actions:
            violations.append(f"duplicate_action_resend:{op.target}")
        seen_actions.add(op.target)

    if escalated:
        # Detailed investigation is unlocked, but the decision to leave the routine path
        # is a durable-record obligation that PRECEDES it: the record is the permission
        # to investigate, not a summary of it. A generic resume journal does not qualify
        # — the record must name the applicable condition and the read scope.
        decision_at: int | None = None
        for i, op in enumerate(history):
            if op.kind != "journal_add":
                continue
            if not policy.escalation_record_is_structured:
                decision_at = i
                break
            if not op.declares_condition:
                continue
            if op.declares_condition != escalation:
                violations.append(
                    f"escalation_decision_condition_mismatch:{op.declares_condition}"
                )
                continue
            if not op.read_scope:
                violations.append("escalation_decision_missing_read_scope")
                continue
            decision_at = i
            break

        # Everything after step 2 is gated behind the record: the detailed investigation
        # AND the high-level action. Unlocking investigation does not dissolve the order
        # among the four steps.
        gated_kinds = (
            policy.pre_decision_steps()
            if policy.escalation_preserves_step_order
            else policy.normal_path
        )
        first_gated = next(
            (i for i, op in enumerate(history) if op.kind not in gated_kinds), None
        )

        if decision_at is None:
            violations.append("escalation_decision_not_recorded")
        elif (
            policy.escalation_record_precedes_investigation
            and first_gated is not None
            and decision_at > first_gated
        ):
            violations.append(
                f"escalation_decision_after:{history[first_gated].kind}"
            )
        return ResumeVerdict(conforms=not violations, violations=tuple(violations))

    # Normal path: the four steps are a closed set, so anything else is a violation
    # whether or not the doc names it. Named deviations get the more specific label.
    for op in history:
        if op.kind in policy.normal_path:
            continue
        if op.kind in policy.forbidden_kinds:
            violations.append(f"forbidden_in_normal_path:{op.kind}")
        elif policy.normal_path_is_closed:
            violations.append(f"outside_closed_normal_path:{op.kind}")

    observed = tuple(op.kind for op in history if op.kind in policy.normal_path)
    if observed != policy.normal_path:
        violations.append(
            f"normal_path_mismatch:{'>'.join(observed) or '(none)'}"
            f"!={'>'.join(policy.normal_path)}"
        )

    return ResumeVerdict(conforms=not violations, violations=tuple(violations))


# --------------------------------------------------------------------------------------
# Representative resume turns.
# --------------------------------------------------------------------------------------

# The #15140 resume, done the way the standard asks for.
ROUTINE_RESUME = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

# Even a single `--help` is outside the closed four-step set (#15147 review j#101748
# finding 1: the acceptance criterion bars "複数の --help", but the owner intent and the
# canonical doc say the normal path is the four steps and nothing else).
ROUTINE_RESUME_WITH_ONE_HELP = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("help_lookup", "sublane dispatch-worker --help"),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

# An operation the doc never names. A deny-list would let it through; a closed set does
# not (#15147 review j#101748 finding 1).
ROUTINE_RESUME_WITH_UNNAMED_DETOUR = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("web_search", "how to dispatch a sublane worker"),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

# The operation history actually observed in #15140 (j#101676): a broad re-read before
# the single high-level entrypoint that was all the turn needed.
OBSERVED_15140_RESUME = (
    ResumeOp("ticket_read", "#15140 j#101676"),
    ResumeOp("rule_full_reread", "central preset agent-workflow.md"),
    ResumeOp("rule_full_reread", "skills/.../workflow.md"),
    ResumeOp("source_full_scan", "grep -r src/"),
    ResumeOp("doc_full_dump", "vibes/docs/logics/*.md"),
    ResumeOp("help_lookup", "sublane --help"),
    ResumeOp("help_lookup", "handoff --help"),
    ResumeOp("help_lookup", "workflow --help"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("journal_add", "#15140 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

RAW_TRANSPORT_RESUME = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("raw_herdr_or_tmux", "herdr agent read"),
    ResumeOp("low_level_pane_io", "mozyo-bridge type %41"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

RESENT_ACTION_RESUME = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

REPEATED_STATUS_RESUME = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("high_level_status", "workflow glance --repo ."),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

ACTION_BEFORE_JOURNAL_RESUME = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
    ResumeOp("journal_add", "#15147 resume decision"),
)

UNREAD_RESUME = (
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("journal_add", "#15147 resume decision"),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
)

ESCALATION_CONDITION = "lane_unknown_or_ambiguous"


def _escalation_decision(condition: str = ESCALATION_CONDITION) -> ResumeOp:
    """A journal that states the condition AND what will be read to check what."""

    return ResumeOp(
        "journal_add",
        "#15147 escalation decision",
        declares_condition=condition,
        read_scope="lane resolution: sublane list output + worktree binding",
    )


# A structured escalation decision recorded BEFORE anything it unlocks.
ESCALATED_INVESTIGATION = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    _escalation_decision(),
    ResumeOp("source_full_scan", "grep -r src/"),
    ResumeOp("doc_full_dump", "vibes/docs/logics/*.md"),
    ResumeOp("help_lookup", "sublane --help"),
    ResumeOp("help_lookup", "handoff --help"),
    ResumeOp("raw_herdr_or_tmux", "herdr agent read"),
)

# The same operations, with the decision written up afterwards. The record is then a
# summary of the investigation rather than the permission for it (#15147 review j#101748
# finding 2).
ESCALATION_RECORDED_AFTERWARDS = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("source_full_scan", "grep -r src/"),
    ResumeOp("doc_full_dump", "vibes/docs/logics/*.md"),
    ResumeOp("raw_herdr_or_tmux", "herdr agent read"),
    _escalation_decision(),
)

# The two histories #15147 review j#101761 finding 1 reported as wrongly conforming.
# (1) An ordinary resume journal standing in for an escalation decision.
GENERIC_JOURNAL_THEN_INVESTIGATION = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("journal_add", "resume decision only"),
    ResumeOp("source_full_scan", "grep -r src/"),
)

# (2) The high-level action taken before the decision was recorded. Escalation unlocks
# investigation, not a reordering of the four steps.
ESCALATED_ACTION_BEFORE_JOURNAL = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
    _escalation_decision(),
)

# A decision naming a condition other than the one the turn escalated under.
ESCALATION_CONDITION_MISMATCH = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    _escalation_decision("destructive_privilege_or_secret"),
    ResumeOp("source_full_scan", "grep -r src/"),
)

# A decision that names the condition but never says what it will read.
ESCALATION_WITHOUT_READ_SCOPE = (
    ResumeOp("ticket_read", "#15147 j#101693"),
    ResumeOp("high_level_status", "sublane list --repo ."),
    ResumeOp(
        "journal_add",
        "#15147 escalation decision",
        declares_condition=ESCALATION_CONDITION,
    ),
    ResumeOp("source_full_scan", "grep -r src/"),
)


def _bodies() -> tuple[tuple[str, str], ...]:
    return (
        ("skills/mozyo-bridge-agent/references/workflow.md", ROOT.joinpath(*CANONICAL_WORKFLOW).read_text(encoding="utf-8")),
        (
            "plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md",
            ROOT.joinpath(*MIRROR_WORKFLOW).read_text(encoding="utf-8"),
        ),
    )


class RoutineResumeStandardDocTest(unittest.TestCase):
    """The standard exists, has one home, and is parseable into a policy."""

    def test_canonical_and_mirror_carry_the_standard(self) -> None:
        for label, body in _bodies():
            with self.subTest(surface=label):
                section = _section(body, SECTION_HEADING)
                for heading in (
                    STEPS_HEADING,
                    FORBIDDEN_HEADING,
                    ESCALATION_HEADING,
                    BOUNDARY_HEADING,
                ):
                    self.assertIn(heading, section, msg=f"{label} lost {heading!r}")
                for anchor in (
                    NO_FULL_REREAD_ANCHOR,
                    ESCALATION_RECORD_ANCHOR,
                    CLOSED_SET_ANCHOR,
                    NAMED_LIST_IS_NOT_RESIDUE_ANCHOR,
                    ESCALATION_RECORD_CONTENT_ANCHOR,
                    ESCALATION_RECORD_NOT_GENERIC_ANCHOR,
                    ESCALATION_KEEPS_STEP_ORDER_ANCHOR,
                ):
                    self.assertIn(anchor, section, msg=f"{label} lost {anchor!r}")

    def test_canonical_and_mirror_parse_to_the_same_policy(self) -> None:
        policies = {label: ResumePolicy.from_workflow_body(body) for label, body in _bodies()}
        self.assertEqual(len(set(policies.values())), 1, msg=f"policy drift: {policies}")

    def test_normal_path_is_the_four_ordered_steps(self) -> None:
        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertEqual(
            policy.normal_path,
            ("ticket_read", "high_level_status", "journal_add", "high_level_action"),
        )

    def test_detailed_investigation_has_exactly_four_conditions(self) -> None:
        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertEqual(policy.escalation_conditions, frozenset(ESCALATION_ANCHORS))

    def test_normal_path_bars_the_observed_15140_operations(self) -> None:
        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertEqual(policy.forbidden_kinds, frozenset(FORBIDDEN_ANCHORS))

    def test_normal_path_is_a_closed_set_not_a_deny_list(self) -> None:
        """#15147 review j#101748 finding 1: the named list is not the permitted residue."""

        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertTrue(policy.normal_path_is_closed)

    def test_escalation_record_must_precede_the_investigation(self) -> None:
        """#15147 review j#101748 finding 2: the record is permission, not a summary."""

        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertTrue(policy.escalation_record_precedes_investigation)

    def test_escalation_record_is_a_structured_decision(self) -> None:
        """#15147 review j#101761 finding 1: a generic journal is not permission."""

        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertTrue(policy.escalation_record_is_structured)

    def test_escalation_does_not_dissolve_the_step_order(self) -> None:
        """#15147 review j#101761 finding 1: step 3 still precedes step 4."""

        policy = ResumePolicy.from_workflow_body(_bodies()[0][1])
        self.assertTrue(policy.escalation_preserves_step_order)
        self.assertEqual(
            policy.pre_decision_steps(), ("ticket_read", "high_level_status")
        )

    def test_standard_is_not_duplicated_into_other_rule_surfaces(self) -> None:
        """One rule, one home (#15147 implementation policy / ``## Workflow docs の正本境界``)."""

        for rel in (
            ".mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md",
            "vibes/docs/rules/agent-workflow.md",
            "src/mozyo_bridge/e_110_execution_platform/f_130_handoff_routing/domain/role_profile_templates.yaml",
        ):
            path = ROOT / rel
            if not path.exists():
                continue
            with self.subTest(surface=rel):
                self.assertNotIn(
                    SECTION_HEADING.lstrip("# "),
                    path.read_text(encoding="utf-8"),
                    msg=(
                        f"{rel} re-states the routine-resume standard; it belongs only in "
                        "the distributed skill body."
                    ),
                )


class RoutineResumeOperationHistoryTest(unittest.TestCase):
    """Representative operation histories, classified against the parsed standard."""

    def setUp(self) -> None:
        self.policy = ResumePolicy.from_workflow_body(_bodies()[0][1])

    def _verdict(self, history, *, escalation=None) -> ResumeVerdict:
        return evaluate_resume_turn(history, policy=self.policy, escalation=escalation)

    def test_routine_resume_is_ticket_read_status_journal_action(self) -> None:
        verdict = self._verdict(ROUTINE_RESUME)
        self.assertTrue(verdict.conforms, msg=verdict.violations)

    def test_even_a_single_help_lookup_leaves_the_normal_path(self) -> None:
        verdict = self._verdict(ROUTINE_RESUME_WITH_ONE_HELP)
        self.assertFalse(verdict.conforms)
        self.assertIn("forbidden_in_normal_path:help_lookup", verdict.violations)

    def test_an_operation_the_doc_never_names_is_still_rejected(self) -> None:
        """A deny-list would admit this; a closed set does not."""

        verdict = self._verdict(ROUTINE_RESUME_WITH_UNNAMED_DETOUR)
        self.assertFalse(verdict.conforms)
        self.assertIn("outside_closed_normal_path:web_search", verdict.violations)

    def test_observed_15140_resume_is_rejected(self) -> None:
        verdict = self._verdict(OBSERVED_15140_RESUME)
        self.assertFalse(verdict.conforms)
        self.assertIn("forbidden_in_normal_path:rule_full_reread", verdict.violations)
        self.assertIn("forbidden_in_normal_path:source_full_scan", verdict.violations)
        self.assertIn("forbidden_in_normal_path:doc_full_dump", verdict.violations)
        self.assertEqual(
            3,
            sum(1 for v in verdict.violations if v == "forbidden_in_normal_path:help_lookup"),
            msg=verdict.violations,
        )

    def test_raw_transport_operations_are_rejected(self) -> None:
        verdict = self._verdict(RAW_TRANSPORT_RESUME)
        self.assertFalse(verdict.conforms)
        self.assertIn("forbidden_in_normal_path:raw_herdr_or_tmux", verdict.violations)
        self.assertIn("forbidden_in_normal_path:low_level_pane_io", verdict.violations)

    def test_resending_a_delivered_action_is_rejected(self) -> None:
        verdict = self._verdict(RESENT_ACTION_RESUME)
        self.assertFalse(verdict.conforms)
        self.assertIn(
            "duplicate_action_resend:sublane dispatch-worker --execute",
            verdict.violations,
        )

    def test_second_status_check_is_rejected(self) -> None:
        verdict = self._verdict(REPEATED_STATUS_RESUME)
        self.assertFalse(verdict.conforms)
        self.assertTrue(
            any(v.startswith("normal_path_mismatch:") for v in verdict.violations),
            msg=verdict.violations,
        )

    def test_acting_before_recording_the_decision_is_rejected(self) -> None:
        verdict = self._verdict(ACTION_BEFORE_JOURNAL_RESUME)
        self.assertFalse(verdict.conforms)
        self.assertTrue(
            any(v.startswith("normal_path_mismatch:") for v in verdict.violations),
            msg=verdict.violations,
        )

    def test_resuming_without_reading_the_durable_journal_is_rejected(self) -> None:
        verdict = self._verdict(UNREAD_RESUME)
        self.assertFalse(verdict.conforms)
        self.assertTrue(
            any(v.startswith("normal_path_mismatch:") for v in verdict.violations),
            msg=verdict.violations,
        )

    def test_each_documented_condition_unlocks_detailed_investigation(self) -> None:
        for condition in sorted(self.policy.escalation_conditions):
            with self.subTest(condition=condition):
                history = tuple(
                    _escalation_decision(condition) if op.kind == "journal_add" else op
                    for op in ESCALATED_INVESTIGATION
                )
                verdict = self._verdict(history, escalation=condition)
                self.assertTrue(verdict.conforms, msg=verdict.violations)

    def test_undocumented_condition_does_not_unlock_investigation(self) -> None:
        verdict = self._verdict(ESCALATED_INVESTIGATION, escalation="just_to_be_safe")
        self.assertFalse(verdict.conforms)
        self.assertIn(
            "unknown_escalation_condition:just_to_be_safe", verdict.violations
        )
        self.assertIn("forbidden_in_normal_path:source_full_scan", verdict.violations)

    def test_escalation_must_be_recorded_on_the_durable_record(self) -> None:
        unrecorded = tuple(op for op in ESCALATED_INVESTIGATION if op.kind != "journal_add")
        verdict = self._verdict(unrecorded, escalation=ESCALATION_CONDITION)
        self.assertFalse(verdict.conforms)
        self.assertIn("escalation_decision_not_recorded", verdict.violations)

    def test_recording_the_escalation_afterwards_is_rejected(self) -> None:
        verdict = self._verdict(
            ESCALATION_RECORDED_AFTERWARDS, escalation=ESCALATION_CONDITION
        )
        self.assertFalse(verdict.conforms)
        self.assertIn("escalation_decision_after:source_full_scan", verdict.violations)

    def test_a_generic_resume_journal_is_not_an_escalation_decision(self) -> None:
        """#15147 review j#101761 finding 1, reported history (1)."""

        verdict = self._verdict(
            GENERIC_JOURNAL_THEN_INVESTIGATION, escalation=ESCALATION_CONDITION
        )
        self.assertFalse(verdict.conforms)
        self.assertIn("escalation_decision_not_recorded", verdict.violations)

    def test_escalation_does_not_permit_acting_before_recording(self) -> None:
        """#15147 review j#101761 finding 1, reported history (2)."""

        verdict = self._verdict(
            ESCALATED_ACTION_BEFORE_JOURNAL, escalation=ESCALATION_CONDITION
        )
        self.assertFalse(verdict.conforms)
        self.assertIn("escalation_decision_after:high_level_action", verdict.violations)

    def test_decision_naming_a_different_condition_is_rejected(self) -> None:
        verdict = self._verdict(
            ESCALATION_CONDITION_MISMATCH, escalation=ESCALATION_CONDITION
        )
        self.assertFalse(verdict.conforms)
        self.assertIn(
            "escalation_decision_condition_mismatch:destructive_privilege_or_secret",
            verdict.violations,
        )

    def test_decision_without_a_read_scope_is_rejected(self) -> None:
        verdict = self._verdict(
            ESCALATION_WITHOUT_READ_SCOPE, escalation=ESCALATION_CONDITION
        )
        self.assertFalse(verdict.conforms)
        self.assertIn("escalation_decision_missing_read_scope", verdict.violations)

    def test_escalation_still_does_not_permit_a_blind_resend(self) -> None:
        history = ESCALATED_INVESTIGATION + (
            ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
            ResumeOp("high_level_action", "sublane dispatch-worker --execute"),
        )
        verdict = self._verdict(history, escalation=ESCALATION_CONDITION)
        self.assertFalse(verdict.conforms)
        self.assertIn(
            "duplicate_action_resend:sublane dispatch-worker --execute",
            verdict.violations,
        )


if __name__ == "__main__":
    unittest.main()
