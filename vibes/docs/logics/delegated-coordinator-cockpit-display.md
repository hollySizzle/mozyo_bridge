# 委譲管制塔 / 孫レーンの window・cockpit 表示方針

Redmine #12391 / #12395。Feature #12386 `Delegated Coordinator / Nested Handoff`。

親 project coordinator が子 (委譲) coordinator へ委譲し、子が必要に応じて孫
implementation lane を使う **shallow delegation** において、子 coordinator と孫
worker の window 分離・cockpit 表示・parent reference metadata・retire owner を
定義する設計正本である。

本 doc は **表示 (display projection) の正本** であり、routing / governance の正本
ではない。2 階層 (管制塔 + sublane) の責務・帯域・dispatch・callback・close・
retirement の正本は `vibes/docs/logics/coordinator-sublane-development-flow.md` に
あり、cockpit identity / display layer の正本は
`vibes/docs/logics/pane-centric-cockpit-semantics.md` にある。本 doc は 3 階層へ
拡張したときの **display 差分だけ** を足し、それらの規約本文を複製しない。

## 結論

```text
親子孫の関係 (governance truth)   -> Redmine issue parent link + dispatch journal
lane identity (routing truth)     -> @mozyo_workspace_id / @mozyo_lane_id / @mozyo_agent_role + live preflight
delegation reference (display)    -> parent lane / delegation root への projection breadcrumb
window 分離 (display)             -> 既定 separate、project policy で調整可能
retire owner (governance)         -> 委譲した coordinator (不在時は ancestor へ escalate)
```

3 点を固定する。

1. **window 分離は display であり、routing 承認ではない**。子 coordinator と孫
   worker を別 window にするのは attention を読みやすくするためであり、隣に pane が
   見えること・同じ window に並ぶことのいずれも lane 境界を越えた direct send の
   承認にはならない (`pane-centric-cockpit-semantics.md` の Identity / Routing /
   Display / Governance 分離をそのまま継承する)。
2. **parent / delegated-coordinator reference は display / audit breadcrumb で
   あり、send target ではない**。孫 lane が持つ parent 参照は cockpit で関係を辿る
   ための projection であり、handoff の宛先解決には使わない。cross-lane handoff は
   従来どおり target-lane Codex gateway 経由で、live target preflight が決める。
3. **delegation で safety invariant を弱めない**。owner approval / US close / durable
   callback / retirement safety は Feature #12386 の方針どおり固定 invariant で
   あり、window をまとめても階層を増やしても緩まない。project policy で調整できるのは
   表示 (window 分離 / 並び / label / 深さ表示) に限る。

## 用語と role

`coordinator-sublane-development-flow.md` の actor 語彙を 3 階層へ拡張する。表記
ゆれの解釈 (管制塔 = main coordinator Codex 等) は同 doc の `## 用語と表記ゆれ` を
正本とする。

- **parent coordinator** (= 既存の管制塔 Codex / main coordinator)。delegation tree
  の root。owner-facing、US close、release、owner approval 集約の最終責務を持つ。
- **delegated coordinator** (子 coordinator)。parent coordinator が bounded scope を
  委譲した Codex。委譲 scope 内で dispatch / audit / callback drain / retire を担う
  が、owner approval と US close は parent へ escalate する (shallow delegation)。
- **grandchild lane** (孫 implementation lane)。delegated coordinator が dispatch した
  implementation lane (target-lane Codex gateway + sublane Claude)。bounded
  implementation worker であり、owner approval を回収しない。

### 「delegation」の語の区別

preset の `### Owner Close Approval Delegation` が言う delegation は **owner →
Codex の close approval 事前委任 (standing delegation)** であり、本 doc の
delegation は **parent coordinator → delegated coordinator の作業委譲 (nested
handoff)** である。両者は別概念で、作業委譲が close approval 委任を新設・拡張する
ことはない。孫 lane で close approval を回収しないのは前者の invariant をそのまま
継承するためである。

### depth の上限

Feature #12386 の設計方針どおり、無限階層は目標にしない。audit 可能な shallow
delegation を扱い、既定では parent → delegated → grandchild の **3 階層まで** を
表示・retire 契約の対象とする。これ以上深い委譲が必要になった場合は、増設前に
parent coordinator が durable record に判断を残す (本 doc は 4 階層以上の表示既定を
定義しない)。

## 正本境界 (3 階層)

`pane-centric-cockpit-semantics.md` の層分けを孫 lane へ拡張する。各層の責務は
同 doc を正本とし、ここでは delegation 固有の所属だけを足す。

```text
durable identity   -> registry / workspace anchor / lane id / role
                      (孫 lane も独立した workspace_id / lane_id を持つ)
governance truth   -> Redmine issue parent link + dispatch journal
                      (誰が誰へ委譲したかの正本。pane 配置から推測しない)
runtime target     -> live tmux pane_id + user option + cwd/process preflight
desired display    -> cockpit group membership + delegation reference (本 doc)
live geometry      -> observed tmux layout, drift し得る
display label      -> pane title / border / prefix, projection only
```

親子孫の関係そのものは Redmine が正本である (issue の parent link、dispatch
journal の `parent_us` / `target_lane`)。cockpit metadata はその projection であり、
**消えても関係は Redmine から再導出できなければならない**。pane option を手で消す /
window をまたいで pane を動かしても、委譲関係・routing・retire owner は壊れない。

## window 分離方針

### 既定: separate window

子 coordinator と孫 worker は **既定で別 window (別 cockpit column / tab)** とする。

理由: delegated coordinator は callback drain / audit 待ちで `callback_due` /
`review_waiting` を抱えやすく、孫 worker は `implementing` であることが多い。両者を
同じ表示単位に混ぜると、責務境界と callback 待ち状態が読みづらくなる
(US #12391 背景)。別 window にすると attention state
(`cockpit-attention-state.md`) を coordinator 行と worker 行で独立に projection
できる。

この既定は `unit-presentation-state-db.md` の `projection_preferences`
(`preferred_projection`) と `cockpit_group_membership` の上に載る desired 表示で
あり、live geometry を正本にしない。drift は read-only に検出し、reconcile /
rebalance / move は preview-first (同 doc / `pane-centric-cockpit-semantics.md`)。

### separate は actuator ではない

`delegation_window_policy: separate` は、delegation metadata を持つ lane / pane を
どう表示するかの desired display policy であり、それだけで新しい tmux window /
worktree / lane を作る actuator ではない。子 coordinator が孫 dispatch を判断した
だけ、または同じ lane 内の Claude へ Redmine anchor を渡しただけでは、3-window
display acceptance を満たしたとは扱わない。

3-window / delegated-tree display の full PASS には、少なくとも次のいずれかが
Redmine から replay でき、かつ live projection に反映されている必要がある。

- 孫 `implementation_worker` lane/window/worktree を新規作成し、その dispatch /
  callback anchor を durable record に残す。
- 既存 lane を孫として明示採用し、採用理由・対象 lane・parent delegated coordinator
  との関係を durable record に残す。

どちらの場合も、live pane / discovery projection は `KIND=implementation_worker`
相当、`DEPTH=2`、`PARENT=<delegated coordinator lane>` 相当を示す。`KIND` /
`DEPTH` / `PARENT` が `-` のまま、または stale pane / stale worktree を成功証跡に
混ぜた状態は full PASS ではなく、display gap として扱う。

### project policy で調整可能な部分

window をまとめたい project は、`unit-presentation-state-db.md` の repo-local
`presentation` config (desired declaration) で表示だけを調整できる。調整は
declarative metadata に限り、route / target / approval を持たない (同 doc の
`#### schema field contract` / fallback matrix を正本とする)。

調整可能 (display knob):

- `delegation_window_policy`: `separate` (既定) | `shared`。子 coordinator と孫
  worker を別 window にするか同 group に並べるか。
- 並び順 / `position` / `pinned` / `hidden` / `label_override` (public-safe)。
- delegation 深さの表示有無 (depth badge / indent などの projection)。

これらは既存 `unit_overrides` / `defaults` 語彙の display field であり、新しい
authority key を増やさない。`shared` を選んでも下記の固定 invariant は変わらない。

### 固定 invariant (project policy で調整不可)

window を `shared` にしても、階層を増やしても緩まない:

- **routing / governance 境界**。同じ window に並んでも cross-lane handoff は
  target-lane Codex gateway 経由のまま。隣接表示を direct send の近道にしない。
- **owner approval の単一集約点**。owner-approval-waiting は常に parent coordinator
  Codex へ集約する (`coordinator-sublane-development-flow.md` の owner approval 集約 /
  preset `### Owner Close Approval Delegation`)。delegated coordinator は孫 lane の
  owner approval を自 lane で解決しない。
- **durable callback 要件**。孫 lane の handoff-worthy state は durable gate journal +
  callback outcome journal が揃って初めて完了 (同 doc / skill workflow の callback
  完了条件)。window をまとめても callback を省略できない。
- **retire safety preflight** (後述)。

## delegation reference metadata (孫 lane が持つ parent 参照)

孫 lane は parent / delegated coordinator への reference を持つ (US #12391 受入)。
これは **display / audit breadcrumb** であり、routing identity ではない。

routing identity (`@mozyo_workspace_id` / `@mozyo_lane_id` / `@mozyo_agent_role`) は
従来どおり各 lane が持つ。delegation reference はそれに添える projection で、tmux
user option として表現してよい (`cockpit-attention-state.md` の attention user
option と同じ projection 扱い)。

```text
@mozyo_lane_kind          = coordinator | delegated_coordinator | implementation
@mozyo_delegation_root    = <parent coordinator の workspace_id/lane_id>   # tree root
@mozyo_delegation_parent  = <直接の親 lane の workspace_id/lane_id>          # 1 つ上
@mozyo_delegation_depth   = 0 (root) | 1 (delegated) | 2 (grandchild)
```

制約 (projection であり authority ではない):

- これらは cache / breadcrumb。手で書き換えられても正本ではない。委譲関係の正本は
  Redmine の parent link + dispatch journal。
- **handoff preflight / routing 判定に使わない**。send target は live TargetRecord
  preflight が決める。`@mozyo_delegation_parent` を send 宛先に昇格させない。
- private absolute path / secret-shaped value / 個人名を出さない。値は
  workspace_id / lane_id へ resolve 済みの id 程度に抑える。
- option が消えても identity / handoff / retire owner が壊れない (Redmine から
  再導出する)。

durable 側 (Redmine) では、孫 lane の Start / dispatch journal に
`delegation_parent` (委譲した coordinator の lane / issue) と `delegation_root` を
記録する。pane option はその projection にすぎない。

## cockpit 最小 metadata contract

cockpit が parent-child-grandchild を辿るための最小集合を固定する。見栄えではなく、
**関係の追跡** と **retire owner の特定** に必要な field だけにする (US #12391 受入の
「最小 metadata」)。

```yaml
delegation_display_record:        # projection record。routing key ではない
  unit_id: string                 # 既存 host_id+workspace_id+lane_id 由来 (cockpit-attention-state と共通)
  lane_kind: coordinator | delegated_coordinator | implementation
  delegation_root: string         # tree root の unit pointer
  delegation_parent: string | null  # 直接の親 unit pointer (root は null)
  delegation_depth: integer       # 0 | 1 | 2
  retire_owner: string            # この lane を退役する責務を持つ unit (後述)
  source_refs:                    # 人間が辿れる anchor のみ
    - redmine:#<issue_id>#journal-<dispatch journal>
    - tmux:<pane_id>@<observed_at>
```

- `delegation_display_record` は `cockpit-attention-state.md` の `AttentionRecord` と
  同じ **derived projection record** であり、どこか 1 つの mutable field を正本に
  しない。必要なら `unit-presentation-state-db.md` の current table に latest derived
  value cache を置いてよいが、Redmine + registry + runtime から再計算可能に保つ。
- これは新しい current table を必須にしない。最小実装は既存 `cockpit_group_membership`
  / `projection_preferences` に depth / kind / parent pointer の表示列を足すか、derive
  時に join するかを実装 task で選ぶ。
- routing / approval / close / handoff target authority を持つ列を増やさない
  (`unit-presentation-state-db.md` の不変条件)。

cockpit 出力 (`agents targets` / future `cockpit status`) は delegation 列を出して
よいが、derived record として出す。例:

```text
WORKSPACE  LANE   ROLE   KIND                   DEPTH  PARENT        ATTENTION
mozyo      12391  codex  coordinator            0      -            healthy
mozyo      12395a codex  delegated_coordinator  1      mozyo/12391  review_waiting
mozyo      12395b claude implementation         2      mozyo/12395a implementing
```

DEPTH / PARENT / KIND は routing key ではない。handoff は Unit → Target preflight を
使う。

## context-free smoke と UX 判定

親子孫表示の UX を検証するときは、既に親子孫構造を agent の chat/pane context に
注入した状態だけで判断しない。Redmine durable anchor だけを受け取った coordinator が
project config / Redmine issue / cataloged docs から delegation tree を発生または
採用できるかを、別 smoke として確認する。

この smoke は #12453 型の「明示 context 付き route replay」と別物である。#12453 型は
既知の parent / child / grandchild 候補を与えて transport と callback を確認する。
context-free smoke は、起点側へ具体 pane id、親子孫候補、window 配置を事前に教えず、
次を検査する。

- durable anchor から parent coordinator が child delegated coordinator を解決できるか。
- child delegated coordinator が必要なら grandchild gateway / worker を発生または採用
  できるか。
- 自律発生しない場合、どの context が不足していたかを Redmine に記録できるか。
- cockpit projection が、発生した tree または発生しなかった理由を operator に示せるか。

成功条件は「必ず親子孫が自然発生すること」ではない。自律発生しない場合も、必要な
durable field / project config / display metadata の不足が replayable に残れば、
UX 設計 input として有効である。逆に、事前に pane id や候補 lane を chat で教えてから
PASS しただけでは context-free smoke の証跡にならない。

route / display / product output は分けて判定する。親→子、子→孫の Redmine journal が
replay できても、同じ lane の worker 採用に留まり delegated-tree metadata が出ない
場合は `PASS-route / PARTIAL-display` であり、3-window UX の full PASS ではない。
薄い ticket が clarification / blocked で終端することは product output 上の valid
blocker になり得るが、route-bound window / metadata の表示証跡を代替しない。

## retire owner と stale-lane safeguard

US #12391 受入の「retire owner / record requirement」を固定する。目的は階層が増えても
stale lane / retirement 漏れを増やさないことである。retirement 契約・fixed fields・
safety preflight の正本は `coordinator-sublane-development-flow.md` の
`$retirement_contract()` と `## サブレーン退役` であり、本 doc は delegation 固有の
**owner と ordering** だけを足す。

### retire owner

- **孫 lane の retire owner は、それを dispatch した delegated coordinator** である。
  parent coordinator ではない。delegated coordinator が孫の close / integration を
  audit し、retire safety preflight を満たしたら退役する。
- **delegated coordinator が先に退役・不在になった場合、retire 責務は ancestor
  (parent coordinator) へ escalate** する。これにより孫 lane が orphan 化して
  cockpit / worktree に残り続ける stale-lane 漏れを防ぐ。escalate は durable record に
  残す (retire owner の付け替えを推測で行わない)。

### ancestor-before-descendant ordering

- **un-retired な孫 lane を持つ delegated coordinator は退役しない**。これは
  `$retirement_contract()` の「dependency ancestor lane は downstream が消費される
  まで保持できる」を delegation tree へ適用したものである。子を先に retire (または
  retire owner を ancestor へ明示的に escalate) してから親 coordinator を retire する。
- 親を先に畳む必要がある場合は、各孫の retire owner を ancestor へ付け替える journal を
  先に記録する。silent な親退役 (孫を宙吊りにする) は invalid。

### record requirement (退役 fixed-field 拡張)

`$retirement_contract()` の fixed fields に delegation の breadcrumb を足す。退役は
destructive 寄り操作なので、`retire_ready` / `retired` journal の前後で bracket する
規約は同 doc を正本とする。本 doc が足す field:

```text
delegation_parent   : 委譲した coordinator の lane / issue (孫 lane の retire owner)
delegation_children : この coordinator が dispatch した未退役孫 lane の list (空でなければ retire blocker)
retire_owner_actor  : 実際に退役を実行した unit (delegated coordinator | escalated ancestor)
escalation_reason   : ancestor へ escalate した場合の理由 (delegated coordinator 不在 等)
```

- `delegation_children` が空でない delegated coordinator の退役は `retire_blocked`
  とする (上記 ordering)。
- 孫 lane の `retired` journal は `delegation_parent` を含め、どの委譲下で消えたかを
  durable に残す。これにより nested retirement が後から監査でき、retirement 漏れが
  `coordinator-sublane-development-flow.md` の stall / retirement sweep で検出可能な
  まま保たれる。
- retirement は close を自己承認しない (同 doc の invariant をそのまま継承)。

## 固定 invariant と project knob の対照

| 項目 | 区分 | 正本 |
| --- | --- | --- |
| 子 coordinator / 孫 worker の window 分離 | project knob (`separate`/`shared`) | 本 doc + `unit-presentation-state-db.md` presentation config |
| 並び順 / pinned / hidden / label / depth 表示 | project knob | `unit-presentation-state-db.md` |
| cross-lane handoff = target-lane Codex gateway | 固定 invariant | `coordinator-sublane-development-flow.md` / skill workflow |
| owner approval の単一集約 (parent coordinator) | 固定 invariant | 同 doc / preset `### Owner Close Approval Delegation` |
| durable callback (gate + callback outcome) | 固定 invariant | skill workflow / 同 doc |
| 孫 retire owner = delegated coordinator (不在時 escalate) | 固定 invariant | 本 doc |
| ancestor-before-descendant retire ordering | 固定 invariant | 本 doc + `$retirement_contract()` |
| retire safety preflight | 固定 invariant | `coordinator-sublane-development-flow.md` |

## Public / Private boundary

OSS default に入れてよいもの:

- generic な lane_kind enum / delegation depth 概念。
- delegation reference の user option 名と projection record schema。
- window 分離の既定 (separate) と display knob 名。
- retire owner / ordering の portable rule と退役 fixed-field。

入れてはいけないもの:

- private project の lane naming / cockpit composition policy。
- operator 固有の window 並び / color / shortcut / iTerm profile。
- private absolute path / private host topology / 個人名 / Redmine project 名。
- 4 階層以上の private 運用 profile (本 doc は 3 階層既定のみ)。

## 実装分割

本 task は spec doc で完了する (`cockpit-attention-state.md` /
`pane-centric-cockpit-semantics.md` / `unit-presentation-state-db.md` と同じ
design-doc 完了モデル)。実装は別 issue / 別 lane に分ける。とくに sibling US の
delegated coordinator role profile (#12387) と grandchild dispatch context policy
(#12389) を block しないよう、本 doc は display / metadata 要件のみを固定し、role
profile field と context-preservation policy には踏み込まない。

候補 follow-up:

1. delegation reference の tmux user option writer (projection)。
2. `agents targets` / future `cockpit status` に KIND / DEPTH / PARENT 列を derived
   projection として出す。
3. `presentation` config の `delegation_window_policy` knob 解決
   (`presentation_grouping.py` の display-only resolver に追加)。
4. retirement fixed-field (`delegation_parent` / `delegation_children` /
   `retire_owner_actor`) の record / sweep への結線。
5. delegation tree を Redmine parent link + dispatch journal から再導出する read
   model (pane option を正本にしない)。
6. context-free smoke: 起点側へ親子孫候補を chat/pane で事前注入せず、Redmine durable
   anchor だけから delegation tree が発生または fail-closed することを確認する。

各 task は runtime / tests を伴うため Claude implementer lane に回し、Codex が review
する。とくに delegation reference / window knob が routing / handoff safety gate を
触らないこと、retire owner escalation が close を自己承認しないことを test で固定する。

## 参照正本

- `vibes/docs/logics/coordinator-sublane-development-flow.md` (2 階層 flow / 帯域 /
  retirement / owner approval 集約の正本)
- `vibes/docs/logics/pane-centric-cockpit-semantics.md` (cockpit identity / display
  layer 分離の正本)
- `vibes/docs/logics/cockpit-attention-state.md` (attention 派生 projection)
- `vibes/docs/logics/unit-presentation-state-db.md` (desired presentation state /
  config knob 境界)
- `vibes/docs/logics/worktree-lifecycle-boundary.md` (worktree retire の責務境界)
- `skills/mozyo-bridge-agent/references/workflow.md` (handoff / callback / sublane
  完了条件)
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md`
  (`### Owner Close Approval Delegation`)
- `rule-public-private-boundary` / `spec-project-map`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/delegated-coordinator-cockpit-display.md --repo . --format text`
