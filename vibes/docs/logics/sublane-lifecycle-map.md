# サブレーン・ライフサイクル統合マップ

Redmine #12605 (parent #12603 / Version #303)。サブレーンの一連のライフサイクル — workspace identity、launch、worktree、callback、retire、merge、acceptance smoke — の **関係と正本の所在を一箇所に集約する navigation map** の正本。各段階の実行契約 (手順・swimlane・CLI flag・判断条件) は既存の canonical doc が正本であり、本 doc はそれらを束ねて読み順と境界を固定するだけで、内容を複製しない。

> 本 doc は **関係マップ (relationship map)** であって、いずれの段階の execution source-of-truth でもない。段階ごとの判断・手順・gate semantics は `## ライフサイクル段階と正本マップ` が指す canonical doc を読む。本 doc とそれら canonical doc が矛盾した場合、canonical doc を優先し、本 doc を是正する。正本分離は [[rule-llm-rule-authoring]] `## 正本分離` に従う。

## 目的

- sublane を「立ち上げて作業し退役する」までの段階が複数 doc に分散しているため、どの段階をどの doc が所有するかを一望できる索引を提供する。
- Git workspace と非 Git workspace で段階ごとに何が変わるかを、関係の観点で一箇所に整理する。
- 既存 doc から本 map へ参照を張り、`mozyo-bridge docs resolve` で sublane 系 source / docs から本 map に到達できるようにする。
- 「retire」のように複数の意味で使われる語を曖昧にしない。

## 非目標

- 段階ごとの execution 契約 (admission / pipeline fill / drain order、worktree add/remove runbook、callback delivery rail、retirement check、merge conflict 時の挙動、acceptance PASS 条件) を本 doc に再掲しない。これらは canonical doc が正本である。
- coordinator / sublane の標準フロー sequence diagram と swimlane を本 doc に複製しない。標準フローの正本は [[logic-coordinator-sublane-development-flow]]。
- CLI flag / help / validation error を本 doc にカタログ化しない。flag の正本は CLI 側である。

## 用語

```yaml
workspace:
  意味: 一つの checkout / repo root を識別する単位。Git workspace と 非Git workspace の両方を含む。
  正本: [[logic-workspace-registry]] / [[logic-project-scoped-workspace-identity]]
Git_workspace:
  意味: git repo root を持つ checkout。worktree add/remove と branch/merge が成立する。
非Git_workspace:
  意味: git repo root を持たない directory scaffold。lane は動かせるが worktree/merge は成立しない。
lane:
  意味: 同一 workspace 内の一つの作業系列 (issue/branch/pane/role の束)。lane identity は workspace と別軸。
target / projection:
  意味: handoff/callback の宛先解決と cockpit 表示への射影。正本: [[logic-unit-target-model]] / [[spec-route-identity-ledger]]
retire_三義:
  sublane_retirement: lane (pane/worktree/branch) の退役。正本: [[logic-worktree-lifecycle-boundary]] / [[logic-coordinator-sublane-development-flow]]
  pane_lifecycle: Claude/Codex pane の guarded_kill / orphan / 再利用。正本: [[logic-session-boundary]]
  fallback_rail_retirement: 互換 rail / legacy 機能の段階的廃止。別概念。正本: [[logic-fallback-retirement-ledger]]
```

## ライフサイクル段階と正本マップ

段階は概ね下記の順で進む。各段階の **判断・手順・gate semantics は所有 doc が正本** であり、本 map は読み順と境界のみを固定する。

```text
[1] workspace identity   identity を確定し、workspace/lane/target を解決する
      正本: logic-workspace-registry, logic-project-scoped-workspace-identity,
            logic-workspace-anchor-project-defaults-migration, logic-managed-state-model,
            logic-unit-target-model

[2] launch               lane/pane を立ち上げ role を bind する (init claude/codex, cockpit append)
      正本: logic-coordinator-sublane-development-flow (dispatch/admission/pipeline fill),
            logic-session-boundary (Claude/Codex pane lifecycle),
            logic-worktree-lifecycle-boundary (worktree サブレーン runbook)

[3] worktree / scaffold   Git: git worktree add で作業木を作る / 非Git: directory scaffold で lane を置く
      正本: logic-worktree-lifecycle-boundary
      注: worktree lifecycle は core CLI ではなく skill/runbook/operator recipe の責務。

[4] callback             作業中の handoff/callback を durable anchor 経由で配送・受領する
      正本: logic-tmux-send-safety-contract (delivery rail / landing marker / fail-closed),
            logic-ack-completion-receiver-state (ACK vs completion vs receiver-state),
            spec-route-identity-ledger (stale pane id ではなく route identity で再解決),
            spec-delegated-coordinator-decision-records (callback target schema)

[4b] hibernate / resume  issue が open のまま lane の常駐 process だけを非破壊的に畳む
                         (worktree/branch/未push commit/lane metadata/callback route は保存)。
                         retire ではない (retire は issue closed 前提)。逆遷移は resume。
      正本: logic-coordinator-sublane-development-flow (Early hibernate ... 節: park basis /
            drain-queue process retention / dogfood 委譲),
            skill references/workflow.md `## Sublane hibernate (プロセス解放) と early hibernate`
      park basis: dependency (依存 wait で park) | early_hibernate (Redmine #13967: review
            approved + staging integration + CI green + dogfood の execution/evidence を専用
            release issue へ委譲。close authority + owner approval は coordinator に残す)。
      注: hibernate は close / dogfood 成功 / owner approval へ読み替えない。common safety gate
          (pending review/callback/integration/work/prompt・dirty/unpushed・identity 不明) は fail-closed。
          owner approval pending は basis 依存 (early hibernate では blocker にしない)。
      release-boundary TOCTOU 保全 fence (Redmine #13843): preflight と process release は atomic で
          なく、間に worker が worktree mutation を開始でき dirty residue を残す。release 直前の再検証・
          typed blocked・post-check withhold・recovery 収束の **execution 契約は本 map に複製しない**。
          正本: [[logic-managed-state-model]] `lane_lifecycle_records` の「hibernate release-boundary
          TOCTOU 保全 fence」節 (fingerprint/activity/attestation/revision の release 直前再検証と
          process_release state 分離)、および `sublane_hibernate*.py` module docstring。

[5] retire               lane を退役する (pane/worktree/branch)。owner 確認なしに退役してよい条件は所有 doc。
      正本: logic-worktree-lifecycle-boundary (sublane retirement authority / record),
            logic-coordinator-sublane-development-flow (retirement drain),
            logic-session-boundary (pane guarded_kill / orphan)
      非該当: fallback rail の retirement は別概念 (logic-fallback-retirement-ledger)。

[6] merge / integration  退役時に lane commit を target branch へ統合する (#12603 retire-time merge)。
      正本: logic-worktree-lifecycle-boundary (#12603 設定駆動 integration 境界),
            logic-coordinator-sublane-development-flow (integration disposition),
            logic-existing-project-sublane-adoption (origin reachability / clean integration branch)
      注: conflict / dirty / target branch 不明では退役せず管制塔へ feedback。

[7] acceptance smoke     ライフサイクル全体を実機で検証する (段階ではなく全体に被さる検証層)。
      正本: logic-delegated-coordinator-real-machine-acceptance (3層実機 acceptance),
            logic-delegated-coordinator-smoke-test-frame (軽量 test frame),
            logic-acceptance-rerun-protocol (closed Version 再開なしの rerun),
            logic-turnkey-e2e-acceptance (published package の turnkey E2E),
            logic-existing-project-sublane-adoption (adoption live smoke)
```

## Git / 非Git workspace の差分

```yaml
identity:
  共通: workspace/lane/target の解決は両方で成立する (非Git workspace も registry が支える)。
  正本: [[logic-workspace-registry]] (非git workspace / dev container を明示サポート)
worktree:
  Git: git worktree add/remove で lane ごとの作業木を作る。
  非Git: worktree は成立しない。directory scaffold で lane を置き、worktree 段階を skip する。
  正本: [[logic-worktree-lifecycle-boundary]]
merge:
  Git: retire 時に lane commit を target branch へ統合する余地がある。
  非Git: branch/merge が無いため retire-time merge は対象外。成果の取り込みは別経路で扱う。
callback / acceptance:
  共通: durable anchor (Redmine journal) 経由の配送・検証は workspace 種別に依らず同じ。
```

非 Git workspace でも sublane を動かせること自体は実機テストで確認する対象であり、その検証は acceptance smoke 系 doc (上記 `[7]`) が正本である。

## 参照正本

本 map が束ねる canonical doc。監査時は本 map のタイトル索引だけで済ませず、対象段階の canonical doc 本文を読む。

- `vibes/docs/logics/coordinator-sublane-development-flow.md`
- `vibes/docs/logics/worktree-lifecycle-boundary.md`
- `vibes/docs/logics/existing-project-sublane-adoption.md`
- `vibes/docs/logics/sublane-bandwidth-policy.md`
- `vibes/docs/logics/project-scoped-workspace-identity.md`
- `vibes/docs/logics/workspace-registry.md`
- `vibes/docs/logics/workspace-anchor-project-defaults-migration.md`
- `vibes/docs/logics/managed-state-model.md`
- `vibes/docs/logics/unit-target-model.md`
- `vibes/docs/logics/session-boundary.md`
- `vibes/docs/logics/tmux-send-safety-contract.md`
- `vibes/docs/logics/ack-completion-receiver-state.md`
- `vibes/docs/logics/fallback-retirement-ledger.md`
- `vibes/docs/logics/acceptance-rerun-protocol.md`
- `vibes/docs/logics/turnkey-e2e-acceptance.md`
- `vibes/docs/logics/delegated-coordinator-real-machine-acceptance.md`
- `vibes/docs/logics/delegated-coordinator-smoke-test-frame.md`
- `vibes/docs/specs/route-identity-ledger.md`
- `vibes/docs/specs/delegated-coordinator-decision-records.md`
- `vibes/docs/specs/delegated-coordinator-role-profile.md`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `git diff --check`
- `mozyo-bridge docs resolve vibes/docs/logics/sublane-lifecycle-map.md --repo . --format text` で関連 canonical docs 解決を確認する。
