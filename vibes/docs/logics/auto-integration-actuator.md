# Guarded auto-integration / retirement-cleanup actuator

Redmine #13686 (parent #12603 / Version #303)。coordinator が手作業で行っていた
「review 承認 → integration branch への統合 → CI → close → lane 退役 (worktree remove /
local branch delete)」を、**gate 付きで replayable な単一 actuator** に移すための設計正本。

owner decision は #13686 j#96335、設計境界は同 j#77124 (Coordinator Design Answer,
approved_with_corrections)。両者が本 doc の上位である。実行契約のうち **authority 側**
(誰が統合してよいか、どの gate を満たす必要があるか) の正本は central preset
`agent-workflow.md` `### Commit Hash Origin 到達可能性` の `coordinator_owned_auto_integration`
であり、本 doc はそれを複製せず、**この repo の実装がその契約をどう構成しているか**を固定する。

> 本 doc は責務境界 + 実装構成の正本であり、gate 語彙・close 条件・review authority の正本
> ではない。矛盾した場合は central preset と Redmine journal を優先し、本 doc を是正する。
> 正本分離は [[rule-llm-rule-authoring]] `## 正本分離` に従う。

## 背景 — なぜ「自動 merge 禁止」を撤回したか

#11889 / [[logic-worktree-lifecycle-boundary]] は、worktree lifecycle を core CLI に取り込むと
core が **identity / discovery / safety primitive** から **Git workflow manager** へ肥大すると
判断し、境界を固定した。central preset も同じ趣旨で「自動 merge / auto-integration 機構の導入」
を全面禁止していた。

owner は j#96335 でこの全面禁止を **範囲が広すぎる** と判断した。撤回されたのは *機構の存在*
の禁止だけであり、次の 2 点は動いていない。

- **実装者は integration branch を前進させない。** 実装者の push は issue / lane branch に限る。
- **統合は coordinator の責務である。** actuator は coordinator の明示操作を代行する実行系で
  あって、新しい authority ではない。

したがって本 actuator が足すのは *権限* ではなく、**同じ権限で行われていた手作業の再現性**
である。手作業だった頃に暗黙だった各 revalidation を、明示的な gate と durable な段階別 outcome
に変えることが主目的である。

## 二つの state machine を分ける (j#77124 必須訂正1)

現行 #12604 `SublaneIntegrationUseCase.evaluate_retire` は `issue_closed` / callbacks drained /
durable retire record を **merge の前に** 要求する。ここへ live merge を単純接続すると、実際の
順序と逆転する。

```text
実際の順序:  review approval → integration → exact-SHA CI → task/US close → retire
#12604 の形: issue close + callback drain → merge → retire      ← 逆転
```

そこで #13686 は 2 つの machine を分離する。両者は state を共有せず、integration 側は close を
行わず、cleanup 側は merge / push を行わない。

```text
[integration]  integration_preflight → (integration_apply) → push_waiting → awaiting_ci → integrated
                                                                                    ↓
                                                                      (issue close は別経路)
                                                                                    ↓
[cleanup]      cleanup_preflight → process_retiring → worktree_removing → branch_cleanup → retired
```

`integration_preflight` / `cleanup_preflight` は **entry phase であり resting state ではない**。
preflight は「失敗 gate を見つけて blocked になる」か「後続 state へ渡す」のどちらかで、
`preflight のまま止まっている` という状態は存在しない (consumer が進捗と誤読するため、test で
固定している)。

### terminal disposition

| disposition | 判定根拠 | 副作用 |
| --- | --- | --- |
| `integrated` | push 済み + 統合 SHA の CI green (**gate は外せない**) | push |
| `already_integrated` | target ancestry (source head が target から到達可能) | なし |
| `patch_equivalent` | **明示的な patch-id evidence** | なし |
| `not_applicable` | 非 Git workspace | なし (process retire は別途走る) |
| `disabled` | `mode: disabled` (既定) | なし |
| `integration_blocked` | いずれかの gate 不成立 | なし |

`already_integrated` と `patch_equivalent` を分けるのは j#77124 必須訂正2 の要求である。前者は
ancestry という機械的事実、後者は evidence を要する主張であり、両者を畳むと durable record が
持っていない ancestry を主張してしまう。どちらも **同じ merge を再生成しない**。

## action record と idempotency (j#77124 必須訂正2)

一つの統合行為は immutable な action record に束ねる。`action_key` はその 6 field そのものである。

```yaml
action_key:
  - issue
  - lane_generation
  - source_head            # full 40-hex commit SHA (branch 名は pin ではない)
  - target_ref
  - expected_target_head   # 存在しない target は `none` sentinel
  - review_generation
```

step ledger は action key ごとに記録され、`done` の step は再実行しない。6 field のいずれかが
drift すれば **別の key** になるため、古い ledger が新しい action を満たすことはない。これが
「部分失敗から再実行しても duplicate merge / delete を起こさない」の実体である。

段階別 outcome は `done` / `not_applicable` / `blocked` / `pending` の 4 値で、
「走った」「そもそも該当しない」「拒否された」「まだ決着していない」を畳まない。

## fail-closed の一覧

action-time に再検証し、一つでも欠ければ副作用の **前** に停止する。全 gate を集めてから報告する
ので、durable record には最初の 1 件ではなく失敗 gate の全集合が載る。

```yaml
integration:
  - foreign_worktree / unknown_target_branch
  - target_not_configured               # 設定された integration branch と exact 一致しない (R2)
  - review_generation_inadmissible      # 最新 generation が approved かつ blocking finding なし
  - source_mutated_after_review         # review 済みの exact head であること
  - source_head_unreachable             # origin 到達可能
  - unpushed_unique_commits / dirty_worktree
  - source_ci_not_green / source_ci_evidence_incomplete / source_ci_head_mismatch  # (R3)
  - unresolved_owner_gate / unresolved_callback
  - target_drift                        # expected_target_head からの drift
  - non_fast_forward                    # ff-only 時
  - merge_conflict                      # merge commit disposition 時
  - integration_worktree_inadmissible   # 専用 worktree が未登録 / dirty / lane 自身 (R2)
  - push_rejected
  - push_outcome_head_missing           # push done なのに着地 head が無い。fallback しない (R3)
  - push_head_mismatch                  # 記録された head が disposition の着地 head でない (R3)
  - integration_ci_evidence_incomplete  # run / check identity / head を欠く (R2)
  - integration_ci_head_mismatch        # push が着地した head と別 commit の run (R2)
  - integration_ci_failed               # 決着したが non-success
cleanup:
  - action_key_mismatch                 # 別 action の authorization を継承しない
  - issue_not_closed / integration_unconfirmed / integration_ci_unsettled
  - unresolved_callback / unresolved_owner_gate / foreign_worktree
  - worktree_path_unregistered / dirty_worktree
  - branch_still_checked_out / unpushed_unique_commits
  - branch_not_reachable_from_target / branch_tip_drift
```

**代替手段を持たない**ことが安全性の中身である。conflict / non-ff / target drift / push 拒否を
rebase や force で解消しない。actuator の port は弱い操作しか公開していないため、「強い形に
fallback する」という選択肢が構造上存在しない。

## 破壊的 step の safety 条件

- `git worktree remove` は **clean かつ exact registered path** に対してのみ、`--force` なしで
  実行する。live adapter が `--force` を渡さないので git 自身が二重の enforcer になる。
- local branch delete は `git branch -D` を使わない。`git update-ref -d <ref> <old_value>` による
  真の compare-and-swap で、record 済み source head を指している間だけ消える。`git branch -d` は
  *HEAD からの到達性* を見るが、それはここで問うている問いではない。
- **ref を消す step はこの 1 つだけである** (`REF_DELETING_STEPS`)。policy toggle は step を
  止められるので、「別 step の条件が評価されないまま後続の step が ref を消す」形を作らない。
- **remote branch delete は存在しない。** R1 は既定 false の toggle として持っていたが、R1 review
  j#96344 finding 1 が (a) local delete を off にすると CAS 条件群の評価ごと飛ばして remote を
  消せる、(b) remote tip に対する CAS が無い、の 2 点を再現した。remote ref の真の CAS は
  `--force-with-lease` を要し、それは j#96335 が禁じた force である。**安全性を提供できない操作は
  「既定 off」で持つのではなく持たない**という判断で、config key / step / port method / adapter
  method を全て削除した。非 force な CAS 経路の有無は owner/design 判断とする。
- 非 Git workspace では worktree / branch step を明示的に `not_applicable` とし、process retire
  だけを独立に実行する。

## 実装構成

```text
domain/auto_integration_records.py       pure: 2 machine が共有する value object 群
domain/auto_integration_policy.py        pure: mode gate / integration 状態遷移
domain/retirement_cleanup_policy.py      pure: close 後の cleanup 状態遷移と CAS 条件
domain/auto_integration_journal.py       pure: durable record renderer (判断はしない)
application/auto_integration_actuator.py port (Protocol) + use case + config→policy 変換
application/auto_integration_live_ops.py live subprocess adapter (実 git)
```

### bool ではなく記録で受ける (R1 review j#96344)

R1 は 4 つの入力を bare bool / bare string で受けており、review が「**bool は監査できない**」と
指摘した。いずれも identity を持つ record へ置換した (正本: `auto_integration_records.py`)。

| R1 | R2 | 何が言えていなかったか |
| --- | --- | --- |
| `integration_ci_green: bool` | `IntegrationCiEvidence` | どの run の・どの required check が・どの commit について green か。無関係な green run が gate を満たしていた |
| `coordinator_confirmed: bool` | (R4 で mode ごと撤回) | 誰が・どの action を・どこに記録して承認したか |
| `integration_worktree: str` | `IntegrationWorktree` | それが lane 自身の checkout でないこと (j#77124 が禁じる操作を actuator 自身が実行し得た) |
| `policy.integration_branch` (未参照) | decision が exact-match を要求 | 設定した branch が実際に統合先を制約すること |
| `source_ci_green: bool` (R2 まで残存) | `IntegrationCiEvidence` | 同上。sibling gate に同じ穴が残っていた (R3) |

CI evidence は **push が着地した head** (ledger の push outcome が記録した commit) と exact-match
する。fast-forward なら source head、merge commit なら merge した commit である。**着地 head が
記録されていない場合に source head へ fallback しない** — 「何が着地したか記録し損ねた」ことは
「source が着地した」証拠ではない (R2 review j#96350 finding 2)。

### 型を足すだけでは足りない — 測定者を固定する (R2 review j#96350)

R1 で bool を型へ変えた 4 入力のうち 2 つは、**値を caller が供給し続けていた**ため R2 でも
自己申告のままだった。forged な `IntegrationWorktree(is_lane_worktree=False)` も、存在しない anchor を
指す `CoordinatorConfirmation` も、そのまま通った。

> **safety fact を測るのは actuator であり、依頼者ではない。**

R3 ではこの 2 つを preflight の入力から外し、actuator が action-time に自分で測る。

R3 は 2 field だけを測り、残りを caller から取っていた。R3 review j#96368 finding 1/2 が
「**2 項目だけ測っても、残りが caller 供給なら mutation authority は依然 caller のもの**」と指摘し、
cleanup 側では **foreign lane の worktree 削除と branch 削除**が caller boolean だけで再現された。

**R4 で caller preflight を廃止した。** `run_integration` / `run_cleanup` は preflight 引数を持たない。
caller が渡すのは action record (identity) と、この actuator 自身の lane 設定だけである。

| 事実の種類 | 誰が測るか |
| --- | --- |
| git 事実 (target head / ancestry / dirty / registered / tip / checked-out / origin 到達) | actuator が `AutoIntegrationGitOperations` の read probe で測定 |
| durable 事実 (review generation・reviewed head・target identity・callback・owner gate・CI) | actuator が `DurableAuthorityReader` port から action-time に読む |
| lane identity (これは自分の lane か) | actuator 自身の `lane_worktree` / `lane_branch` と照合 |
| patch equivalence | **測定できない**。明示 evidence を要する主張であり、probe が無いので提供しない |

authority reader 未注入なら durable 事実は何も確立されず、`integrated` にも `retired` にも到達しない
(fail-closed)。cleanup は record の path/branch が **actuator 自身の lane と exact 一致**しない限り
`foreign_worktree` で止まる — CAS tip 一致は「branch が動いていない」ことしか言わず「それが自分のものか」を
言わないためである。

### 測定は step ごとに取り直す (R5 review j#96385 findings 2/3)

**一度だけ測った snapshot は、その後のすべての mutation にとって stale である。** しかも actuator が
作用する世界は **actuator 自身の mutation が変える** 世界である。R5 まではこれを取り違えていた:

- push が成功すると remote target は `expected_target_head` から landed head へ移る。それを
  pre-push の期待値と比べていたため、**自分の成功を drift と誤判定して resume が恒常的に止まった**
  (feature が完了不能だった)。→ **pre-push は expected-head CAS、post-push は landed-head が
  現在の target から到達可能か**、と質問を分けた。到達不能は `integration_lost_from_target`
  (「誰かが先に動かした」= drift とは別の事実、「我々の成果が消えた」)。
- `already_integrated` は **push 前にのみ** terminal disposition である。push 後は source が target
  から到達可能なのは当然であり、そこで終了すると exact-SHA CI gate を飛ばしてしまう。
- cleanup も remove と delete の間で世界が変わる。`branch_checked_out_elsewhere` は **削除対象の
  worktree 自身も数える**ため、remove 前後で答えが違う。両 machine とも **step ごとに再測定**する。

> **test double が mutation の効果を反映しないと、検査そのものが無効になる。**
> R5 の 2 件はどちらも、fake が push 後も target head を静止させ、remove 後も checkout 状態を
> 固定していたために test をすり抜けた。mutating port の fake は自分の mutation を世界へ適用する。

### ledger は provenance と順序を持つ (R3 review j#96368 finding 3)

`StepOutcome` は `recorded_by` を持ち、actuator は **自分が記録した entry しか数えない**。
さらに mutation の前に `ledger_integrity_errors` が dependency order と必須 head を検査する。
push を apply より前に記録した ledger では、R3 は apply だけ実行して **push せずに `integrated`** へ
到達していた。merge の push は自 run の apply が生んだ commit を押す。source head への fallback は
**decision 層と mutation 層の両方から**除去した (R3 は decision 層しか直していなかった)。

### `coordinator_confirmed` mode は提供しない (R3 review j#96368 finding 4)

R3 は confirmation resolver を port として置いたが production binding が無く、mode は live 実行不能で、
任意の injected resolver が架空 anchor を保証できた。reviewer が示した 2 択のうち「配線完了まで
mode を非提供にする」を採った。**follow-up が実装すべき契約**は次のとおり: anchor を action-time に
fresh-read し、その記録が **この exact action key** を confirm していることを確認し、`issuer_role` は
**記録の author から導出**する (caller が名乗った role は authority ではない)。

domain は IO を持たず、事実は全て caller が preflight として渡す ([[logic-object-oriented-architecture-policy]]
の pure core 方針、既存 `domain/sublane_integration_policy.py` と同じ形)。use case は
**decision が authorize した step だけ**を 1 回ずつ実行し、outcome を ledger へ積む。

CI は actuator が actuate しない。統合 SHA の CI は非同期 gate であり、use case は
`pending` を記録して停止する。呼び手は run が **決着した後**に `done` として記録し、verdict を
`integration_ci_green` として渡す。「run が決着した」と「run が green だった」は別の事実である。

## 設定 (`.mozyo-bridge/config.yaml` の `auto_integration`)

```yaml
auto_integration:
  mode: disabled            # auto | disabled (既定 disabled)
  integration_branch: null  # 未設定は runtime 解決。設定時は action の target と exact 一致必須
  ff_only: true             # 既定 (j#96335)
  remove_worktree: true
  delete_local_branch: true
```

`delete_remote_branch` key は存在しない (上記「破壊的 step の safety 条件」参照)。宣言すると
unknown key として fail-closed する。

**CI key も存在しない。** R2 は `require_source_ci` / `require_integration_ci` を持ち、
「j#96335 が『branch/target CI』を設定駆動項目に列挙している」ことを根拠に waiver を owner 授権済み
と主張した (dispute j#96346)。R2 review j#96350 finding 1 がこれを否とし、**私はその判断を受け入れて
dispute を撤回した** (j#96351)。理由は 2 つある。

1. **anchor の数が逆だった。** j#77124 state 5 (`integrated: origin reachability + exact-SHA CI
   green を確定`)、j#96335 自身の target flow (`... → exact integration SHA CI green → Close Gate`)、
   j#96337 の fail_closed (`CI未確定`) の 3 つが「integrated には CI green が要る」と述べている。
   「branch/target CI を設定駆動」は **どの** required check を要求するかの設定とも読め、その読みなら
   j#96335 は自己整合する。私の読みは同 journal を自己矛盾させる読みだった。
2. **waiver に downstream semantics が無かった。** cleanup は統合 SHA の CI green を常時要求する。
   waiver 後の lane は cleanup が永久 block するか、未実行 CI を green と自己申告して破壊的 step へ
   進むかのどちらかになる。end-to-end で成立しない gate は gate ではない。

既定 `mode: disabled` は **behavior-preserving** である。#13686 以前は auto-integration が存在
しなかったため、block を宣言しない repo は従来どおり完全手動の coordinator 統合を保つ。

config は **operational intent のみ**を持つ。state machine は判断時に config の field を読まない
ため、flag は step を止められても gate を外せない。閉じた key 集合には force push / rebase /
approval / review / close を表す key が構造上存在せず、boundary 形の key
(`owner` / `approval` / `review` / `close` / `route` / `send` / credential 等) は既存の
closed-schema screen が拒否する。宣言状況は `mozyo-bridge config status` の
`auto_integration.*` leaf row で読める (未宣言の実効値が推測ではなく表示される)。

## scope 境界 / 未了

- **CLI subcommand 結線は本 tranche に含まない。** `application/cli_sublane_retire.py` が
  #14755 の保護 path であるため、R1 では library + config + docs + preset 層までとした。
  actuator を運用 command から呼ぶ結線は follow-up。
- **Herdr live smoke / live acceptance は未実施。** #13686 受入条件の残余として親 #12603 へ
  引き継ぐ。
- `domain/sublane_integration_policy.py` (#12604) の retire 判断は **置き換えていない**。本 doc の
  2 machine はその隣に新設したものであり、既存 `sublane retire` の挙動は変えていない。両者の
  統合 / 片寄せは別 issue の判断とする。

## 検証

- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_policy`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_retirement_cleanup_policy`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_live_ops`
  (live adapter が構成する実 argv と refusal。`_run` を stub した hermetic test で、実 git process は起動しない)
- R1 review j#96344 の 5 finding は `R1ReviewFindingRegressionTest` /
  `R1ReviewFinding1RegressionTest` / `NoRemoteRefDeleteTest` に、**再現した入力そのもの**で
  pin してある。verdict は j#96345。
- R2 review j#96350 の 4 finding は `R2ReviewFindingRegressionTest` および
  `CoordinatorConfirmationResolutionTest` / `MergeCommitRunTest` に同様に pin してある。
  verdict と full-surface escalation の受け入れは j#96351。
- `python3 -m unittest tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_actuator`
- `python3 -m unittest tests.unit.e_130_governance_distribution.f_140_rules_docs_catalog.test_auto_integration_config`
- `PYTHONPATH=src python3 -m mozyo_bridge docs validate --repo .` ほか catalog 検証一式。
- preset 本文を変えたため `PYTHONPATH=src python3 -m mozyo_bridge scaffold canonical --check` と
  `scaffold status --target .` を通す (canonical body → packaged preset → repo-local preset の
  3 段同期。詳細は [[logic-scaffold-rules]])。
