# Workflow step command design

Redmine #12755 Add workflow step as the single standard agent command の設計方針を固定する。
本書は `mozyo-bridge workflow step` の思想、責務境界、既存 primitive との関係を定義する。
具体的な CLI flag、JSON field、実装 module 名、error wording は実装側 help / tests を正本にする。

## 背景

GK3500 の `grandparent -> parent -> child -> grandchild` smoke で見えた問題は、
個々の delivery primitive が不足していることだけではない。現状は `project-gateway consult`、
`project-gateway child-intake`、`handoff send`、`handoff ticketless-callback`、
`handoff q-enter`、`delegate-*`、`%pane` debug delivery が同じ運用面に見えており、
AI / operator が route、pane、rail、role transition を選ぶ形になっている。

通常 workflow では、AI は同じ command を叩くだけでよい。次に何をすべきかは
mozyo-bridge が current lane / durable gate / route identity から解く。

## 設計原則

1. **通常入口は 1 つにする。**
   AI / operator の標準入口は `mozyo-bridge workflow step` とする。`project-gateway`
   や `handoff` は primitive / compatibility / debug surface であり、通常 smoke evidence
   の手順として選ばせない。

2. **一歩だけ進める。**
   `workflow step` は workflow state machine を 1 step 進める。複数 hop を黙って進めず、
   step の結果として次 state / next owner / blocker を返す。

3. **交通整理は自動化し、設計判断は自動化しない。**
   route 解決、delivery rail 選択、callback rail 選択、anchor requirement の fail-closed 判定は
   command が持つ。domain/design answer、Redmine issue 作成・選択判断、owner approval、
   review approval、release / destructive / credential 操作は command が代行しない。

4. **semantic identity を route authority にする。**
   `%pane` は cache、self-fence、debug evidence に限る。route authority は workspace、
   project scope、lane role、durable record、route identity から解く。

5. **fail closed を成功経路と同じくらい重要に扱う。**
   ambiguity、missing route、same-lane loop、anchor missing、unsafe provider binding、
   prohibited role transition は別 command 探索へ落とさず、structured blocked result と
   next owner を返す。

6. **primitive は残すが、通常 UX から退ける。**
   既存の `project-gateway consult` / `child-intake` / `handoff send` /
   `ticketless-callback` / `q-enter` は workflow step の実装部品として残す。
   compatibility と debug の価値はあるが、AI が通常選ぶ command ではない。

## 標準 surface

最小 surface は次の形にする。

```text
mozyo-bridge workflow step
mozyo-bridge workflow step --dry-run
mozyo-bridge workflow step --json
```

`--dry-run` は side effect を発生させず、解決された state / next_action / reason を返す。
`--json` は UI / automation が読む structured result を出す。

`--to-role`、`--intent`、`--target %pane`、`--mode queue-enter` のような選択は標準入口に置かない。
必要なら debug / primitive command 側に残す。

## State machine の入力

`workflow step` は少なくとも次を読む。

- current cwd / workspace identity
- `mozyo init` 済み lane metadata
- current lane role / provider binding
- project scope
- Redmine issue / latest gate journal / pending gate state
- route identity registry / live agent inventory
- pending delivery / callback state
- command compatibility policy

pane scrollback、chat message、active pane だけを source of truth にしない。
Redmine anchor が存在する場合、durable gate を読まずに実行してはならない。

## State machine の出力

`workflow step` は human text に加えて、JSON では少なくとも次を返せる設計にする。

```yaml
state: <current_workflow_state>
next_action: <resolved_action_or_blocked>
execution: executed | ready | dry_run | blocked | no_op
reason: <fixed_reason_token>
next_owner: workflow | caller | parent | child | grandchild | operator | owner
primitive: <internal_primitive_or_none>
durable_anchor: <redmine_or_ticketless_pointer_or_none>
```

実 field 名は実装時に固定してよい。本書が固定するのは「結果が replayable であり、
次 owner が明示される」ことである。

## 許可される自動実行

`workflow step` が自動実行してよいのは、workflow 交通整理に限定する。

- semantic route-plan resolution
- grandparent -> parent の ticketless consultation forward
- parent project_gateway -> child delegated_coordinator の ticketless work-intake forward
- callback state が既に決まっている場合の structured ticketless callback
- Redmine issue / journal anchor が既に存在し、role transition が許可されている場合の anchored handoff

## 禁止される自動実行

次は `workflow step` が自動判断してはならない。

- domain/design answer の作成
- Redmine issue 作成・既存 issue 選択の判断
- Redmine anchor なし worker dispatch
- owner close approval / review approval
- release / publish / credential / destructive operation
- rclone / Google Drive / Finder / mount / external service operation
- project Claude direct send across lane / workspace boundary
- hidden subagent dispatch
- raw pane typing / debug `%pane` delivery を product evidence にすること

## 祖父・親・子・孫への適用

| lane | as-is の典型操作 | `workflow step` の責務 |
| --- | --- | --- |
| grandparent | `project-gateway route-plan` + `project-gateway consult` | routing metadata だけで parent gateway を解決し、ticketless consultation を送る。分類不能なら blocked。 |
| parent | `project-gateway route-plan --from-role project_gateway` + `project-gateway child-intake` | domain/design を答えず、child coordinator へ ticketless work-intake を送る。same-lane / missing / ambiguous は blocked。 |
| child | Redmine anchor 判断 + `handoff send` | anchor が必要な state を検出し、anchor 未決定なら child decision required として止まる。anchor ready なら worker dispatch を行う。 |
| grandchild | Redmine-governed work + reply/callback | Redmine anchor を読んで実装 state を進める。anchor なしなら実行しない。 |
| callback side | `ticketless-callback` / `q-enter consultation_callback` | pending callback state を検出し、caller lane へ structured result を返す。 |

## as-is / to-be 説明

人間向けの現状説明と対比表は `vibes/docs/logics/workflow-command-as-is.html` に置く。
本書は設計方針の正本であり、HTML は説明資料として扱う。

## 実装順序

1. `workflow step --dry-run` を作り、state 解決と structured result を固定する。
2. grandparent -> parent ticketless consultation を内部 primitive へ委譲する。
3. parent -> child ticketless work-intake を内部 primitive へ委譲する。
4. callback state を内部 primitive へ委譲する。
5. anchored worker dispatch は Redmine anchor ready state に限定して委譲する。
6. help / docs で `workflow step` を標準入口、既存 primitive を内部 / compatibility / debug として分類する。

## GK3500 を `workflow step` で駆動するシーケンス

通常 smoke では、各 visible lane で AI が同じ `mozyo-bridge workflow step` を叩くだけでよい。
command family、rail、pane、role transition を AI が選ばない。各 lane の current
identity (`@mozyo_lane_kind` stamp / project scope / provider binding) から lane role を
解き、one-step-down transition を内部 primitive に委譲するか、fail-closed で次 owner を返す。

| step | lane (current binding) | `workflow step` の解決 | `execution` / `next_owner` / 内部 primitive |
| --- | --- | --- | --- |
| 1 | grandparent (`department_root_coordinator`) | inventory 内の唯一の cockpit-visible project gateway を解決し、ticketless consultation を forward する。gateway 0 件 / 複数件 / detached は fail-closed。 | `ready` (consultation_ready) / `parent` / `project-gateway consult` |
| 2 | parent (`project_gateway`) | same-lane self-fence 付きで child coordinator を解決し、ticketless work-intake を forward する。domain/design は答えない。same-lane / missing / ambiguous は fail-closed。 | `ready` (work_intake_ready) / `child` / `project-gateway child-intake` |
| 3 | child (`delegated_coordinator`) | worker dispatch の Redmine anchor 要件を検出する。anchor 未決定なら `anchor_required` で停止 (child decision required)。 | `blocked` (anchor_required) / `child` / なし |
| 4 | grandchild (`implementation_worker`) | anchor を読んで実装 state を進める。anchor が無ければ `worker_runs_without_anchor` で停止し、child へ blocked callback を返す。 | `no_op` (redmine_work_ready) / `grandchild` / なし |
| callback | project_gateway / delegated_coordinator | 既に決まった consultation/work-intake 結果を caller lane へ no-anchor callback rail で返す。 | `ready` (callback_ready) / `caller` / `handoff ticketless-callback` |

step 3 の anchor 決定 (Redmine issue 作成 / 選択) は `workflow step` が代行しない。child が
anchor を決めた後、その already-determined anchor を渡す escape (`--issue` / `--journal`) で
anchored worker dispatch が表現できるが、標準 arg-free surface では `anchor_required` に
fail-closed する。これは `## 禁止される自動実行` の Redmine issue 作成・選択判断、および
anchor なし worker dispatch 禁止と整合する。

`--dry-run` は各 step の解決結果 (`state` / `next_action` / `execution` / `reason` /
`next_owner` / `primitive` / `durable_anchor`) を mutate せずに返す。`--json` は同じ envelope を
1 個の JSON object として返す。実 flag / state / reason token は CLI help / tests を正本にする。

primitive (`project-gateway consult` / `child-intake` / `handoff send` /
`ticketless-callback` / `q-enter` / `delegate-*`) は internal / compatibility / debug surface
として残るが、通常 smoke では選ばせない。`%pane` / `q-enter` / `queue-enter` / `--mode` は
標準 surface に出さない。

## Runtime store との next-action reconcile (Redmine #13291)

`workflow step` (live tmux routing) と `workflow resume` (persisted runtime store の
lifecycle next-action) は独立 2 engine だった。#13291 で step は resume と同じ
runtime store (`workflow-runtime.sqlite`) を **decision 入力として read** し、live 出力に
store の pending action を reconcile する。方向は「step が runtime store を読む」であり、
resume は報告面のまま (step は store を mutate しない)。合成は pure・fail-toward-safe で
実装は `f_140.../domain/workflow_step_reconcile.py` に置く。

合成規則は fixed vocabulary (`reconcile_disposition`) で決定的にする。序列は次の通り:

1. **degrade (非破壊)**: store 不在 (`store_absent`) / 読取・schema・fold 失敗
   (`store_unavailable`) は live 出力をそのまま返す (従来の step 出力を byte-identical に
   維持 = 後方互換)。reconcile field は出さない。
2. **no pending**: store の overall action が positive-occupancy な no-op
   (`none` / `hold` / `await_implementation`) は反映対象なし (`store_no_pending_action`)。
   live 変更なし。
3. **aligned (surface のみ)**: pending だが gating でない action (低リスク callback delivery
   / perform_review など、store 自身が `requires_confirmation=false` かつ
   `blocked_reason` 空) は live 出力を変えず、store action を報告に添える
   (`store_aligned`)。live leg が既に forward でない (blocked / no-op) 場合は gating action
   でも surface のみ。
4. **gates (fail-toward-safe)**: pending かつ gating な action — store 自身の vocabulary で
   `requires_confirmation` (integrate / close / retire / dispatch-next / redeliver /
   resolve-blocker / owner-or-release gate) または `blocked_reason` (unknown action /
   unresolved route) — が live の forward (`ready`) leg と矛盾する場合、live leg を
   fail-closed `blocked` に downgrade する (`store_gates_live`, reason
   `store_pending_action_gates`)。step は未処理の workflow gate を黙って越えて forward
   しない。安全側 (より保守的な engine) が勝ち、agent は先に `workflow resume` で
   pending action を処理するよう案内される。

gating 判定は step 独自の risk model を新設せず store engine の既存 vocabulary
(`requires_confirmation` / `blocked_reason`) を再利用する。execution leg の自動実行化
(actuation) は非 goal であり、reconcile は 2 decision の **合成** と、最大でも forward leg の
拒否のみを行う。reason token / field 名の正本は実装 (`workflow_step_reconcile.py` +
tests) とする。

## herdr backend での workflow step 再収束 (Redmine #13489)

tmux path の `workflow step` は current lane を tmux `%pane` (`current_pane()`) と tmux
discovery inventory の照合で解決する。純 herdr session には `TMUX_PANE` が無く pane も tmux
inventory に無いため、この解決は `self_lane_unresolved` に fold する。#13446 は暫定的に
fail-closed dead-end (`herdr_self_lane_unresolved`、`sublane create/start --execute` を案内)
を置いたが、これは単一標準入口の完成ではなかった (Bug #13494)。

#13489 はこの dead-end を **herdr-native 解決** に置き換える。lane role を tmux `%pane` では
なく herdr-native identity から解決する:

- **identity source** = launch-time sender env (`MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` /
  `MOZYO_LANE_ID`, spec `herdr-native-identity.md` §2) を `resolve_sender_identity` で
  fail-closed に読む + workspace registry の project scope。tmux `%pane` は使わない。
- **lane role 分類は divergent model を作らない (設計原則 4)。** tmux state machine と同じ
  role 語彙へ、documented shared-project-workspace model (spec §1 / `sublane list` fold
  `sublane_herdr_projection`) から導出する。ただし **provider/placement は workflow authority
  ではない** (mid-review j#74748 F1 / j#74749 F1 / j#74750): mzb1 `role` は runtime provider
  token であり、default-lane pair は step 時点で gateway/grandparent を区別する durable role
  authority を持たない (default-lane Claude は coordinator の assistant であって worker では
  ない)。よって **非 default lane slot のみ** を lane-local class として分類し、default lane は
  fail-closed する:
  - **非 default** lane + provider `claude` → `implementation_worker` (孫 worker);
  - **非 default** lane + provider `codex` → `delegated_coordinator` (sublane gateway / 子);
  - **default** lane (provider 不問) → fail-closed (`ambiguous_default_coordinator_role`);
  - unknown provider → fail-closed (`herdr_lane_role_unresolved`)。
  - registry `project_name` を role/scope authority にしない (display metadata、dir 名 default)。
- **anchor gate (j#74748 F3 / j#74784 / j#74787)。** worker/gateway は **source-of-truth Redmine**
  を authority として anchor を検証した場合のみ ready/no_op を返す。lane metadata も workflow
  runtime store も **caller-supplied advisory projection** (任意 caller が書ける) であり authority
  ではない: lane の **単一 non-retired lane-metadata record** が *candidate* issue を名指す
  (cardinality 保持 — duplicate active / active+retired stale / missing / retired は fail-closed、
  set 化しない)。その candidate issue の journal を既存 credential-gated `LiveRedmineJournalSource`
  (daemon-trusted credential / redirect 拒否 / injected transport / redacted error) で read-only 取得し、
  `markers_from_source` の **structured gate marker** (gate-bearing kind のみ、machine `[mozyo:…]`
  token、prose 不可) から issue 一致の exact journal を得て `durable_anchor=redmine:issue=<id>:journal=<id>`
  を verify する。credential 未設定 / transport 失敗 / issue-journal 不在 / gate marker 無し / issue
  不一致は **fail-closed** (`herdr_anchor_unverified`、credential/URL は出力しない)。未検証 anchor で
  ready を返さない。新 credential/network 実装は既存 port を再利用し重複させない。
- **store↔Redmine issue-correlation (j#74810 F3c)。** caller-supplied advisory store が同 lane に
  異なる `(issue, journal, gate)` を主張する場合は fail-closed (`herdr_anchor_store_mismatch`、store の
  canonical `redmine:<issue>:<journal>` event を advisory cross-check として抽出)。さらに共通
  `reconcile_step_with_store` は herdr の live-verified anchor issue (`live_anchor_issue`) と store の
  overall pending action の `target_issue`/anchor を相関し、不一致 action は `store_aligned`/gate せず
  fixed disposition `store_issue_mismatch` で不採用 (audit 反映、live outcome 不変)。tmux は
  `live_anchor_issue=None` で byte 不変。
- **same-lane worker liveness は cardinality (j#74749 F2 / j#74750)。** gateway は同一
  `(workspace, lane, claude)` の 0 / 1 / 2+ を保持し、2+ = ambiguous / locator 欠落 = fail-closed
  とし、重複 identity を silent target にしない。
- 出力は tmux と同じ replayable `WorkflowStepOutcome` envelope。`workflow step` は backend に
  依らず同じ contract を返し、**herdr live outcome も tmux と同一の store reconcile / dry-run /
  one-step execution pipeline を通る** (j#74748 F2、backend 差は live lane outcome 解決のみ)。

### Increment 境界 (j#74685 design_boundary / task-level mid-review)

本 US は workflow / routing / compatibility / destructive boundary に触れるため、実装途中の
task-level design mid-review が必須 (Start Gate j#74685)。増分は次で切る:

- **Increment 1 (resolution-only)**: 非 default lane の lane identity + role + anchor を解決し、
  role ごとの next_action / next_owner / herdr surface を返す。worker は verified anchor を読んで
  実装 (`no_op`); gateway は verified anchor + 単一 live same-lane worker を確認し
  `sublane dispatch-worker` へ (`no_op`)、anchor 未検証 / worker missing・ambiguous・locator 欠落は
  fail-closed; **default lane (coordinator pair) は durable role authority 不在で fail-closed
  (`ambiguous_default_coordinator_role`)**。**sublane lifecycle mutation も delivery も行わない**
  (`primitive=none`)。anchor read は worker/gateway、inventory read は gateway が anchor gate を
  通過した場合のみ (default/unknown は store/inventory を読まず block)。
- **Increment 2 (coordinator disposition j#74855 で bounded authorize)**: policy が既に許可した
  sublane dispatch の **一段** 自動実行を実装。gateway lane が **verified source-of-truth anchor
  + 単一 live same-lane worker (stable target) + identity-known** を満たす場合、`workflow step`
  は executable outcome (`primitive=herdr_sublane_dispatch_worker`, `execution=ready`) を返し、
  CLI が既存 `sublane dispatch-worker --execute` を **共通 pipeline (dry-run / store reconcile /
  fail-closed) 経由で一段** 実行する (ACK は completion でない)。worker lane は自 anchor 読取
  no_op のまま、coordinator (default) lane は fail-closed のまま。gating store action (issue-
  correlated) は dispatch を blocked へ downgrade する。**destructive drain/retire の pane/
  worktree/local-branch mutation、任意 target/lane/route、raw shell 直結、domain/design・issue
  起票・Review Gate・owner approval・release・credential の自動承認は引き続き禁止** (j#74855)。
  actual drain/retire actuator は専用 actuator + coordinator-only + exact owner approval +
  closed/integrated/clean/origin-reachable/callback-drained/identity-known + task-level 再 review
  を先に要する (j#74748)。

実 module / reason token / next_action 文言は実装 (`domain/workflow_step_herdr.py` +
`application/herdr_workflow_step.py` + tests) を正本にする。tmux path は byte 不変
(`_herdr_step_preflight` は backend=tmux で `None` を返し、tmux rail に一切触れない)。

## 関連正本

- `vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`
- `vibes/docs/logics/workflow-command-as-is.html`
- `vibes/docs/specs/route-identity-ledger.md`
- `vibes/docs/logics/tmux-send-safety-contract.md`
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md`
