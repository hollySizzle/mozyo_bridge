# Sublane / Worktree Operating Runbook (portable)

Redmine #11929 (parent #11928)。sublane / worktree 運用スタイルを他プロジェクトでも再現するための、時系列順の **実行 runbook**。

> 互換性注記: 管制塔 / サブレーン開発フローの一次 spine は
> [[logic-coordinator-sublane-development-flow]] である。本 doc は worktree
> 作成、handoff、review、退役に関する時系列の補助 runbook であり、同じ
> workflow を別の完全な規約本文として再定義しない。判断が分岐する場合は
> [[logic-coordinator-sublane-development-flow]] を優先する。

> この doc は **時系列 runbook** である。一つの sublane を作成→実装→review→退役まで時系列で並べ、各 step で従うべき規約は既存の正本 section へ **リンクする**。規約本文をここに複製しない (重複回避)。深い規約は次の正本に置く:
> - 運用哲学 / identity 層 / lane 役割: [[logic-cockpit-sublane-operating-model]]
> - core と skill/runbook の責務境界: [[logic-worktree-lifecycle-boundary]]
> - lane admission / drain / soft profile: [[logic-sublane-bandwidth-policy]]
> - portable な手順詳細 (handoff lifecycle / natural-name target / sublane callback / coordinator stop / owner approval aggregation / stall detection / main-unit Claude / audit-owned commit): distributed skill [[skill-workflow-reference]] (`skills/mozyo-bridge-agent/references/workflow.md`)
>
> 本 doc は portable な手順骨子のみを置く。具体 path / repo 名 / session nickname / 社内固有 lane 規約は operator の private runbook 側に置き、OSS default に混ぜない ([[rule-public-private-boundary]])。例は placeholder で書く。

## 0. 前提条件 (prerequisites)

- mozyo-bridge CLI が利用できる。開発中で installed CLI が repo source より遅れる場合は repo-local CLI を使う (`PYTHONPATH=src python3 -m mozyo_bridge ...`; skill `## Dogfooding Version Boundary` / `## Stall ... Stale CLI`)。
- cockpit session が利用できる。pane adoption は registry-aware `init` を使う (#11427; [[logic-workspace-registry]])。
- durable ticket system がある。本 runbook は Redmine を例にするが、原則は ticket system 非依存である: **durable record (issue/journal または task/comment) が source of truth、pane message は pointer** (skill `## Ticket-ID Entrypoint` / `## Handoff Lifecycle`)。
- worktree lifecycle は mozyo-bridge core ではなく本 runbook 側で扱う ([[logic-worktree-lifecycle-boundary]])。

## 1. issue / branch / worktree / lane / pane の対応付け

一つの作業単位を次のように対応させる。対応は durable record (issue) に記録し、pane 配置から推測しない。

```text
work unit  = 1 issue
           + 1 branch
           + 1 git worktree (checkout)
           + 1 lane         (cockpit 上の checkout レーン; lane_id で識別)
           + 1 Codex pane   (lane gateway)  + 1 Claude pane (implementer)
```

- identity facts (workspace_id / lane_id / role / pane) は core の discovery primitive が観測する。`mozyo-bridge agents targets` で確認する (skill `## Natural-Name Target Handoff`)。
- same workspace の複数 checkout は `lane_id` で別 lane として識別される ([[logic-cockpit-sublane-operating-model]] `## Lane の役割`)。

## 2. lane 作成 + cockpit append/adopt

worktree の add は **素の git** で行う (core command ではない; [[logic-worktree-lifecycle-boundary]])。path/branch 命名は operator 判断。

```text
git worktree add <worktree-path> -b <branch>   # path/branch は operator 判断。issue 番号からの強制生成は前提にしない
mozyo cockpit ...                              # lane を cockpit に append / adopt
mozyo-bridge init claude                       # / codex。registry-aware adoption + role bind (#11427)
mozyo-bridge agents targets --session <cockpit-session>   # lane/repo/role/pane を確認
```

cockpit group (named tmux session) は display grouping であって routing identity ではない (skill `## Named Cockpit Groups ...`)。

## 3. coordinator → sublane への dispatch (target-lane Codex gateway 経由)

cross-lane handoff は **target lane の Codex** を gateway として通す。同一物理 session でも lane 境界は governance 境界 (skill `## Natural-Name Target Handoff` の cross-lane rule / [[logic-cockpit-sublane-operating-model]] `## Cross-Lane Routing Rule`)。

- Implementation Request を作る前に [[logic-sublane-bandwidth-policy]] の
  dispatch decision を durable record に残す。implementation-shaped work は
  sublane-first が default であり、main unit / default-lane Claude に直接渡す
  場合は例外理由を明示する。理由のない default-lane handoff は correction
  対象である。
- dispatch 前に [[logic-sublane-bandwidth-policy]] の admission rule を確認する。空き pane / worktree があっても、coordinator に unread review_request、owner_waiting、blocked callback、retire_ready lane が残っている場合は先に drain する。
- durable anchor を先に記録 → `mozyo-bridge handoff send --to codex --target <target_lane_codex_%pane> --target-repo auto` で通知。
- Claude への direct delivery は same-lane addressing に限定。cross-session `--to claude` は CLI が拒否する。

### 推奨 dispatch batch

通常の v0.8 以降の実装 batch では、coordinator は 3 本までの active
implementation sublane を標準運用として使う。1 本ずつ完了を待つ逐次運用は、
blocking queue が無い状態では throughput smell として扱い、serial にする理由を
dispatch decision に書く。

4 本目は burst として扱い、review / owner / close / callback queue を starving
しない根拠を記録する。5 本目以降は explicit owner/operator decision なしに開か
ない。

## 4. sublane Claude の implementation 境界

sublane Claude は bounded implementation worker (skill `### Sublane Claude` / `## Sublane Coordinator Callback`)。

- pane scrollback ではなく durable record から実装する。
- `implementation_done` / `review_request` gate を記録し、verification と residual risk を再現可能に残す。
- owner close approval は回収しない (owner-facing は coordinator Codex)。

## 5. coordinator monitoring / callback / owner approval aggregation

- sublane は handoff-worthy state (blocked / implementation_done / review_request / review result / commit recorded / owner-approval-waiting) で coordinator lane の Codex へ callback する (skill `## Sublane Coordinator Callback`)。callback は pointer、durable record が正本。
- coordinator は callback_due / review_waiting / owner_waiting / blocked / retire_ready を bandwidth state として扱い、[[logic-sublane-bandwidth-policy]] の drain order に従って処理する。
- coordinator は停止時に「why / on-approval / meanwhile」の next-action 提案を durable journal に残す (skill `## Coordinator Stop And Next-Action Standard`)。
- owner-approval-waiting は常に単一 coordinator Codex に集約し、sublane 内で解決しない (skill `## Owner Approval Aggregation`)。
- callback が来ない場合は「delivered dispatch journal + 期待 durable journal 欠如」で stall candidate を判定し 4 状態に分類する (skill `## Stall And No-Progress Detection Standard`)。
- main unit に Claude pane がある場合は assistant 用途に限定する (skill `## Main-Unit Claude Safe-Use Boundary`)。

### callback が欠けた時の管制塔 sweep

callback は pointer なので、欠けても durable progress が消えるわけではない。
coordinator は新しい sublane を開く前に、active lane の Redmine journal を sweep
し、次を分類して記録する。

- durable progress があるが coordinator callback / ack が無い:
  `progress_without_callback`。既存 journal を拾って review / close flow へ進め、
  done な work を再 dispatch しない。
- durable progress も無い: `no_progress_after_handoff`。delivery anchor と期待 gate
  を明示して再通知または blocker 化する。
- callback 試行が失敗している: `callback_delivery_failed`。失敗理由を読み、
  stale CLI なら repo-local CLI で再通知する。
- progress はあるが callback / receive-method journal が無い:
  `callback_not_attempted`。process gap として記録し、必要なら sublane 側へ補正を
  依頼する。

この sweep は owner approval や close を self-authorize しない。review gate、
owner close approval、status close はそれぞれ別 gate として処理する。#12145 の
ように implementation_done / review / owner_close_approval / integration が揃った
後も Redmine status が `着手中` のまま残る場合は、[[logic-sublane-bandwidth-policy]]
の `close_waiting` として扱い、新規 dispatch より先に close gate と status を
整合させる。

## 6. review / merge / push / CI / lane retirement

- review 粒度は preset に従う (UserStory 単位の US-level audit、または単独 issue の per-issue review; central preset / skill `## Ticket System Conventions`)。
- commit は audit record 成立後に Codex が audit-approved diff のみを commit する (skill `## Audit-Owned Commit Authority`)。implementer は commit を作らず diff を残す運用も可。
- push / CI は coordinator / owner gated。push 前に local checks が green であること、push 後に CI 結果を durable record に記録する。
- commit-bearing work を close する前に、integration disposition を durable record に残す。少なくとも次のいずれかを Close Gate の basis に含める:
  - target branch に merge 済み。
  - push 済みで、CI / merge owner / branch が記録済み。
  - `git cherry -v <base> <branch>` 等で target branch と patch-equivalent と確認済み。
  - integration を明示 defer し、残す branch / commit / owner / follow-up issue が記録済み。
  - no-commit / docs-only 等で integration 不要であることが明示済み。
  review approval と owner close approval だけでは、local sublane commit の所在を保証しない。commit-bearing work の integration disposition が無い場合は `integration_waiting` として扱い、status close より先に drain する。
- lane retirement (worktree 削除) は **素の git** で行い、削除前に dirty / in-scope 変更が無いことを確認する safety step を必ず踏む ([[logic-worktree-lifecycle-boundary]])。
- routine retirement は coordinator Codex の責務であり、条件を満たす lane は owner 確認なしに退役してよい。条件は [[logic-worktree-lifecycle-boundary]] `## sublane retirement authority` を読む。
- owner approval が必要なのは、未統合 commit、scope 不明 dirty diff、credential / private 情報の可能性、owner 判断待ち、active review / handoff、identity ambiguity など、routine retirement 条件を外れる場合である。
- lane count が local soft profile を超えている場合は、close 済み / `retire_ready` lane を次の optional dispatch より先に退役する ([[logic-sublane-bandwidth-policy]])。

```text
git status --short                  # in-scope dirty が無いこと、または disposable local runtime state のみであることを確認
git cherry -v <base> <branch>        # 必要なら patch-equivalent / integrated を確認
git worktree remove <worktree-path>
# 必要なら cockpit pane を kill。material な退役は durable record に残す
```

## 7. known friction と対処

| friction | 対処 (本 runbook scope) | 正本 |
|---|---|---|
| stale installed CLI (landed 直後の subcommand を拒否) | repo-local CLI (`PYTHONPATH=src python3 -m mozyo_bridge`) を使う | skill `## Dogfooding Version Boundary` / `## Stall ... Stale CLI` |
| Claude pane が auto mode でない | cockpit / sublane 作成経路の launch-context policy default で `claude --permission-mode auto` を再現可能に付与する (#11925)。`settings.json` には書かない。`MOZYO_CLAUDE_PERMISSION_MODE` は override rail。既存 pane には非 retroactive (手動切替 / 再起動が必要)。`dry-run` / `doctor claude_launch_policy` で検出する | `#11924` / `#11925` / [[logic-cockpit-sublane-operating-model]] |
| stalled lane / callback 欠落 | durable record から stall candidate を判定・分類し、再通知を journal に残す | skill `## Stall And No-Progress Detection Standard` |
| close-ready issue が open のまま残る | close gate / owner close approval / integration record を照合し、`close_waiting` として新規 dispatch より先に閉じる | [[logic-sublane-bandwidth-policy]] |
| cockpit 列幅の偏り | operator が手動 rebalance (display 品質問題; identity は不変) | operator runtime; [[logic-cockpit-sublane-operating-model]] |

これらは observed friction ([[logic-cockpit-sublane-operating-model]] `## 観測された前提`) への運用対処であり、core CLI への取り込みではない。

## 8. public / private boundary と non-goals

- mozyo-bridge core を Git worktree manager にしない。`git worktree add/remove` の core CLI lifecycle command を追加しない ([[logic-worktree-lifecycle-boundary]])。
- private path / 社内固有 lane 規約 / operator-specific policy を OSS default に混入させない ([[rule-public-private-boundary]])。本 runbook は portable な手順骨子のみ。具体 cockpit composition・削除条件・並列上限・命名規約・優先順位は operator private runbook に置く。
- 本 runbook は docs-only。source / test / core CLI / 配布 shape を変更しない。

## 検証

- `mozyo-bridge docs validate --repo .` ほか catalog 検証一式 (本 doc の catalog 登録時)。
- `mozyo-bridge docs validate --check-file-coverage --repo .`。
- `mozyo-bridge docs generate-file-conventions --repo . --check`。
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`。
- `mozyo-bridge docs resolve vibes/docs/logics/sublane-worktree-operating-runbook.md --repo . --format text` で関連 docs 解決を確認。
- shared skill / 配布物は変更しないため `scripts/sync_plugin_skill.sh --check` は drift を出さない。
