# 委譲管制塔の decision / callback / correction record

Redmine #12398 / US #12392 / Feature #12386 (`Delegated Coordinator / Nested Handoff`)。

`delegated_coordinator` が子 project 内で dispatch / no-dispatch を判断し、孫
implementation lane を使うかどうかを決め、handoff-worthy state を親 coordinator
route へ callback し、安全 invariant が破られた場合に correction を残す。これらの
判断が durable record に落ちないと、親 coordinator は callback / audit 時に子の
dispatch 判断を検査できず、parent close / owner approval / release gate の bypass を
事後に検出・是正できない。

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

本書が足すのは、spine の単一 project bandwidth record に対する **委譲 (delegation)
識別 fields**、親が子の dispatch 判断を検査するための **parent-inspectable callback
fields**、安全 invariant bypass の **correction record** である。

## 用語

- `parent_coordinator_route`: 委譲元である最上位 `coordinator` (親管制塔) への
  durable callback 経路。anchor pointer であり pane 配置ではない。
- `delegated_coordinator_lane`: 子 project の `delegated_coordinator` lane identity。
  workspace / lane / role の pointer で表し、private path は書かない。
- `grandchild lane`: `delegated_coordinator` が downstream dispatch する
  `implementation_worker` lane。spine の孫 (grandchild) に対応する。

すべての record は Redmine issue / journal に残す。fields には issue ID と state
token を書き、private path や operator-specific cockpit details は書かない
(public/private boundary)。

## 1. dispatch decision record (child dispatch decision)

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

## 2. no-dispatch decision record

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

## 3. parent callback record

`delegated_coordinator` が handoff-worthy state を `parent_coordinator_route` へ
callback するときの durable record。目的は、親 `coordinator` が callback / audit 時に
**子の dispatch 判断を検査できる**ことである。callback は durable anchor への pointer
であり work log ではない (spine / role-profile spec)。

```markdown
## Delegated callback

- record_kind: delegated_callback
- from: <delegated_coordinator_lane>
- to: <parent_coordinator_route>
- state: <implementation_done | review_request | review_result | owner_close_approval_waiting | blocked>
- child_issue: <子 issue id>
- parent_issue: <親 issue / US id>
- child_dispatch_anchors:
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
- `child_dispatch_anchors` は本書 §1 / §2 の record anchor を列挙する。親 coordinator
  はこの anchor から子の dispatch / no-dispatch 判断を検査する。anchor を欠いた
  callback は「子の判断が検査不能」であり、callback として不完全とする。
- `owner_approval_waiting: yes` の場合、`delegated_coordinator` は自 lane で owner
  approval を solicit / collect / ratify しない (role-profile spec の安全 invariant)。
  親 coordinator route の単一 aggregation point へ戻す。
- callback / audit で安全 invariant 違反を観測した場合は `correction_needed` に
  bypass kind を立て、§4 の correction record を起票する。

## 4. process gap correction record

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
  欠いた callback は、子の dispatch 判断が検査不能であり完了扱いにしない。
- owner approval は親 coordinator route の単一 aggregation point に戻す。子 lane で
  ratify した場合は §4 `owner_approval_bypass` の correction を起票する。
- parent issue close は最上位 `coordinator` のみ。子が close した場合は §4
  `parent_close_bypass` の correction を起票する。
- release / publish の owner gate を子が bypass した場合は §4 `release_gate_bypass`
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
