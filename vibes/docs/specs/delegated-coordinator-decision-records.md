# 委譲管制塔の decision / callback / correction record

Redmine #12398 / US #12392 / Feature #12386 (`Delegated Coordinator / Nested Handoff`)。

親 `coordinator` が子 project / child workspace へ委譲するかどうかを判断し、
`delegated_coordinator` が子 project 内で dispatch / no-dispatch を判断し、孫
implementation lane を使うかどうかを決め、handoff-worthy state を親 coordinator
route へ callback し、安全 invariant が破られた場合に correction を残す。これらの
判断が durable record に落ちないと、親 coordinator は callback / audit 時に親→子
委譲判断と子→孫 dispatch 判断を検査できず、parent close / owner approval / release
gate の bypass を事後に検出・是正できない。

本書は委譲管制塔の **decision / callback / correction record schema の正本** である。
record の固定 fields と許容 vocabulary を定義する。role 語彙・責務境界・実行 spine
は既存正本を読み、本書に複製しない。

## 既存正本との関係

本書は schema だけを足す。次の正本と重なる規約本文は複製しない。

- role 語彙 (`coordinator` / `delegated_coordinator` / `implementation_gateway` /
  `implementation_worker`) と parent close・owner approval・callback・downstream
  dispatch の責務境界は `vibes/docs/specs/delegated-coordinator-role-profile.md`
  (以下 role-profile spec) を正本とする。
- 帯域 / admission / pipeline fill / drain order、`## Sublane dispatch decision`
  bandwidth record template、孫 dispatch を `purpose: preserve_coordinator_context`
  として判断する policy、no-dispatch 記録粒度の base 方針は
  `vibes/docs/logics/coordinator-sublane-development-flow.md` (以下 spine) を正本と
  する。
- 子 coordinator / 孫 lane の window 分離・cockpit 表示・parent reference metadata は
  `vibes/docs/logics/delegated-coordinator-cockpit-display.md` を正本とする。
- release / publish の owner gate は `vibes/docs/rules/release-distribution.md`、
  durable record の public/private 境界は `vibes/docs/rules/public-private-boundary.md`
  を正本とする。

本書が足すのは、親→子委譲の **parent delegation decision fields**、spine の単一
project bandwidth record に対する **委譲 (delegation) 識別 fields**、親が子の
dispatch 判断を検査するための **parent-inspectable callback fields**、安全 invariant
bypass の **correction record** である。

## 用語

- `parent_coordinator_route`: 委譲元である最上位 `coordinator` (親管制塔) への
  durable callback 経路。anchor pointer であり pane 配置ではない。
- `owning_us_coordinator_route`: 子 project 側の owning US / audit coordinator への
  durable callback 経路。親 project の `parent_coordinator_route` と異なる場合が
  ある。anchor pointer であり pane 配置ではない。
- `callback_target`: handoff-worthy state を通知すべき route anchor。各 target は
  `purpose` を持つ。少なくとも `delegation_parent` と
  `owning_us_coordinator` / `audit_coordinator` を区別する。
- `child_coordinator_route`: 親 `coordinator` が委譲先として使う子 project /
  child workspace の `delegated_coordinator` gateway route。anchor pointer であり
  pane 配置ではない。
- `delegated_coordinator_lane`: 子 project の `delegated_coordinator` lane identity。
  workspace / lane / role の pointer で表し、private path は書かない。
- `grandchild lane`: `delegated_coordinator` が downstream dispatch する
  `implementation_worker` lane。spine の孫 (grandchild) に対応する。

すべての record は Redmine issue / journal に残す。fields には issue ID と state
token を書き、private path や operator-specific cockpit details は書かない
(public/private boundary)。

## 1. parent delegation decision record

親 `coordinator` が子 project / child workspace に implementation-shaped work を委譲
するか、または委譲せず親 lane / child workspace Claude へ直接流すかを判断したときの
durable record。目的は、親→子 gateway を使わない判断が bootstrap 例外なのか、単なる
role bypass なのかを後から検査できるようにすることである。

```markdown
## Parent delegation decision

- record_kind: parent_delegation_decision
- parent_coordinator: <親 coordinator route / lane pointer>
- parent_issue: <親 issue / US id>
- child_project: <子 project / child workspace identifier>
- child_issue: <子 project 側 issue id | not_created | not_applicable>
- child_coordinator_route: <delegated_coordinator route anchor | unavailable | not_adopted>
- child_delegation: <used | avoided | not_applicable>
- child_delegation_anchor: <子 coordinator への durable handoff anchor | not_applicable>
- no_child_delegation_reason: <下表の token | not_applicable>
- follow_up_validation: <子 route / sublane replay を確認する issue / journal anchor | none>
- correction_required: <false | true:process_gap_correction_required>
```

要求粒度:

- **記録不要 (parent-context-neutral default)**: 親 coordinator の read-only 調査、
  ticket triage、owner-facing explanation、親 project 内だけで完結する判断は、専用の
  parent delegation decision を要求しない。
- **記録必須**: 子 project の実装 file / scaffold / runtime entrypoint に影響する
  work、child workspace Claude への直接 `implementation_request`、子 coordinator
  gateway を使うべき workflow の bypass、bootstrap / child gateway unavailable /
  child route not adopted を理由に main/default lane で実装する判断は、実装 handoff
  前に parent delegation decision を残す。
- **borderline は具体理由**: 子 project の成果物を変えるか、子 coordinator の authority
  を迂回するかが曖昧な場合は、`child_delegation: avoided` と具体的な
  `no_child_delegation_reason` を記録し、監査者が後で妥当性を判定できるようにする。

`no_child_delegation_reason` の許容 vocabulary:

| token | 意味 |
| --- | --- |
| `bootstrap_child_route_not_adopted` | 子 project の router / scaffold / delegated coordinator route を整備する前段作業であり、子 gateway がまだ正規入口として成立していない。follow-up validation を必ず書く。 |
| `child_gateway_unavailable` | 子 coordinator pane / route / identity gate が利用不能。利用不能事実と retry / recovery anchor を併記する。 |
| `child_scope_not_implementation` | 子 project に関する read-only 調査、ticket 整理、owner-facing explanation であり、実装成果物を変えない。 |
| `urgent_minimal_correction` | release / CI / publish 等の進行を止めるための最小 correction。緊急性と後続 re-audit anchor を併記する。 |
| `<具体記述>` | 上記に当てはまらない borderline 判断。なぜ子 coordinator を使わない方が安全かを具体化する。 |

固定境界:

- `child_delegation: avoided` は親が子 close / owner approval / release gate を
  self-authorize する根拠にならない。close / approval / release authority は
  role-profile spec と governed preset のまま維持する。
- `bootstrap_child_route_not_adopted` は一度きりの導入例外であり、同じ child project で
  繰り返し使う場合は process gap として扱う。bootstrap 後の通常実装は
  `child_coordinator_route` へ委譲するか、使わない理由を再記録する。
- 子 workspace Claude へ直接 implementation_request を送る場合でも、pane message は
  Redmine anchor への pointer であり、Implementation Done / Review Request / callback
  は durable record に残す。
- 親→子の delegation decision は、後続の delegated callback または parent audit が
  検査できる durable anchor に置く。anchor を欠いた child gateway bypass は process
  gap として §5 correction record の対象になる。

## 2. dispatch decision record (child dispatch decision)

`delegated_coordinator` が子 issue の implementation-shaped work を孫 lane へ
dispatch すると判断したときの durable record。spine の `## Sublane dispatch decision`
bandwidth record template の fields をそのまま使い、本書はその上に delegation 識別
fields を **追加 block** として先頭に足す。admission / pipeline fill / fill_decision
の語彙と判断規則は spine を正本とし、ここで再定義しない。

dispatch decision record は次の追加 fields を持つ。

```markdown
## Delegated dispatch decision

- record_kind: delegated_dispatch_decision
- delegated_coordinator: <child project の delegated_coordinator lane pointer>
- parent_coordinator_route: <親 coordinator callback route anchor>
- parent_issue: <親 issue / US id（close authority は親に残る）>
- child_issue: <dispatch 対象の子 issue id>
- grandchild_lane: <孫 implementation lane identity | not_applicable>
- dispatch_anchor: <孫 lane への durable dispatch anchor pointer>
- delegation_depth: <shallow（監査可能な浅さ。無限階層は invalid）>
<spine の `## Sublane dispatch decision` fields をここに続けて記録する>
```

固定境界:

- `delegated_coordinator` の downstream dispatch は role-profile spec のとおり
  **shallow delegation のみ**。`delegation_depth` は監査可能な浅さを示し、無限階層を
  作る dispatch は invalid とする。
- 孫 dispatch を選ぶかどうかは spine の `### 孫 dispatch / context 保護` を正本とし、
  主目的は `purpose: preserve_coordinator_context`。本 record は spine の
  `grandchild_dispatch` / `purpose` field をそのまま継承する。
- この record は `parent_coordinator_route` から到達できる durable anchor に置く。
  親 coordinator が callback / audit 時に子の dispatch 判断を検査できることが要件で
  あり、後述の parent callback record はこの record の anchor を参照する。

## 3. no-dispatch decision record

孫を使わない判断を毎回 journal 化すると durable record の信号対雑音比が下がる。
記録粒度の base 方針は spine の `#### no-dispatch 記録の粒度` を正本とする。本書は
`delegated_coordinator` 文脈での **要求条件と許容理由 vocabulary** を固定する。

要求粒度 (spine と整合):

- **記録不要 (context-neutral default)**: read-only investigation / ticket-only
  update / 小さい journal update を `delegated_coordinator` が自 lane で処理する場合、
  専用の no-dispatch journal を要求しない。これらは context 圧迫が無い default で
  あり、明示記録なしで進めてよい。
- **dispatch decision へ 1 行追記 (context-consuming だが自 lane 処理)**: spine の
  孫 dispatch 候補条件 (long diff / long test log / iterative trial / 大量 journal
  読解 / parent callback context 保持) のいずれかに触れる work を孫へ出さず
  `delegated_coordinator` 自身が処理する場合、新しい独立 journal を起票せず、上記
  dispatch decision record に `grandchild_dispatch: avoided` と `no_dispatch_reason`
  を 1 行追記する。
- **borderline のみ理由を具体化**: context を消費しそうだが coordinator が context
  を保持したい borderline 判断のときだけ `no_dispatch_reason` を具体化する。

`no_dispatch_reason` の許容 vocabulary:

| token | 意味 |
| --- | --- |
| `context_cost_low` | context 圧迫が小さく、自 lane 処理で pointer / 判断を失わない。 |
| `single_pass_no_iteration` | 試行錯誤の往復が無く、中間状態が context に積もらない。 |
| `urgent_minimal_correction` | spine `### Admission Rule` 例外の urgent minimal correction。durable reason を併記する。 |
| `<具体記述>` | borderline 判断。上記に当てはまらない場合のみ具体理由を書く。 |

no-dispatch decision は「context を消費し得る work を孫に出さなかった非自明判断」に
集中させ、context-neutral な default work には記録を課さない。

## 4. parent callback record

`delegated_coordinator` が handoff-worthy state を `parent_coordinator_route` へ
callback するときの durable record。目的は、親 `coordinator` が callback / audit 時に
**親→子 delegation 判断と子→孫 dispatch 判断を検査できる**ことである。callback は
durable anchor への pointer であり work log ではない (spine / role-profile spec)。

### 4.1 callback target model

単純な親子委譲では `parent_coordinator_route` だけで足りるが、既存 project の
external-submodule adoption や multi-project cockpit では、委譲元の親 coordinator と
子 project の owning US / audit coordinator が別 lane になることがある。この場合、
単一 `callback_route` だけでは片方の coordinator が durable state を見落とし、
US-level audit / child disposition が停止して見える。

そのため delegated callback は、必要な callback 先を **目的付き target 群**として
記録する。各 target は pane id ではなく route anchor として扱い、private runtime
identifier は journal 上の実機結果にだけ書く。

```markdown
## Delegated callback targets

- record_kind: delegated_callback_targets
- source_state: <implementation_done | review_request | review_result | owner_close_approval_waiting | blocked>
- child_issue: <子 issue id>
- parent_issue: <親 issue / US id>
- callback_targets:
  - purpose: delegation_parent
    route: <parent_coordinator_route>
    required: true
    outcome_anchor: <callback outcome journal | pending | blocked | not_applicable>
  - purpose: owning_us_coordinator | audit_coordinator
    route: <owning_us_coordinator_route>
    required: <true | false>
    outcome_anchor: <callback outcome journal | pending | blocked | not_applicable>
- pass_condition: <all_required_callback_outcomes_recorded | blocked_with_replayable_retry>
```

固定境界:

- `delegation_parent` は親 project の close / owner approval authority を持つ
  coordinator への通知である。
- `owning_us_coordinator` / `audit_coordinator` は子 project 側の US-level audit /
  child disposition を進める coordinator への通知である。
- 両者が同一 route なら target を統合してよいが、同一であることを durable record に
  明記する。推測で省略しない。
- required target の callback outcome が `sent` / `blocked` / `not_applicable` の
  いずれでも記録されていない状態を PASS / complete としない。`blocked` は候補と
  retry command を持つ replayable block の場合だけ受理する。
- same-lane surfacing はどの target の outcome にも数えない。

```markdown
## Delegated callback

- record_kind: delegated_callback
- from: <delegated_coordinator_lane>
- to: <parent_coordinator_route>
- callback_targets_anchor: <Delegated callback targets anchor | same_as_parent_only>
- state: <implementation_done | review_request | review_result | owner_close_approval_waiting | blocked>
- child_issue: <子 issue id>
- parent_issue: <親 issue / US id>
- child_dispatch_anchors:
  - <parent delegation decision anchor（あれば）>
  - <dispatch decision record anchor>
  - <no-dispatch を追記した dispatch decision anchor（あれば）>
- dispatch_judgment_summary: <子の dispatch / no-dispatch 判断の 1 行 pointer。work log にしない>
- owner_approval_waiting: <yes | no>
- correction_needed: <none | parent_close_bypass | owner_approval_bypass | release_gate_bypass>
- durable_anchor: <この callback の anchor>
```

固定境界:

- `state` の vocabulary は role-profile spec の handoff-worthy state
  (implementation_done / review_request / review_result / owner_close_approval_waiting
  / blocked) に従う。
- `callback_targets_anchor` が `same_as_parent_only` 以外の場合、required target 全件の
  outcome が記録されるまで callback は不完全である。単一 parent callback だけで
  owning US / audit coordinator への pointer を代替しない。
- `child_dispatch_anchors` は本書 §1 / §2 / §3 の record anchor を列挙する。親
  coordinator はこの anchor から親→子 delegation と子→孫 dispatch / no-dispatch
  判断を検査する。anchor を欠いた callback は「委譲判断が検査不能」であり、
  callback として不完全とする。ただし §3 の context-neutral default で専用
  no-dispatch record を要求しない場合は、`dispatch_judgment_summary` に
  `context-neutral default` を明記して検査可能にする。
- `owner_approval_waiting: yes` の場合、`delegated_coordinator` は自 lane で owner
  approval を solicit / collect / ratify しない (role-profile spec の安全 invariant)。
  親 coordinator route の単一 aggregation point へ戻す。
- callback / audit で安全 invariant 違反を観測した場合は `correction_needed` に
  bypass kind を立て、§5 の correction record を起票する。

## 5. process gap correction record

子 (delegated_coordinator または downstream) が安全 invariant を bypass した場合の
是正 record。role-profile spec の固定 invariant — owner approval の単一 aggregation
point、parent issue close は最上位 `coordinator` のみ、handoff-worthy state の durable
callback — のいずれかが破られたときに使う。観測は親 coordinator の callback / audit、
または auditor が行う。

correction path は次の 3 つの bypass を必ず cover する。

```markdown
## Process gap correction

- record_kind: process_gap_correction
- gap_kind: <parent_close_bypass | owner_approval_bypass | release_gate_bypass>
- observed_by: <parent_coordinator_callback | audit | auditor>
- bypassing_actor: <delegated_coordinator | implementation_gateway | implementation_worker>
- affected_anchor: <不正に close / approve / release した issue / US / Version>
- correction_action: <下表のいずれか>
- restored_authority: <parent_close=最上位 coordinator | owner_approval=親 aggregation point の owner | release=owner>
- follow_up: <re-audit / re-route / hold の durable anchor>
- durable_anchor: <この correction の anchor>
```

bypass kind と correction path の対応:

| `gap_kind` | 破られた invariant | `correction_action` | 復元先 authority |
| --- | --- | --- | --- |
| `parent_close_bypass` | parent issue close は最上位 `coordinator` のみ | 親 issue / US を reopen し、子の close を無効化する。close authority を最上位 `coordinator` に戻す。 | 最上位 `coordinator` |
| `owner_approval_bypass` | owner approval は単一 aggregation point、子 lane で ratify しない | 子 lane の owner approval を revoke し、owner-approval-waiting を親 coordinator route の単一 aggregation point へ re-route する。owner が一度だけ判断する。 | 親 aggregation point の owner |
| `release_gate_bypass` | release / publish は owner gate (release-distribution rule) | release / publish を hold し、`vibes/docs/rules/release-distribution.md` の owner gate を復元する。実害が出ていれば roll back / 是正を owner 判断へ戻す。 | owner |

固定境界:

- correction record は bypass を「無かったこと」にしない。bypass の観測・是正・復元先
  authority を durable anchor に残し、必要なら re-audit を `follow_up` に書く。
- correction は子の close / approval / release を **self-authorize しない**。是正後の
  正規 close / owner approval / release は、それぞれの正本 (role-profile spec /
  spine / release-distribution rule) の正規経路で取り直す。
- correction record も public/private boundary に従い、issue ID / state token /
  anchor pointer で表し、private path や secret を書かない。

## 安全 invariant (固定)

project policy knob は本書の record fields を省略してよい根拠にならない。

- handoff-worthy state は parent callback record を残す。`child_dispatch_anchors` を
  欠いた callback は、親→子 / 子→孫の委譲判断が検査不能であり完了扱いにしない。
  context-neutral default で anchor が無い場合でも、`dispatch_judgment_summary` に
  その理由を明記する。
- multi-project / external-submodule adoption では、`delegation_parent` と
  `owning_us_coordinator` / `audit_coordinator` の callback target を明示し、required
  target 全件の outcome を残す。単一 `callback_route` による parent callback だけでは
  multi-coordinator callback coverage を満たさない。
- owner approval は親 coordinator route の単一 aggregation point に戻す。子 lane で
  ratify した場合は §5 `owner_approval_bypass` の correction を起票する。
- parent issue close は最上位 `coordinator` のみ。子が close した場合は §5
  `parent_close_bypass` の correction を起票する。
- release / publish の owner gate を子が bypass した場合は §5 `release_gate_bypass`
  の correction を起票する。

## 参照正本

- `vibes/docs/specs/delegated-coordinator-role-profile.md` (role 語彙・責務境界・安全 invariant)
- `vibes/docs/logics/coordinator-sublane-development-flow.md` (dispatch / bandwidth / 孫 dispatch / no-dispatch 粒度 spine)
- `vibes/docs/logics/delegated-coordinator-cockpit-display.md` (子 coordinator / 孫 lane の display 投影)
- `vibes/docs/rules/release-distribution.md` (release / publish owner gate)
- `vibes/docs/rules/public-private-boundary.md` (durable record の public/private 境界)
- `vibes/docs/rules/agent-workflow.md`
- `skills/mozyo-bridge-agent/references/workflow.md`
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md`
- `.mozyo-bridge/rules/llm_rule_authoring.md`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/specs/delegated-coordinator-decision-records.md --repo . --format text`
