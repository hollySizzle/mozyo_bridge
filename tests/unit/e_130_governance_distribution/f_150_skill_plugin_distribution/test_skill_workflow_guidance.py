"""Skill workflow guidance / semantic-anchor tests (Redmine #12148, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of SkillCrossWorkspaceGuidanceTest and
SkillWorkflowSemanticAnchorsTest out of the monolithic test spine, per
#12145 Priority 2 and vibes/docs/logics/refactor-split-strategy.md. No test
logic changed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
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
        # (Markers pin the Japanese skill body since Redmine #13050.)
        "## Workspace 横断 handoff",
        # The gateway target form must stay copy-pasteable and now carries
        # the workspace identity gate that admits the send on the default
        # rail.
        "--to codex --target <target_session>:codex --target-repo",
        # The constrained cross-session admission contract (Redmine #11301).
        "制約付き identity gate",
        "`--mode` 不要で default rail 上で動く",
        # `--mode standard` / `--mode pending` must read as a fallback, not
        # as mandatory-because-queue-enter-rejects-all-cross-session.
        "fallback として引き続き利用できる",
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
        # body has lost a primary topic. Headings are pinned verbatim
        # against the Japanese skill body (Redmine #13050 translation).
        "## 作業開始",
        "## Ticket-ID 入口",
        "## Ticket システム運用規約",
        "## Handoff ライフサイクル",
        "## Workspace 横断 handoff",
        "## 同一レーン Claude dispatch",
        "## Sublane の coordinator callback",
        "## 名前付き cockpit group と複数 local cockpit session",
        "## Coordinator stop と next-action 標準",
        "## Owner 承認の集約",
        "## Stall / no-progress 検出標準",
        "## Sublane 完了 guardrail",
        "## Sublane 退役 drain",
        "## Dispatch 後の fill loop",
        "## 既存 project の sublane 導入",
        "## Claude / Codex 役割境界",
        "## Policy / skill authoring 境界",
        "### Repo-Local Guardrail Autonomous Lane",
        "## Audit-Owned Commit Authority",
        "## Workflow 変更の反映確認 (Workflow Change Verification)",
        # Redmine #13029 upstream of the repo-local core workflow: spec-decision
        # routing, design-consultation firing, backlog reconciliation, delegated
        # coordinator role model (incl. grandchild dispatch), and the narrative
        # issue-labeling rule move into the distributed body.
        "## 仕様決定 routing",
        "## Design Consultation 発火判断",
        "## Backlog reconciliation gate (deferred intent の即時 durable 分類)",
        "## 委譲コーディネータ role model (delegated coordinator)",
        "### 孫 dispatch / context 保護",
        "### 固定 role profile template",
        "### 実装者 escalation trigger (Claude → Codex)",
        # Redmine #13060 upstream of the medium-priority operational
        # doctrines: the ACK / delivery / completion separation and the
        # runtime-fingerprint verification discipline.
        "## ACK / delivery / completion の分離",
        "## Runtime fingerprint 検証規律",
        # Redmine #13489 (owner intent j#74953): the agent wait / polling
        # efficiency standard, plus its four load-bearing sub-sections.
        "## Wait / polling 効率標準",
        "### blocking wait を token 消費と誤認しない",
        "### bounded wait は user commentary SLA 内に収める",
        "### timeout / state 不変時に pane history を掘らない",
        "### 通常 finding は gate journal にまとめ、即時 interrupt は Critical に限定する",
        # Redmine #13518 (owner intent j#75078): dispatch 後は LLM turn を
        # zero-wait で終了し、通常運用は mozyo facade only、raw Herdr/tmux は
        # operator debug に限る。45–55 秒 cadence は LLM turn ではなく
        # background watcher / operator debug へ再帰属した。
        "### dispatch / handoff 後は LLM turn を zero-wait で終了する",
    )

    PHRASE_MARKERS: tuple[str, ...] = (
        # Prose markers are verbatim substrings of the Japanese skill
        # body (Redmine #13050 translation); literal tokens stay as-is.
        # Role boundary — Claude implements, Codex audits, and the
        # gateway can't be reframed by short imperatives.
        "通常開発タスクの実装は Claude が所有する",
        "Codex は `mozyo_bridge` の通常開発タスクを直接実装しない",
        "それだけでは Codex が direct edit を行う authorization にはならない",
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
        "Codex による audit-owned commit",
        "Refs: Redmine #<issue_id>",
        "Journal: <journal_id>",
        "Refs: Asana task <task_id>",
        "Audit: Asana comment <comment_id>",
        # Close-Approval-Separation reminder pulled from the central
        # preset is the load-bearing distinction between Review Gate
        # and Close Gate.
        "review approval 単独は close approval ではない",
        "owner close approval journal",
        # Handoff Lifecycle vocabulary — durable record is the source
        # of truth, pane is a pointer.
        "まず durable な source of truth を記録するか特定する",
        "pane 通知は依然として pointer でしかない",
        # Sublane coordinator callback (Redmine #11852). A sublane must
        # report handoff-worthy states back to the coordinator lane's
        # Codex with a durable anchor, cross-lane Codex-to-Codex, so the
        # work does not look stalled from the coordinator cockpit.
        "coordinator lane へ簡潔な callback を送らなければならない",
        "owner close approval requested",
        "lane 横断 callback は sublane の Codex が所有する",
        # Same-lane Claude dispatch submit-completion (Redmine #12207, updated for
        # the Redmine #12597 standard_target_admission rail). A same-lane
        # Codex→Claude dispatch is a standard handoff that must reach submit
        # (queue-enter on an active split, or an inactive registered split
        # auto-activated by standard_target_admission; marker-observed `--mode
        # standard` only for an unadmitted inactive cockpit-grid split);
        # `--no-submit` / `--mode pending` stays an explicit operator/debug
        # fallback, not the standard dispatch path.
        "その dispatch は **標準 handoff であり、submit を完了しなければならない**",
        "inactive-split の Claude pane: standard_target_admission が登録済み pane を activate する。`--mode standard` が必要なのは未 admit の pane のみである",
        "`--no-submit` / `--mode pending` は標準の dispatch path ではない",
        # Named cockpit groups — grouping vs identity separation
        # (Redmine #11853). A multi-cockpit layout must not become an
        # implicit cross-group send shortcut, and the cross-group rail
        # must route through the target group's Codex gateway.
        "**cockpit group は名前付き tmux session である**",
        "routing や identity の正本ではない",
        "**target group の Codex** pane を経由させ",
        "複数 cockpit session は session 横断 Claude shortcut を作らない",
        # Coordinator stop and next-action standard (Redmine #11860). Every
        # coordinator stop records a durable reason plus a three-part
        # next-action proposal and returns ready work to the queue, without
        # relaxing Close Approval Separation or self-authorizing a carve-out.
        "すべての stop に next-action 提案を持たせ",
        "stop が正当化されるのは、残る next action が owner 承認範囲*のみ*になったときだけである",
        "next-action 提案は self-authorization ではない",
        "gate された作業は、保持した pane ではなく queue に返す",
        # Owner approval aggregation (Redmine #11867). Owner-approval-waiting
        # always converges on the single main coordinator Codex, is never
        # resolved inside the sublane, and the waiting queue is enumerable
        # from the durable record independent of pane count.
        "owner 窓口の集約点は main coordinator Codex の一点である",
        "sublane は owner 承認を自 lane 内で決して解決しない",
        "owner-action-needed",
        "owner-approval-waiting 集合は durable record の性質であり、durable record から列挙できる。pane の走査によってではない",
        "集約は self-authorization ではない",
        # Stall and no-progress detection (Redmine #11880). The coordinator
        # defines a stall candidate from the durable record, classifies it into
        # four states, treats a stale CLI as a distinct callback-delivery
        # failure, and records every stall check and re-notification.
        "**stall candidate とは、handoff は delivery されたが、期待される次の durable journal が operator の許容 window 内に現れていない作業単位**",
        "`no_progress_after_handoff`",
        "`progress_without_callback`",
        "`callback_delivery_failed`",
        "`callback_not_attempted`",
        "stale CLI は handoff / callback 中の独立した stall mode である",
        "その事実を issue 上に記録する",
        "検出は完了済み作業の re-dispatch ではない",
        # Sublane completion guardrails (Redmine #12213). A handoff-worthy
        # state is incomplete until its callback outcome journal lands, a
        # dependency hold parks on the durable record instead of waiting on a
        # go-ahead, the coordinator owns callback drain and downstream resume,
        # and a commit hash is origin-reachability-checked before it is recorded
        # in a gate — all carried in a fixed-field shape a checker can read.
        "### handoff-worthy state は callback outcome journal が載るまで完了しない",
        "その callback outcome journal が記録されるまで complete ではない",
        "### dependency hold は durable record に park する (go-ahead を待たない)",
        "operator への go-ahead の質問で停止しない",
        "### callback drain と downstream resume は coordinator が所有する",
        "coordinator は callback drain (蓄積した callback outcome journal を読み、それに基づいて行動する)",
        "downstream resume",
        "### gate へ commit hash を記録する前の origin 到達性 preflight",
        "その commit が `origin` から到達可能であることを検証し、結果を `origin_reachable` として記録する",
        "### 固定 field の journal shape",
        "`resume_condition`、`resume_owner`、`origin_reachable`",
        # Sublane retirement drain (Redmine #12214). A closed lane is the
        # default retire candidate, a dependency ancestor is retained until
        # downstream consumed, an open hold condition forbids retirement, a
        # destructive op requires a green safety preflight, and the coordinator
        # owns the retirement drain after the callback drain — bracketed by
        # retire_ready / retired journals in a checker-readable fixed-field
        # shape.
        "### closed lane は既定の retire candidate である",
        "その lane は既定で `retire_candidate` となる",
        "### dependency ancestor lane は downstream 消費まで retain する",
        "`retirement_state: retain_until_downstream_consumed`",
        "### hold 条件が open の間は退役を禁止する",
        "`retire_blockers` list が空でない限り `retirement_state: retire_blocked`",
        "### 破壊的操作の safety preflight",
        "lane を `retire_candidate` から `retirement_state: retire_ready` へ進める",
        "### retire_ready / retired journal shape",
        "### 退役 drain は callback drain の後に coordinator が所有する",
        "coordinator は callback drain の後に退役 drain を実行する",
        # Post-dispatch fill loop (Redmine #12355, portable extract of the
        # repo-local spine identified by the #12353 inventory). Pipeline-first
        # is the default and serialization is the recorded exception; a single
        # successful dispatch is not a coordinator stop; the minimal
        # coordinator-blocking vocabulary, the Drain Order, and the one durable
        # fill decision are pinned so the distributed body keeps the loop that
        # ties the drains to the next dispatch.
        "### pipeline-first が default、直列化は記録付き例外",
        "### 最小の coordinator-blocking state 語彙",
        "### Drain 順序",
        "### dispatch / drain のたびに loop を再実行する",
        "dispatch が 1 件成功しただけでは coordinator の stop 条件には**ならず**",
        "`stop_coordinator_blocking`",
        "`stop_soft_profile_full`",
        "repo-local spine `vibes/docs/logics/coordinator-sublane-development-flow.md` の portable な抽出である",
        # Existing-project sublane adoption (Redmine #12432, portable extract
        # of the repo-local runbook vibes/docs/logics/existing-project-sublane-
        # adoption.md added under #12423). Adopting the governed scaffold + flow
        # into an existing project preserves its routing, is reachable from the
        # `--with-sublane-flow` profile, keeps the bootstrap exception from
        # relaxing any approval gate, and carries the full adoption sequence
        # (preflight / decomposition / dispatch / scaffold+catalog / verify /
        # origin-reachable commit / callback recovery / close order).
        "### 既存 project 導入が適用される場面",
        "### 導入編集前の read-only preflight",
        "**既存 routing を保全する**",
        "### Dispatch decision と scaffold / rules / catalog 導入",
        "### 検証・origin 到達 commit・callback recovery・close 順序",
        "### 既存 project 導入が緩めない境界",
        "導入は setup 経路であり、いかなる gate の緩和でもない",
        "bootstrap 例外",
        "clean な `scaffold status` は workflow 導入ではない",
        # Workflow Change Verification policy.
        "Workflow Change Verification",
        "通常開発タスクは Claude が実装する",
        # Redmine #13029 portable core-workflow upstream. Spec-decision
        # routing keeps the coordinator-owned / sublane-decidable split and
        # the stop-on-coordinator-owned-decision rule; design consultation
        # keeps the firing axis; backlog reconciliation keeps the immediate
        # four-way durable classification; the delegated-coordinator model
        # keeps the fixed role invariants and the grandchild-dispatch
        # purpose; the labeling rule and the implementer escalation window
        # stay verbatim.
        "sublane は実装を止め、durable record に design consultation / blocked / owner-action-needed を記録し",
        "後戻りコスト × 実装者反証の有益性",
        "実装者は実装せず、設計に答える",
        "**owner decision pending** — 実装せず、owner 判断待ちとして残す",
        "`purpose: preserve_coordinator_context`",
        "parent issue close は最上位 `coordinator` のみが行う",
        "owner approval は最上位 `coordinator` の単一 aggregation point に集約し、子 lane 内で ratify しない",
        "`grandchild_dispatch: avoided`",
        "必ず `#<id> <短い概要>` の形で書く",
        "ユーザーとの対話窓口は原則 Codex に統一する",
        # Redmine #13060 medium-priority doctrine upstream. Delivery ACK
        # never stands in for completion, pane silence is not a completion
        # detector, a version string alone is not runtime evidence, and a
        # fingerprint mismatch never counts as PASS.
        "**delivery ACK を task completion の代理にしない。**",
        "「観測できない」ことは「完了した」ことではない",
        "runtime signal で workflow gate を自動前進させない",
        "version 文字列単独を evidence にしない",
        "`blocked` または `environmental` として記録し、PASS evidence に混ぜない",
        "実行 surface をその場で自己修復しない",
        "--with-worktree-runbook",
        # Redmine default-project resolution (Redmine #10689). The
        # workspace-local snippet path and the "explicit wins over
        # default" / "UNVERIFIED escalates" rules must stay in the
        # skill body so agents pick them up at session start.
        "Default project 解決",
        ".mozyo-bridge/redmine-defaults.md",
        ".mozyo-bridge/project-defaults.yaml",
        "明示の `project_id` は常に default に優先する",
        "UNVERIFIED",
        # Redmine #13489 (owner intent j#74953): the load-bearing semantics of the
        # wait / polling efficiency standard. A byte-parity pass alone would not
        # catch these being deleted or weakened, so pin them verbatim: a blocking
        # wait is not token spend, the 45-55s bounded cadence, no pane-history dig
        # on an unchanged timeout, the 20-40 line read window, no scrollback re-read,
        # findings batched to the gate journal, and immediate interrupts limited to
        # Critical safety / authority / irreversibility.
        "10–30 秒間隔の反復 poll を「進捗確認」として標準化しない",
        "user commentary SLA 内の **45–55 秒**を基本周期とする",
        "bounded wait が timeout し durable state が不変なら、pane history を読まない",
        "pane 末尾の **20–40 行**を読む",
        "同じ scrollback を再読しない",
        "gate journal の時点でまとめて durable record に載せる",
        "即時 interrupt は、**安全・authority・不可逆リスクに関わる Critical**",
        "user commentary SLA 内の 45–55 秒基本 cadence に収める",
        # Redmine #13518 (owner intent j#75078): the load-bearing semantics of
        # the zero-wait / mozyo-only doctrine. Pin them verbatim so a byte-parity
        # pass alone cannot let them be deleted or weakened: the LLM turn ends
        # without blocking wait / poll after a dispatch, raw herdr wait/read/list
        # + pane/tmux ops are operator-debug primitives (not agent tools), the
        # 45–55s cadence is re-homed to the background watcher / operator layer,
        # and the four role profiles carry the mozyo-facade-only + zero-wait/yield
        # discipline.
        "blocking wait も poll も実行せず、turn を終了 (yield) する",
        "`herdr agent wait` / `herdr agent read` / `herdr agent list` / raw pane・tmux 操作は adapter test と operator debug のための primitive",
        "watcher / operator 側の観測周期である",
        "通常運用は mozyo semantic facade (`workflow step` / `handoff` 等) のみを使う。raw Herdr / tmux command は adapter test / operator debug に限り、通常 turn では使わない",
        "dispatch / handoff / callback を送信したら blocking wait / poll をせず turn を終了 (zero-wait / yield) し、進捗再開は durable callback による新 turn に委ねる",
        "handoff / callback を送信したら blocking wait / poll をせず turn を終了 (zero-wait / yield) し、進捗再開は durable callback による新 turn に委ねる",
        # Redmine #13745 (parent #13490): the fixed gateway/worker role-profile
        # harness is synced to the durable-callback + duplicate-control contract
        # that worked in #13569 (j#77346-j#77348). Pin the load-bearing clauses
        # verbatim so a byte-parity pass alone cannot delete or weaken them: the
        # gateway does not request a main duplicate review, single-sends
        # `changes_requested` to the same-lane worker, state-only-callbacks
        # `approved` upstream, records the callback outcome and fails closed on a
        # self / foreign / ambiguous target or uncertain delivery (no blind
        # retry), and never reads worker completion / pane state / transport ACK
        # as a Review Gate or integration completion; the worker records its
        # verdict / correction to the same-lane gateway and keeps the hierarchical
        # route instead of callbacking a main coordinator or foreign lane direct.
        "same-lane ownership を確認したら main coordinator に重複 review を要求しない (durable Review Result が正本)",
        "`changes_requested` は same-lane worker へ単回送達し、blind re-send しない",
        "review_result が `approved` のときは上位 (<upstream_coordinator>) へ状態だけを callback し、diff review を main で重複させない",
        "callback outcome (sent / blocked / not-attempted) を durable 記録する。actual target が self-route / foreign lane / ambiguous、または delivery が uncertain なら fail-closed で停止し、blind retry しない",
        "worker の完了報告・pane 状態・transport ACK を Review Gate approval や integration 完了と読み替えない",
        "implementation / review finding verdict / correction を durable 記録し、same-lane gateway (<gateway_callback_target>) へ返す",
        "main coordinator や foreign lane へ直接 callback せず、same-lane gateway を経由する階層 route を維持する",
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
    """Pin the same-lane dispatch submit-completion contract in the
    consolidated sublane workflow spine (Redmine #12207 / #12215).

    `vibes/docs/logics/coordinator-sublane-development-flow.md` is the
    repo-local workflow spine. It must carry the same submit-completion
    contract the skill body pins, so an agent reading the spine learns that a
    same-lane dispatch reaches submit and does not rest at a pending prompt. A
    future edit that drops the contract fails here loudly rather than silently
    reopening the #12207 stall.
    """

    DOC_PATH = (
        "vibes",
        "docs",
        "logics",
        "coordinator-sublane-development-flow.md",
    )

    REQUIRED_MARKERS: tuple[str, ...] = (
        "この文書は repo-local の **一次 spine**",
        "target-lane Codex が durable anchor を読み、same-lane Claude へ実装依頼を submit 完結で渡す",
        "`--no-submit` / `--mode pending` は operator / debug fallback",
        "$forbid(\"coordinator_assistant へ実装型 work を直接渡す\")",
    )

    def test_operating_model_doc_carries_same_lane_dispatch_contract(self) -> None:
        body = ROOT.joinpath(*self.DOC_PATH).read_text(encoding="utf-8")
        for marker in self.REQUIRED_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        "vibes/docs/logics/coordinator-sublane-development-flow.md is "
                        f"missing #12207 same-lane dispatch marker {marker!r}; "
                        "the submit-completion contract regressed or this anchor "
                        "list needs an intentional update in the same commit."
                    ),
                )


class SublaneCompletionGuardrailsDocTest(unittest.TestCase):
    """Pin the sublane completion guardrails in the consolidated workflow
    spine (Redmine #12213 / #12215).

    `vibes/docs/logics/coordinator-sublane-development-flow.md` is the
    repo-local workflow spine. It must carry the #12213 completion guardrails:
    handoff-worthy states callback to the coordinator, missing callbacks are
    swept from durable state, and commit hashes are origin-reachability-checked
    before they are recorded in a gate. A future edit that drops the contract
    fails here loudly rather than silently reopening the
    #12189-#12191 / #12207 gaps.
    """

    DOC_PATH = (
        "vibes",
        "docs",
        "logics",
        "coordinator-sublane-development-flow.md",
    )

    REQUIRED_MARKERS: tuple[str, ...] = (
        "commit hash を gate に書く場合は origin reachability を先に確認する",
        "sublane は handoff-worthy state で管制塔 Codex へ callback する",
        "$callback_sweep()",
        "progress_without_callback / no_progress_after_handoff / callback_delivery_failed / callback_not_attempted",
        "callback / review / owner / integration / close / retirement",
    )

    def test_operating_model_doc_carries_completion_guardrails(self) -> None:
        body = ROOT.joinpath(*self.DOC_PATH).read_text(encoding="utf-8")
        for marker in self.REQUIRED_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        "vibes/docs/logics/coordinator-sublane-development-flow.md is "
                        f"missing #12213 sublane completion guardrail marker "
                        f"{marker!r}; the completion-condition redefinition "
                        "regressed or this anchor list needs an intentional "
                        "update in the same commit."
                    ),
                )


class SublaneRetirementDrainDocTest(unittest.TestCase):
    """Pin the sublane retirement drain in the consolidated workflow spine
    (Redmine #12214 / #12215).

    `vibes/docs/logics/coordinator-sublane-development-flow.md` is the
    repo-local workflow spine. #12213 defined the front of a sublane's life
    (completion / callback drain); #12214 defines the back (retirement). The doc
    must carry the retirement state machine, blockers, safety preflight, and
    retire_ready / retired bracket. A future edit that drops the section fails
    here loudly rather than silently reopening the Version #222
    resident-closed-lane accumulation gap.
    """

    DOC_PATH = (
        "vibes",
        "docs",
        "logics",
        "coordinator-sublane-development-flow.md",
    )

    REQUIRED_MARKERS: tuple[str, ...] = (
        "## サブレーン退役",
        "retirement_state = retire_candidate / retire_ready / retain_until_downstream_consumed / retire_blocked / retired",
        "retire_blockers = active_lane, review_pending, owner_approval_pending, unresolved_callback, dirty_worktree, pending_prompt, unpushed_commit, unknown_target_identity",
        "safety_preflight = redmine_closed, worktree_clean, origin_reachable, pending_prompt_absent, callback_drained, target_identity_known",
        "retire_ready / retired journal で destructive 操作の前後を bracket",
        "閉じた lane は default retire candidate",
        "`retired` journal には removed / killed した worktree、pane、branch、`durable_anchor`",
    )

    def test_operating_model_doc_carries_retirement_drain(self) -> None:
        body = ROOT.joinpath(*self.DOC_PATH).read_text(encoding="utf-8")
        for marker in self.REQUIRED_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        "vibes/docs/logics/coordinator-sublane-development-flow.md is "
                        f"missing #12214 sublane retirement drain marker "
                        f"{marker!r}; the retirement-stage definition regressed "
                        "or this anchor list needs an intentional update in the "
                        "same commit."
                    ),
                )


if __name__ == "__main__":
    unittest.main()
