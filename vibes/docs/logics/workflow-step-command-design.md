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

## 関連正本

- `vibes/docs/logics/ticketless-project-gateway-runtime-ux.md`
- `vibes/docs/logics/workflow-command-as-is.html`
- `vibes/docs/specs/route-identity-ledger.md`
- `vibes/docs/logics/tmux-send-safety-contract.md`
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md`
