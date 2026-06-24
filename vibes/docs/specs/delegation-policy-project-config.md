# delegation policy project config

Redmine #12397 / US #12390 / Feature #12386 (`Delegated Coordinator / Nested Handoff`)。

delegated coordinator / grandchild dispatch を「使うかどうか」「どこまで深く使うか」は project や operator 環境で異なる。mozyo-bridge は設定駆動を前提に、運用しきい値を project ごとに調整できる必要がある。本書は delegation policy の **project config knob schema** と、緩めてよい運用しきい値・緩めてはいけない safety invariant の境界を定義する repo-local の一次正本である。

## 目的

- delegation policy の knob (`enable_delegated_coordinator` / `enable_grandchild_dispatch` / `max_delegation_depth` / `max_active_child_lanes` / window policy / decision record policy) を project 設定の field contract として固定する。
- 設定で緩めてよい運用しきい値と、project policy で緩めてはいけない固定 safety invariant を分離する (受入条件)。
- default を浅い階層・安全側に倒す。delegation は opt-in であり、設定が無い project は従来どおりの単一 coordinator + sublane spine で動く。

## scope と非 scope

- 本書の scope は config knob の schema field contract、default、fail-closed 規約、固定 invariant との境界の定義に限る。
- config の **runtime parser / loader / resolver** 実装は本書では行わない。`presentation` config と同じく schema 契約を先に固定し、loader 結線は follow-up code task に分ける (`## 実装状況と follow-up`)。
- window 分離方針 (`delegation_window_policy`) の表示語彙の正本は `vibes/docs/logics/delegated-coordinator-cockpit-display.md` + `vibes/docs/logics/unit-presentation-state-db.md` の `presentation` config である。本書はそれを delegation policy surface の一部として参照するだけで、表示 schema を再定義しない。
- role 語彙・責務境界の正本は `vibes/docs/specs/delegated-coordinator-role-profile.md`、孫 dispatch の context 保護判断の正本は `vibes/docs/logics/coordinator-sublane-development-flow.md` の `### 孫 dispatch / context 保護` である。本書はこれらの policy を「設定で何を調整でき、何を固定するか」の観点で束ねるだけで、判断基準そのものを複製しない。

## 設定の置き場所と層

delegation policy config は repo-local の `.mozyo-bridge/config.yaml` に `delegation:` top-level section として置く。`presentation:` と同じ層 (review 可能な desired declaration) であり、runtime truth ではない。

- config は **desired policy declaration**。実際の dispatch 可否は runtime preflight と spine の admission / bandwidth 判断が最終決定する (`unit-presentation-state-db.md` の desired/runtime 分離をそのまま継承)。
- config が無い / `delegation:` section が無い project は、後述の **safety-biased default** で動く。delegation は default で無効なので、config 不在は「従来の単一 coordinator + sublane spine」と等価である。
- config は portable / public-safe な値だけを持つ。private path / host topology / 個人名 / Redmine project 名・private lane naming を持たない (`rule-public-private-boundary`)。
- config と decision record は actuator ではない。`enable_grandchild_dispatch: true`
  や `delegation_window_policy: separate`、`delegated_dispatch_decision` が存在しても、
  runtime が実際に孫 lane/window/worktree を作成または明示採用し、live metadata を
  stamp / project しなければ 3-window display acceptance は満たさない。

## config knob schema (conceptual)

```yaml
delegation:
  version: 1
  enable_delegated_coordinator: false   # master gate。false なら nested delegation を行わない
  enable_grandchild_dispatch: false     # depth 2 (孫 lane) を許可するか
  max_delegation_depth: 1               # root からの委譲 hop 上限。hard ceiling 2 を超えない
  max_active_child_lanes: 1             # delegated coordinator が同時に保持できる child/grandchild lane 数
  decision_record_policy: minimal       # minimal | verbose。no-dispatch / context-neutral 記録の粒度のみを制御
  # window policy は presentation config が正本。ここでは再定義しない:
  #   presentation.grouping... delegation_window_policy: separate | shared
```

この shape は declarative metadata である。plugin manifest でも install surface でも dynamic predicate language でもない。

### depth 語彙

depth は `delegated-coordinator-cockpit-display.md` と共通とする。

```text
depth 0 = parent coordinator (delegation tree の root)
depth 1 = delegated coordinator (委譲された子 coordinator)
depth 2 = grandchild implementation lane (孫 lane)
```

`max_delegation_depth` は root からの委譲 hop の上限である。`0` = 委譲なし (単一 coordinator)、`1` = parent → delegated まで、`2` = grandchild まで。**hard ceiling は 2** (Feature #12386 の audit 可能な shallow delegation = 3 階層まで)。`2` を超える値は fail-closed で reject し、4 階層以上の運用 default を本書は定義しない。

## field contract

`delegation.version`
: Integer schema version。writer support 後は明示必須。unsupported newer version は config application を fail-closed する。

`delegation.enable_delegated_coordinator`
: bool。master gate。`false` のとき nested delegation を行わず、`max_delegation_depth` は実効 `0` に倒れる (parent coordinator + sublane の 2 階層 spine のみ)。default `false`。

`delegation.enable_grandchild_dispatch`
: bool。depth 2 (孫 lane) dispatch を許可するか。`enable_delegated_coordinator: false` のときは無意味 (常に無効)。`true` でも孫 dispatch の判断基準は spine の `purpose: preserve_coordinator_context` であり、本 flag は「許可するか」のみを与える。default `false`。

`delegation.max_delegation_depth`
: integer。root からの委譲 hop 上限。許可域 `0..2`。hard ceiling `2` を超える値、負値、非整数は invalid config。`enable_delegated_coordinator: false` のとき実効上限は `0` に clamp。default `1`。

`delegation.max_active_child_lanes`
: integer >= 1。1 delegated coordinator が同時に保持できる未退役 child/grandchild lane の上限。これは spine の bandwidth soft profile / admission を **置き換えない**。両者の min が実効 dispatch 上限であり、spine 側がより厳しければ spine が勝つ。default `1` (安全側)。

`delegation.decision_record_policy`
: enum `minimal` | `verbose`。delegation decision の **no-dispatch / context-neutral 記録の粒度のみ** を制御する。`minimal` = `### 孫 dispatch / context 保護` の granularity をそのまま採用 (context-neutral default work は記録不要、context を消費し得る work を孫に出さない非自明判断のみ 1 行追記)。`verbose` = borderline でない context-neutral 判断も記録する。**callback / gate / correction record の要件は invariant 側に属し、この knob では消せない** (下記)。default `minimal`。

### field ownership

| field family | owner | notes |
| --- | --- | --- |
| enable / depth / active-lane / record-policy knob | repo-local `delegation` config | desired policy declaration。public-safe |
| window 分離 (`delegation_window_policy`) | `presentation` config (`unit-presentation-state-db.md`) | 表示 knob。本書は参照のみ |
| 孫 dispatch trigger (`preserve_coordinator_context`) | spine (`coordinator-sublane-development-flow.md`) | config は許可域を与える。判断基準は spine |
| bandwidth soft profile / admission | spine | config の `max_active_child_lanes` と min を取る |
| role 語彙・責務境界 | `delegated-coordinator-role-profile.md` | config は role を再定義しない |
| owner approval / review / close / callback | Redmine governed workflow | never config truth (固定 invariant) |

## 緩めてよい運用しきい値 (project knob) と固定 safety invariant

受入条件の核心。次表で「設定で調整可能」と「project policy で緩めない固定 invariant」を分離する。

| 項目 | 区分 | default | 正本 |
| --- | --- | --- | --- |
| delegated coordinator を使うか (`enable_delegated_coordinator`) | project knob | `false` (opt-in) | 本書 |
| 孫 dispatch を許可するか (`enable_grandchild_dispatch`) | project knob | `false` | 本書 |
| 委譲 depth 上限 (`max_delegation_depth`, ceiling 2) | project knob (hard ceiling 固定) | `1` | 本書 |
| 同時 child/grandchild lane 数 (`max_active_child_lanes`) | project knob (spine と min) | `1` | 本書 + spine |
| window 分離 (`delegation_window_policy`) | project knob | `separate` | `delegated-coordinator-cockpit-display.md` |
| no-dispatch 記録粒度 (`decision_record_policy`) | project knob | `minimal` | 本書 + spine `### 孫 dispatch / context 保護` |
| owner approval 単一集約 (parent coordinator) | **固定 invariant** | — | role-profile / spine / preset `### Owner Close Approval Delegation` |
| parent issue close は最上位 coordinator のみ | **固定 invariant** | — | role-profile |
| durable anchor (Redmine journal) を work record とする | **固定 invariant** | — | preset / skill workflow |
| hidden subagent 禁止 (全 delegated/grandchild lane は宣言済み durable-anchored lane) | **固定 invariant** | — | 本書 + Feature #12386 設計方針 |
| durable callback 要件 (gate + callback outcome) | **固定 invariant** | — | role-profile / cockpit-display / skill workflow |

### 固定 invariant の明文化

どの knob を緩めても次は変わらない。これらは config schema に対応する key を持たず、設定値で無効化できない。

- **owner approval 単一集約**: owner-approval-waiting は常に最上位 `coordinator` の単一 aggregation point へ集約する。delegated coordinator は孫 lane の owner approval を自 lane で solicit / collect / ratify しない。`enable_*` を true にしても、`max_*` を上げても変わらない。
- **parent issue close**: 親 issue / 親 US の close authority は最上位 `coordinator` のみ。delegated coordinator は子 project 内 issue の close に限る。
- **durable anchor**: 新規 work の正本は Redmine issue / journal。pane scrollback / handoff chat / cockpit notification を work record の正本にしない。
- **hidden subagent 禁止**: delegation で開く child / grandchild lane は、Redmine の parent link + dispatch journal を持つ **宣言済み durable-anchored lane** でなければならない。coordinator の context 内に隠れた、durable anchor を持たない subagent / 影の lane を作らない。「context 圧迫回避」は宣言済み lane への dispatch で達成し、不可視な階層追加で達成しない。
- **durable callback 要件**: handoff-worthy state (implementation_done / review_request / review_result / owner_close_approval_waiting / blocked) は durable gate journal + callback outcome journal が揃って初めて完了。`decision_record_policy: minimal` が省略してよいのは **context-neutral な no-dispatch 判断の記録だけ**であり、callback / gate / correction record は省略対象外。

### knob 間の安全な相互作用

- master gate が優先: `enable_delegated_coordinator: false` のとき、他 knob の値に関わらず nested delegation は起きない (`max_delegation_depth` 実効 0、`enable_grandchild_dispatch` 無効)。
- depth と grandchild flag の整合: `max_delegation_depth < 2` または `enable_grandchild_dispatch: false` のいずれかで孫 lane は許可されない (AND 条件で初めて depth 2 が開く)。
- config は許可域のみを与える: knob を緩めても、実際の dispatch は spine の bandwidth admission / runtime preflight / `purpose: preserve_coordinator_context` 判断を通る。config は上限を「広げる」のではなく「狭める / 許可する」方向にのみ効き、固定 invariant を越えない。

## fail-closed / fallback matrix

`presentation` config と同じ fail-closed 規約を継承する。曖昧・不正・権限 shape の config は安全側 (delegation 抑制) に倒す。

| condition | result |
| --- | --- |
| config 不在 / `delegation:` section 不在 | safety-biased default を適用 (delegation 無効、単一 coordinator + sublane spine 等価) |
| unknown top-level field / unsupported newer `version` | config application を fail-closed し、invalid config diagnostic を surface。delegation は default (無効) に倒す |
| `max_delegation_depth` が `0..2` 外 / 負値 / 非整数 | invalid config。実効 depth を `0` に clamp し diagnostic を surface |
| `max_active_child_lanes < 1` / 非整数 | invalid config。実効 `1` に clamp |
| `enable_grandchild_dispatch: true` だが `max_delegation_depth < 2` | 孫 dispatch は不許可のまま (より厳しい側が勝つ)。diagnostic は任意 |
| authority-shaped key (owner approval / close / callback / route / target / pane / credential を制御しようとする key) | reject。固定 invariant は config truth にならない |
| config が live runtime と矛盾 | config は desired declaration。action permission は side-effecting command の live preflight が決める (`unit-presentation-state-db.md`) |

## Public / Private boundary

OSS default に入れてよいもの:

- generic な delegation knob 名 / enum 値 / default / depth ceiling 概念。
- 固定 invariant と project knob の対照表。
- fail-closed / fallback matrix。

入れてはいけないもの:

- private project の lane naming / delegation 運用 profile。
- operator 固有の window 並び / 個人名 / private host topology。
- private absolute path / secret-shaped value / Redmine project 名。
- 4 階層以上の private 運用 default (本書は hard ceiling 2 = 3 階層のみ)。

## 実装状況と follow-up

本 task は schema field contract と invariant 境界を固定する spec doc で完了する (role-profile #12387 / cockpit-display #12391 と同じ design-doc 完了モデル)。runtime 実装は別 issue / 別 lane に分け、Codex が review する。

候補 follow-up:

1. `.mozyo-bridge/config.yaml` の `delegation` section loader / parser (`presentation` config の `from_record` と同じ fail-closed pattern)。
2. delegation policy resolver: master gate / depth clamp / grandchild AND 条件 / `max_active_child_lanes` と spine bandwidth の min を解決する display-only / decision-support layer。route / approval / close authority を持たない。
3. invalid config diagnostic (unsupported version / out-of-range depth / authority-shaped key) の surface。
4. `decision_record_policy` を spine の `### 孫 dispatch / context 保護` 記録粒度へ結線。
5. delegation tree depth を Redmine parent link + dispatch journal から再導出し、config 上限との整合を検査する read model。
6. grandchild dispatch actuator: `delegated_dispatch_decision` を、宣言済み
   grandchild worktree/lane/window の作成または明示採用と live delegation metadata
   stamping (`KIND` / `DEPTH` / `PARENT`) へ接続する。decision record / same-lane
   worker handoff だけでは display full PASS としないことを regression smoke で固定する。
   (実装済み #12473: `mozyo-bridge handoff delegate-grandchild-stamp` が宣言済み
   delegation chain を検証し、grandchild が depth 2 の `implementation` lane に
   derive することを fail-closed で確認したうえで、discovery が読む
   `@mozyo_lane_kind` / `@mozyo_delegation_parent` を live pane へ stamp する。
   `delegation_depth` / `delegation_root` は parent chain から derive され pane
   option には書かない。stamp は display/audit breadcrumb のみで routing authority
   を持たない。regression は decision/same-lane-worker だけでは display が blank の
   ままで、stamp 後に初めて `KIND=implementation` / `DEPTH=2` / `PARENT` が出ることを
   固定する。)

各 task は runtime / tests を伴うため Claude implementer lane に回す。とくに config knob が固定 invariant (owner approval / parent close / durable anchor / hidden subagent 禁止 / callback) を緩められないこと、`enable_*: false` / config 不在が behavior-preserving (従来 spine) であることを test で固定する。

## 参照正本

- `vibes/docs/specs/delegated-coordinator-role-profile.md` (role 語彙 / 責務境界 / 固定 role profile template)
- `vibes/docs/logics/coordinator-sublane-development-flow.md` (孫 dispatch context 保護 / bandwidth / admission の正本)
- `vibes/docs/logics/delegated-coordinator-cockpit-display.md` (window 分離 / `delegation_window_policy` 表示 knob)
- `vibes/docs/logics/unit-presentation-state-db.md` (repo-local `.mozyo-bridge/config.yaml` desired config の schema 契約 / fail-closed pattern)
- `skills/mozyo-bridge-agent/references/workflow.md` (handoff / callback / sublane 完了条件)
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` (`### Owner Close Approval Delegation`)
- `rule-public-private-boundary` / `spec-project-map`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/specs/delegation-policy-project-config.md --repo . --format text`
