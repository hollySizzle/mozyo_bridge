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
| `integrated` | push 済み + 統合 SHA の CI green | push |
| `already_integrated` | target ancestry (source head が target から到達可能) | なし |
| `patch_equivalent` | **明示的な patch-id evidence** | なし |
| `not_applicable` | 非 Git workspace | なし (process retire は別途走る) |
| `coordinator_confirmation_required` | `mode: coordinator_confirmed` で確認未取得 | なし |
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
  - review_generation_inadmissible      # 最新 generation が approved かつ blocking finding なし
  - source_mutated_after_review         # review 済みの exact head であること
  - source_head_unreachable             # origin 到達可能
  - unpushed_unique_commits / dirty_worktree
  - source_ci_not_green                 # 設定で必須化を外せる
  - unresolved_owner_gate / unresolved_callback
  - target_drift                        # expected_target_head からの drift
  - non_fast_forward                    # ff-only 時
  - merge_conflict                      # merge commit disposition 時
  - push_rejected / integration_ci_failed
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
- remote branch delete は既定 false。有効化しても local の CAS 条件は緩まない。
- 非 Git workspace では worktree / branch step を明示的に `not_applicable` とし、process retire
  だけを独立に実行する。

## 実装構成

```text
domain/auto_integration_policy.py        pure: mode gate / action record / integration 状態遷移
domain/retirement_cleanup_policy.py      pure: close 後の cleanup 状態遷移と CAS 条件
application/auto_integration_actuator.py port (Protocol) + use case + config→policy 変換
application/auto_integration_live_ops.py live subprocess adapter (実 git)
```

domain は IO を持たず、事実は全て caller が preflight として渡す ([[logic-object-oriented-architecture-policy]]
の pure core 方針、既存 `domain/sublane_integration_policy.py` と同じ形)。use case は
**decision が authorize した step だけ**を 1 回ずつ実行し、outcome を ledger へ積む。

CI は actuator が actuate しない。統合 SHA の CI は非同期 gate であり、use case は
`pending` を記録して停止する。呼び手は run が **決着した後**に `done` として記録し、verdict を
`integration_ci_green` として渡す。「run が決着した」と「run が green だった」は別の事実である。

## 設定 (`.mozyo-bridge/config.yaml` の `auto_integration`)

```yaml
auto_integration:
  mode: disabled            # auto | coordinator_confirmed | disabled (既定 disabled)
  integration_branch: null  # 未設定は runtime 解決。解決不能は fail-closed
  ff_only: true             # 既定 (j#96335)
  require_source_ci: true
  require_integration_ci: true
  remove_worktree: true
  delete_local_branch: true
  delete_remote_branch: false   # 既定 false (j#96335)
```

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
- `python3 -m unittest tests.integration.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.test_auto_integration_actuator`
- `python3 -m unittest tests.unit.e_130_governance_distribution.f_140_rules_docs_catalog.test_auto_integration_config`
- `PYTHONPATH=src python3 -m mozyo_bridge docs validate --repo .` ほか catalog 検証一式。
- preset 本文を変えたため `PYTHONPATH=src python3 -m mozyo_bridge scaffold canonical --check` と
  `scaffold status --target .` を通す (canonical body → packaged preset → repo-local preset の
  3 段同期。詳細は [[logic-scaffold-rules]])。
