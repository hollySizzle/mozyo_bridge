# CLI / MCP Shared Application API

Redmine #15149（親 Feature #15148 180_LLM向けMCP操作入口）。CLI 固有の parse / print と、
workflow・identity・authority・send safety の判断を分離し、CLI と local MCP が同じ
application 処理を呼べる境界を確定する設計正本。

本書は boundary の正本であり、MCP server 本体（#15151）、tool schema、managed LLM の
入口切替（#15150）を含まない。

## Decision

判断は **1 箇所** に置き、entry を 2 つにする。

- **shared application processing**: `commands.run_handoff_orchestration`。
  typed input（`HandoffCommandInput`）を取り、CLI 文字列・TTY・argv を読まない。
- **CLI entry (adapter)**: `commands.orchestrate_handoff(args)`。
  `argparse.Namespace` をここで終端し、typed input に変換して上を呼ぶ。
- **application API entry**: `handoff_application_service.run_handoff(request)`。
  typed request を取り typed result を返す。local MCP server はこれを **同一 process
  内で** 呼ぶ。

MCP が CLI を subprocess 実行する wrapper 設計は採らない（#15148 Boundary）。
CLI は test / CI / bootstrap / 復旧 / operator debug 用途として維持する（#15154）。

## Scope Of The First Cut

最初に分離した高レベル操作は **handoff family の 4 操作** である。

| operation | CLI | 内容 |
| --- | --- | --- |
| `send` | `handoff send` | anchored な cross-agent send。semantic target selection を先に適用する |
| `reply` | `handoff reply` | anchored reply（`--kind` 既定 `reply`） |
| `ticketless_callback` | `handoff ticketless-callback` | anchor 無し callback rail（#12703） |
| `cross_workspace_consult` | `handoff cross-workspace-consult` | 受信者を Codex gateway に固定する design consult（#11779） |

この 4 つを最初に選ぶ理由は、#15148 が挙げる 4 判断（workflow / identity / authority /
send safety）がすべてここに集中しており、typed input（#13729）と typed outcome
（`DeliveryOutcome`）が既に存在するため、判断本体に触れずに entry だけを分離できる
ためである。

**この cut に含まれないもの**（後続 issue）:

- `project-gateway consult` / `child-intake`（`ticketless_consultation` /
  `ticketless_work_intake` rail）。これらは別 operation であり、本 API の operation
  vocabulary に入れていない。
- `sublane` / `cockpit` / `status` / `agents` 等、他 family の高レベル操作。
- MCP server 実装そのもの（#15151）と authority 検証の接続（#15152）。

## Layers

```text
MCP tool (#15151)                 CLI parser / cmd_handoff_*
        |                                   |
        v                                   v
run_handoff(HandoffRequest)      orchestrate_handoff(args)   <- Namespace はここで終端
        |                                   |
        +---------------+-------------------+
                        v
        orchestrate_handoff_input(HandoffCommandInput, ...)   <- transport binding を install
                        v
        run_handoff_orchestration(...)                        <- 全 gate はここ
```

`orchestrate_handoff_input` より下に CLI 由来の型は無い。

### Core-owned entry policy

`f_130_handoff_routing/domain/handoff_operation.py` が operation 語彙
（`HANDOFF_OPERATIONS`）と entry policy（`HandoffEntryPolicy`）を持つ。CLI entry
（`application/handoff_command.py`）と application API の双方がこの表を読む。

entry policy は「どの既定 kind か」「受信者を固定するか」「relaxed な
receiver-binding gate を全 mode で強制するか」「anchorless rail か」「semantic
selection を走らせるか」だけを表す。**authority は与えない**。

`entry_policy_for` は未知 operation を fail closed（`UnknownHandoffOperation`）で
拒否する。任意 command 文字列を operation として通す経路は無い。

### Typed input / output

型は **static に検査可能** にする（`object-oriented-architecture-policy.md` の
static typing policy、review j#102080 finding_f2）。公開境界の field を `Any` に
しない。

- 入力: `HandoffCommandInput`（#13729 の frozen value object）。
- operation: `HandoffOperation`（`Literal`）。`Literal` は定数から生成できないため
  語彙を再記述するが、`get_args(HandoffOperation) == HANDOFF_OPERATIONS` を test で
  pin して drift を防ぐ。runtime gate は `entry_policy_for` の fail closed。
- 出力: `HandoffResult`
  - `status`: `completed` / `fail_closed`
  - `exit_code`
  - `outcome`: 最後に publish された `Optional[DeliveryOutcome]`
  - `emissions`: `HandoffEmission` の tuple。emit context は緩い mapping ではなく
    **名前付きの型付き field**（`record_format` / `recovery_command` /
    `duplicate_lane_panes` / `retry` / `activation` / ...）で持つ。field 集合は
    delivery record sink の signature と test で突き合わせる。未知 keyword は
    `extra` に退避し、完了済み send を壊さずに欠落も起こさない。
  - `delivered`: **共有 injection-stage authority** の判定
    （`injection_stage_for_outcome(outcome) == STAGE_SUBMITTED_CONFIRMED`）。
    `exit_code == 0` は配達の証明ではない（#13583 / #14232）。
  - `error_message`: fail-closed 時の gate message

### Fail-closed carrier

`shared/errors.die` は `CommandAbort(SystemExit)` を送出する。`SystemExit` の subclass
であるため CLI の exit contract（型・`.code`・interpreter 挙動）は変わらず、message は
attribute として型付きで取れる。application API は stdout / stderr を parse しない。

### Transport binding

terminal transport backend の選択（#13253 / #13255 / #13261 / #13320）は
`application/handoff_transport_wiring.py` が持つ。#15149 で読み取り対象を
`HandoffTransportContext`（`repo_root` / `to` / `target` / `target_repo` /
`target_lane` / resolved project-gateway capability）という typed record にし、
Namespace decorator を context manager `runtime_transport_binding` へ移した。
選択 logic は 1 つであり、CLI send と API send が別 backend に解決することはない。

#### Process-global slot の非重複（fail closed）

binding は `commands` module の global（`run_tmux` / `capture_pane` /
`active_herdr_turn_start_rail`）を差し替えて install する。したがって **同一 process
内で 2 つの handoff run が同時に slot を所有することはできない**。A enter → B enter
→ A exit → B exit の interleaving は、B の実行中に A 以前の値を掴ませ、かつ両 scope
終了後に A の shim を残す（review j#102080 finding_f1 の再現）。

CLI では 1 process 1 command のため到達不能だったが、in-process application API
（長寿命の local MCP server）では到達する。よって `runtime_transport_binding` は
scope 全体を process-wide に guard し、重複 scope を `die` で **fail closed に拒否**
する（zero send。CLI には構造化拒否、API には typed `fail_closed` result）。tmux
default の run も guard 対象とする — install しないだけで、他 run の herdr shim を
使って送ってしまうため。blocking せず即拒否するのは、同一 thread の再入を deadlock
ではなく即時のエラーとして表面化させるためである。

transport を request-scoped injection にする（module-global 差し替え自体をやめる）の
が本来の解であり、深い rail 群が injected transport port を取る変更を要する。それは
別 change であり、それまでは本不変条件を **仮定せず検査する**。

## Invariants

1. **判断を二重実装しない。** entry policy は core 表 1 つ。gate は orchestration 1 つ。
   API 側に gate の再実装・緩和・追加をしない。
2. **API は CLI が通す gate を skip できない。** argparse の `choices` は CLI の
   presentation であり authority ではない。受信者語彙 / source / kind / mode /
   send semantics / anchor / identity / gateway route はすべて orchestration 内の
   gate が再検証する。
3. **API は CLI が通さない gate を追加しない。** API 固有の許可経路を作らない。
4. **request は operation が持たない rail を要求できない。** `apply_entry_policy` が
   entry policy field をすべて policy から再設定するため、`send` を要求しながら
   anchorless ticketless rail を立てる、といった smuggling は成立しない。
5. **stdout / stderr / TTY / argv / subprocess に依存しない。** record の print は CLI
   adapter の責務であり、API は同じ情報を typed data で返す。
6. **pane message は正本ではない。** durable anchor（Redmine issue / journal）が正本で
   ある点は変わらない。API は notification を送るだけで workflow 判断を持たない。
7. **同一 process で handoff run を重ねない。** transport slot が process-global で
   ある限り、重複は fail closed で拒否する（上記「Process-global slot の非重複」）。
8. **公開境界の field を `Any` にしない。** result / emission / request / port の型は
   具体型で表し、restatement（`Literal` 語彙、emission の context field 集合）は
   source と test で突き合わせる。

## Non-goals

- MCP server / tool schema の公開（#15151）。
- CLI の削除（#15154 が維持境界を決める）。
- raw tmux 操作、任意 command 文字列、pane mutation を操作として公開すること。
- external plugin API（`plugin-ready-adapter-boundary.md` の非目標を引き継ぐ）。
- 本 API の record shape に対する public ABI / 互換保証。internal であり変更しうる。

## Verification

- `mozyo-bridge tests run` の focused / full。
- `tests/unit/.../f_130_handoff_routing/test_handoff_operation_policy.py`:
  operation 語彙・entry policy が CLI entry と一致すること、smuggling の fail-closed。
- `tests/unit/.../f_130_handoff_routing/test_handoff_application_service.py`:
  typed result、injection-stage による delivery 判定、fail-closed carrier、
  argparse / subprocess / TTY 非依存、公開境界に `Any` が無いこと、`Literal` 語彙と
  emission context field 集合の drift gate。
- `tests/integration/.../f_130_handoff_routing/test_handoff_application_api_parity.py`:
  同一の拒否を CLI と API の双方から実 orchestration に通し、構造化 outcome と exit code
  が一致すること、API が stdout へ何も書かないこと、binding 重複が fail closed で拒否
  され slot が leak しないこと。
- `mozyo-bridge health check`（module-health）、`mozyo-bridge docs validate --repo .`。

## Next

1. #15151 local MCP server と高レベル tool schema（本 API を呼ぶ）。
2. #15152 変更操作の MCP tool へ durable authority 検証を接続。
3. project-gateway operation（consult / child-intake）の application API 化。
