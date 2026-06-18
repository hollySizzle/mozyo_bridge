"""Skill workflow guidance / semantic-anchor tests (Redmine #12148, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of SkillCrossWorkspaceGuidanceTest and
SkillWorkflowSemanticAnchorsTest out of the monolithic test spine, per
#12145 Priority 2 and vibes/docs/logics/refactor-split-strategy.md. No test
logic changed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class SkillCrossWorkspaceGuidanceTest(unittest.TestCase):
    """Pin the cross-workspace gateway contract in the skill body.

    Updated for Redmine #11301. The earlier #10332 wording said the
    `cross_session_claude` recovery path must always name `--mode standard`
    (or `--mode pending`) because the default `queue-enter` rail rejected
    every cross-session target. Since #11301 that is no longer true: the
    default queue-enter rail admits a cross-session `--to codex` gateway
    send under a constrained identity gate (an explicit `--target` PLUS a
    passing `--target-repo`). `--mode standard` / `--mode pending` are now
    fallbacks, not a requirement. This test pins the new contract — gateway
    target form with `--target-repo`, the constrained-admission wording, and
    the non-git scaffold identity root — so a future rule-edit cannot
    silently revert to the stale "always pass --mode" guidance.
    """

    REQUIRED_GUIDANCE_MARKERS = (
        # Cross-Workspace Handoff section heading must be present.
        "## Cross-Workspace Handoff",
        # The gateway target form must stay copy-pasteable and now carries
        # the workspace identity gate that admits the send on the default
        # rail.
        "--to codex --target <target_session>:codex --target-repo",
        # The constrained cross-session admission contract (Redmine #11301).
        "constrained identity gate",
        "no `--mode` needed",
        # `--mode standard` / `--mode pending` must read as a fallback, not
        # as mandatory-because-queue-enter-rejects-all-cross-session.
        "remain available as fallbacks",
        # A scaffolded non-git workspace is a first-class identity root.
        ".mozyo-bridge/scaffold.json",
    )

    def _skill_workflow_body(self, *parts: str) -> str:
        return (ROOT.joinpath(*parts) / "references" / "workflow.md").read_text(
            encoding="utf-8"
        )

    def test_canonical_skill_keeps_constrained_gateway_guidance(self) -> None:
        body = self._skill_workflow_body("skills", "mozyo-bridge-agent")
        for marker in self.REQUIRED_GUIDANCE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"skills/mozyo-bridge-agent/references/workflow.md is "
                    f"missing #11301 marker {marker!r}; cross-workspace "
                    f"gateway guidance regressed to stale wording."
                ),
            )

    def test_plugin_mirror_keeps_constrained_gateway_guidance(self) -> None:
        body = self._skill_workflow_body(
            "plugins", "mozyo-bridge-agent", "skills", "mozyo-bridge-agent"
        )
        for marker in self.REQUIRED_GUIDANCE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"plugin skill mirror is missing #11301 marker "
                    f"{marker!r}; sync_plugin_skill.sh drift or upstream "
                    f"canonical regressed."
                ),
            )


class SkillWorkflowSemanticAnchorsTest(unittest.TestCase):
    """Pin Redmine #10663: broaden semantic anchors beyond #10332.

    `PluginMarketplaceTest::test_plugin_skill_mirror_matches_canonical`
    detects *byte* drift between the canonical skill body and the
    plugin mirror. `SkillCrossWorkspaceGuidanceTest` pins the #10332
    cross-workspace marker subset.

    This class extends the semantic anchor set to cover the rest of
    the workflow body's load-bearing sections — handoff lifecycle,
    role boundary, Codex direct-edit gate, autonomous lane, audit-
    owned commit authority, workflow-change verification. A future
    canonical edit that quietly drops one of these sections passes the
    byte-drift gate (canonical + mirror in sync) but would still need
    to clear this test, so a single missing marker fails CI loudly.

    Markers are deliberately verbatim substrings from the canonical
    body. Wording changes that intentionally rename a section MUST
    update this list in the same commit; the explicit failure surfaces
    the intent.
    """

    SECTION_MARKERS: tuple[str, ...] = (
        # Major section headings — drop any of these and the workflow
        # body has lost a primary topic.
        "## Start Of Work",
        "## Ticket-ID Entrypoint",
        "## Ticket System Conventions",
        "## Handoff Lifecycle",
        "## Cross-Workspace Handoff",
        "## Same-Lane Claude Dispatch",
        "## Sublane Coordinator Callback",
        "## Named Cockpit Groups And Multiple Local Cockpit Sessions",
        "## Coordinator Stop And Next-Action Standard",
        "## Owner Approval Aggregation",
        "## Stall And No-Progress Detection Standard",
        "## Claude / Codex Role Boundary",
        "## Policy / Skill Authoring Boundary",
        "### Repo-Local Guardrail Autonomous Lane",
        "## Audit-Owned Commit Authority",
        "## Workflow Change Verification",
    )

    PHRASE_MARKERS: tuple[str, ...] = (
        # Role boundary — Claude implements, Codex audits, and the
        # gateway can't be reframed by short imperatives.
        "Claude owns implementation for normal development tasks",
        "Codex does not directly implement normal development tasks",
        "are not by themselves authorization for Codex to perform a direct edit",
        # Codex direct-edit gate vocabulary (Redmine path).
        "`codex_direct_edit` gate journal",
        "role: 実装者",
        "direct_edit: true",
        "allowed_paths",
        # Autonomous lane — the carve-out and its required journal.
        "Repo-Local Guardrail Autonomous Lane",
        "codex_autonomous_edit",
        "vibes/docs/rules/**",
        "vibes/docs/logics/**",
        "vibes/docs/specs/**",
        # Audit-owned commit authority — close approval separation
        # and the per-system commit message contracts must stay
        # verbatim so operators can copy-paste them.
        "Audit-Owned Commit Authority",
        "Codex audit-owned commit",
        "Refs: Redmine #<issue_id>",
        "Journal: <journal_id>",
        "Refs: Asana task <task_id>",
        "Audit: Asana comment <comment_id>",
        # Close-Approval-Separation reminder pulled from the central
        # preset is the load-bearing distinction between Review Gate
        # and Close Gate.
        "Review approval alone is not close approval",
        "owner close approval journal",
        # Handoff Lifecycle vocabulary — durable record is the source
        # of truth, pane is a pointer.
        "the durable source of truth",
        "pane notification is still only the pointer",
        # Sublane coordinator callback (Redmine #11852). A sublane must
        # report handoff-worthy states back to the coordinator lane's
        # Codex with a durable anchor, cross-lane Codex-to-Codex, so the
        # work does not look stalled from the coordinator cockpit.
        "send a concise callback to the coordinator lane",
        "owner close approval requested",
        "The sublane's Codex owns the cross-lane callback",
        # Same-lane Claude dispatch submit-completion (Redmine #12207). A
        # same-lane Codex→Claude dispatch is a standard handoff that must reach
        # submit (queue-enter on an active split, marker-observed `--mode
        # standard` on an inactive cockpit-grid split); `--no-submit` /
        # `--mode pending` stays an explicit operator/debug fallback, not the
        # standard dispatch path.
        "that dispatch is a **standard handoff and must complete the submit**",
        "Inactive-split Claude pane uses marker-observed `--mode standard`",
        "`--no-submit` / `--mode pending` is not the standard dispatch path",
        # Named cockpit groups — grouping vs identity separation
        # (Redmine #11853). A multi-cockpit layout must not become an
        # implicit cross-group send shortcut, and the cross-group rail
        # must route through the target group's Codex gateway.
        "A **cockpit group is a named tmux session**",
        "not the routing or identity source of truth",
        "route it through the **target group's Codex** pane",
        "Multiple cockpit sessions do not create a cross-session Claude shortcut",
        # Coordinator stop and next-action standard (Redmine #11860). Every
        # coordinator stop records a durable reason plus a three-part
        # next-action proposal and returns ready work to the queue, without
        # relaxing Close Approval Separation or self-authorizing a carve-out.
        "make every stop carry a next-action proposal",
        "A stop is justified only when the *only* remaining next actions are in the owner-approval range",
        "A next-action proposal is not self-authorization",
        "Hand gated work back to the queue, not to a held pane",
        # Owner approval aggregation (Redmine #11867). Owner-approval-waiting
        # always converges on the single main coordinator Codex, is never
        # resolved inside the sublane, and the waiting queue is enumerable
        # from the durable record independent of pane count.
        "The single owner-facing aggregation point is the main coordinator Codex",
        "A sublane never resolves owner approval inside its own lane",
        "owner-action-needed",
        "the owner-approval-waiting set is a property of the durable record, enumerable from the durable record, not by scanning panes",
        "Aggregation is not self-authorization",
        # Stall and no-progress detection (Redmine #11880). The coordinator
        # defines a stall candidate from the durable record, classifies it into
        # four states, treats a stale CLI as a distinct callback-delivery
        # failure, and records every stall check and re-notification.
        "A **stall candidate is a unit of work whose handoff was delivered but whose expected next durable journal has not appeared**",
        "`no_progress_after_handoff`",
        "`progress_without_callback`",
        "`callback_delivery_failed`",
        "`callback_not_attempted`",
        "Stale CLI is a distinct stall mode during a handoff or callback",
        "it records that fact on the issue",
        "Detection is not re-dispatch of completed work",
        # Workflow Change Verification policy.
        "Workflow Change Verification",
        "Claude implements the normal development task",
        # Redmine default-project resolution (Redmine #10689). The
        # workspace-local snippet path and the "explicit wins over
        # default" / "UNVERIFIED escalates" rules must stay in the
        # skill body so agents pick them up at session start.
        "Default project resolution",
        ".mozyo-bridge/redmine-defaults.md",
        ".mozyo-bridge/project-defaults.yaml",
        "An explicit `project_id` always wins over the default",
        "UNVERIFIED",
    )

    SKILL_PATH = (
        "skills",
        "mozyo-bridge-agent",
        "references",
        "workflow.md",
    )
    PLUGIN_MIRROR_PATH = (
        "plugins",
        "mozyo-bridge-agent",
        "skills",
        "mozyo-bridge-agent",
        "references",
        "workflow.md",
    )

    def _body(self, *parts: str) -> str:
        return ROOT.joinpath(*parts).read_text(encoding="utf-8")

    def _check_markers(self, body: str, *, label: str) -> None:
        for marker in self.SECTION_MARKERS + self.PHRASE_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        f"{label} is missing workflow semantic anchor "
                        f"{marker!r}. Either the canonical skill body lost a "
                        f"load-bearing section / phrase, or this anchor list "
                        f"needs an intentional update in the same commit."
                    ),
                )

    def test_canonical_skill_carries_workflow_semantic_anchors(self) -> None:
        self._check_markers(
            self._body(*self.SKILL_PATH),
            label="skills/mozyo-bridge-agent/references/workflow.md",
        )

    def test_plugin_mirror_carries_workflow_semantic_anchors(self) -> None:
        self._check_markers(
            self._body(*self.PLUGIN_MIRROR_PATH),
            label="plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/references/workflow.md",
        )


class SameLaneDispatchDurableDocTest(unittest.TestCase):
    """Pin the same-lane dispatch submit-completion contract in the durable
    operating-model doc (Redmine #12207).

    `vibes/docs/logics/cockpit-sublane-operating-model.md` is the durable
    operating-model source of truth (a Repo-Local Guardrail Autonomous Lane
    doc). It must carry the same submit-completion contract the skill body
    pins, so an agent reading the operating model — not just the skill — learns
    that a same-lane dispatch reaches submit and does not rest at a pending
    prompt. A future edit that drops the section fails here loudly rather than
    silently reopening the #12207 stall.
    """

    DOC_PATH = (
        "vibes",
        "docs",
        "logics",
        "cockpit-sublane-operating-model.md",
    )

    REQUIRED_MARKERS: tuple[str, ...] = (
        "## Same-Lane Claude Dispatch の Submit 完結",
        "標準 handoff であり、submit まで完結する",
        # The inactive-split case routes to the submit-completing standard rail.
        "recovery command どおり `--mode standard --target",
        # The pending fallbacks are explicitly not the standard dispatch path.
        "`--no-submit` / `--mode pending` は標準 dispatch 経路ではない",
        # The non-goals are spelled out: no gate relaxation, no blind Enter.
        "blind Enter も導入しない",
        # The reproduction anchor stays cited.
        "#12207",
    )

    def test_operating_model_doc_carries_same_lane_dispatch_contract(self) -> None:
        body = ROOT.joinpath(*self.DOC_PATH).read_text(encoding="utf-8")
        for marker in self.REQUIRED_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        "vibes/docs/logics/cockpit-sublane-operating-model.md is "
                        f"missing #12207 same-lane dispatch marker {marker!r}; "
                        "the submit-completion contract regressed or this anchor "
                        "list needs an intentional update in the same commit."
                    ),
                )


if __name__ == "__main__":
    unittest.main()
