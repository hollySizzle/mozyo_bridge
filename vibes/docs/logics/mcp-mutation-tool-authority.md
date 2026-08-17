# MCP Mutation Tool Authority

Redmine #15152（親 Feature #15148 180_LLM向けMCP操作入口）。local MCP server が公開する
**mutating tool**（handoff / sublane の高レベル変更操作）と、その実行前 durable authority
検証を確定する設計正本。

前提となる境界は `cli-mcp-shared-application-api.md`（#15149、「判断は 1 箇所、entry は
2 つ」）と `local-mcp-tool-surface.md`（#15151、read/plan surface・framing・projection
規律）。本書はその上に **mutating entry** を置く。managed LLM の入口切替（#15150）、
段階導入と操作履歴検証（#15153）、CLI の維持境界（#15154）を含まない。

## Decision

mutating tool は **宣言された閉集合** `MUTATING_TOOL_NAMES` の 3 つのみ。

| tool | 対応 CLI | 共有 application body |
| --- | --- | --- |
| `handoff_send` | `handoff send` | `run_handoff(HandoffRequest)`（#15149 typed API） |
| `handoff_reply` | `handoff reply` | 同上（operation=`reply`、entry policy は core 表） |
| `sublane_start` | `sublane create/start` | `run_sublane_start(SublaneStartCommand)`（本書で追加した typed shared service） |

anchorless の `ticketless_callback` と受信者固定の `cross_workspace_consult` は本 round の
tool 語彙に**含めない**。durable anchor 検証を核とする本 surface の主張に合わず、#15153 の
段階導入実測を経て別 round で判断する。

## Invariants

1. **判断は共有 body にのみ存在する。** tool handler は判断を持たない。handoff は
   `run_handoff` が entry policy と orchestration 全 gate（durable anchor ownership /
   receiver vocabulary / identity / gateway route / send safety）を適用し、sublane は
   `run_sublane_start` が CLI と同一順序の admission（work-unit config fail-closed →
   **#15146 delegated_coordinator parent-authority admission** → provider launchability
   preflight）と actuation use case 内 gate（identity / anchor / sender attestation /
   fill admission / pre-mutation admission）を適用する。MCP は CLI が通す gate を skip
   できず、CLI が通さない gate を足さない（#15149 Invariant 2 / 3 の継承）。
2. **authority 不足・identity 不一致・曖昧 target は副作用前に typed refusal。**
   sublane の admission refusal は閉じた reason token（`invalid_repo_local_config` /
   #15146 の `parent_*` 4 token / `provider_unresolved` / `provider_not_launchable` /
   anchor ownership の `anchor_*` 4 token）で返り、worktree / pair / dispatch の副作用は
   ゼロ。handoff の refusal は orchestration が publish する structured blocked outcome
   （`DeliveryOutcome.reason` の閉語彙）で返る。
   **actuation は dispatch の有無を問わず durable authority を要求する**（R2、review
   j#106834 finding_authoritybypass）: `actuate=true` は journal を必須とし
   （`anchor_required`）、その journal が当該 issue に実在し帰属することを共有 authority
   `verify_live_handoff_anchor`（#14246）で **worktree / pair mutation より前に**検証する。
   sender attestation preflight も create-only を含む全 actuation で mutation 前に走り、
   **capability の欠落自体が typed refusal**（R3、review j#106868
   finding_senderauthoritygap）: `preflight_dispatch_sender` を持たない ops port は
   `sender_authority_capability_missing` で fail-closed になる（#13613 の port-opt-in
   読みは撤回 — port 不在は authority 成立ではない）。判定本体は 1 つの共有 authority
   `evaluate_dispatch_sender`（workspace anchor + env 解決 sender identity +
   coordinator provider binding + default lane 照合。herdr runtime 非依存）で、herdr
   adapter と tmux の `LiveSublaneActuatorOps` の双方がそれへ委譲する。dispatch leg の
   send-time 検証は従来どおり残るが、それだけでは lane 作成後の検出になる — その順序
   こそが R1 finding の内容だった。この強化は共有 body（service / use case、gate 本体は
   `sublane_actuator_gates.sender_authority_admission`）側にあり、CLI の
   `--execute --no-dispatch`（journal なし create-only）も同じ契約変更を受ける
   （MCP-only の adapter gate は作らない）。
   **ただし sender check は caller 認証ではない**（ADR-0006、review j#106903
   finding_clientenvspoof）: `evaluate_dispatch_sender` が照合する `MOZYO_*` は呼び出し元
   プロセスの ambient env であり、stdio MCP server では client 制御下。照合先 (workspace
   anchor は gitignore だが workspace_id は board / assigned name / journal marker に半公開、
   coordinator provider は committed / 既定 codex、lane_id は定数) は repo / 運用面から取得
   可能なので、client は一致値を自己設定できる = **偽造可能**。これは弱い identity 信号で
   あり偽造防止 authority ではない。**現段階の信頼境界は runtime perimeter (同一ホスト・
   attested pane 内に居ること)** と正直に宣言する (SERVER_INSTRUCTIONS / tool description /
   本 doc)。偽造不能な caller 認証 (operator トークン → 証明書 / #15195) は user 貸し借り・
   ネットワーク露出機能と同時に導入する (#15579 へ park)。finding_clientenvspoof は本 threat
   model 明示化により deferred (再評価トリガー = 露出拡大機能の実装)。
3. **MCP adapter は #15146 の回避策ではない。** parent-authority の判定は
   `delegated_parent_authority_gate`（core 修正そのもの）を service 経由で呼ぶ。CLI 側
   handler `cmd_sublane_start` も同じ service を呼ぶ Namespace adapter に縮退しており、
   gate 連鎖の第二実装は存在しない（#14224 の plan/execute drift の再発防止。regression が
   AST で構造断言する）。
4. **raw surface は表現できない。** mutating tool の input schema に pane locator / tmux
   target / 任意 command 文字列は存在せず、`catalog_surface_violations()` の禁止 token
   検査は mutating tool にも適用される。`read_only=False` は `MUTATING_TOOL_NAMES` の
   member だけに許され、宣言 member が read-only を名乗ることも violation（双方向）。
   server 起動時 fail-closed は #15151 のまま。
5. **unmanaged row へ副作用を届かせる経路を持たない**（#15152 j#102930 / j#102998）。
   受信者は role 語彙、lane は identity であり、配送解決は managed assigned identity と
   durable authority を経由する。assigned identity を持たない（`decode_reason=empty_name`）
   live row は名指しできない — 拒否は enumeration ではなく**構造**で成立する。起動系の
   副作用は canonical creator rail（herdr session rail / cockpit append、identity 付与込み）
   のみを通る。runtime 発行 action receipt による更なる束縛は upstream 依存
   （#15195 NO-GO、Herdr Discussions #2652）で本書の scope 外。
6. **result projection は allowlist。** #15151 の r4f3 / r5f1 規律を mutating 側にも適用
   する。republish するのは閉 token（status / injection stage / dispatch result /
   `blocked_reasons` の閉 token 等）と caller 供給 identity（anchor issue/journal、
   lane_label 等）のみ。producer 自由文（CLI `die` message、`next_action` prose、step
   command line、**および sublane の `outcome.reason`** — gate の自由文 detail を連結する
   ため private path / exception 本文を含み得る。R4、review j#106903
   finding_reasonproseleak）と pane / private path evidence（`target` / `worktree_path`
   / `gateway_pane` / `steps`）は**捨てる**（scrub しない）。公開 `reason` は
   `status` と `blocked_reasons`（いずれも閉 token）から固定文で**再構成**し、summary も
   閉 token のみで組む。operator 詳細は CLI に残す。
7. **delivered は injection-stage authority のみから導く。** `delivered: false` +
   `status: completed` は「送信は終端したが submission 未確認」であり、exit code から
   配達を推定しない（#13583 / #14232 の継承）。
8. **`sublane_start` は plan 既定。** `actuate: true` の明示だけが副作用を許す。input
   property 名は `actuate`（`execute` は禁止 token `exec` に部分一致するため。guard を
   弱めず名前側を合わせた判断）。

## Layers

```text
MCP tools/call (handoff_send / handoff_reply / sublane_start)
        v
f_180 application/mutation_tools.py     <- projection allowlist だけを持つ
        |                                   (判断ゼロ、subprocess ゼロ)
        +-- run_handoff(HandoffRequest)             [f_130, #15149]
        |         v
        |   orchestrate_handoff_input -> run_handoff_orchestration (全 gate)
        |
        +-- run_sublane_start(SublaneStartCommand)  [f_140, #15152 新設]
                  v
            resolve_work_unit_fields (共有 precedence)
            delegated_parent_authority_verdict (#15146 gate)
            provider_preflight_refusal (#13569)
            verify_live_handoff_anchor (#14246 — actuate 時の pre-mutation
                anchor ownership、R2 j#106834)
            _resolve_sublane_ops -> SublaneActuateUseCase.run (actuation 全 gate。
                anchor 必須と sender attestation は dispatch=false を含む)
                  ^
            CLI cmd_sublane_start も同じ service を呼ぶ（Namespace はここで終端）
```

## Verification

- `tests/unit/.../f_140_.../test_sublane_start_service.py`: admission の順序と typed
  refusal、provider snapshot の到達、request field の CLI 等価、blocked → exit 1。
- `tests/unit/.../f_180_.../test_mcp_mutation_tools.py`: projection allowlist（pane /
  path / prose の不在）、refusal の固定文再構成、mutating 宣言 guard の双方向検出、
  禁止 token 検査の mutating 適用。
- `tests/regressions/test_issue_15152_mcp_mutation_authority.py`: #15146 admission が
  MCP の plan / actuate 両 path で typed に発火し副作用ゼロであること、CLI と同一
  token であること、両 entry が単一 service body を通る構造断言、receiver gate の
  CLI/API/MCP 一致、pane locator 引数が schema 境界で protocol error になること。
- 既存 suite の契約更新: catalog は 7 tool（4 read + 3 mutating）、`handoff_send` は
  unknown-tool probe から除外、#13569 R3-F1 の snapshot spy seam は service へ移動
  （契約は不変）。

## Non-goals

- `ticketless_callback` / `cross_workspace_consult` / `workflow step` execute の tool 化。
- runtime 発行 action-bound receipt（#15195、upstream 待ち）と、生成元 rail / action id の
  read API 投影。
- managed LLM の標準入口切替（#15150）、段階導入・実機操作履歴検証（#15153）。
- 破壊的操作（retire / kill / delete）の tool 化。公開 mutating surface は additive のみ。

## Next

1. #15150 managed LLM の標準操作入口を MCP へ切り替える。
2. #15153 MCP 標準操作入口を段階導入し操作履歴で検証する（3 層 live smoke を含む）。
