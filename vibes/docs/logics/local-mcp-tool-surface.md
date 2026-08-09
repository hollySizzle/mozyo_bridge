# Local MCP Tool Surface

Redmine #15151（親 Feature #15148 180_LLM向けMCP操作入口）。installed package から
起動できる local MCP server と、LLM が使う高レベル read/plan tool schema、そして
Unit 状態の read model を確定する設計正本。

前提となる境界は `cli-mcp-shared-application-api.md`（#15149）が確定した
「判断は 1 箇所、entry は 2 つ」である。本書はその上に **MCP 側の entry** を置く。
本書は mutating tool への authority 検証（#15152）、managed LLM の入口切替（#15150）、
CLI の維持境界（#15154）を含まない。

## Decision

local MCP server は **read/plan のみ** を公開する。tool 語彙は閉じた 4 つで、
外部から 5 つ目を表現する手段を持たない。

| tool | 対応 CLI | 内容 |
| --- | --- | --- |
| `docs_resolve` | `docs resolve` | 変更対象 path を governing docs へ解決する |
| `workflow_glance` | `workflow glance` | active lane を workflow state / next action / delivery anomaly へ射影する |
| `workflow_step_plan` | `workflow step --dry-run` 相当 | 次に安全な 1 step を **解決するだけ**。dispatch しない |
| `unit_state` | （新規） | 指定 Unit の状態を 4 軸で read-only に返す |

実装は `src/mozyo_bridge/e_110_execution_platform/f_180_llm_mcp_operation_entry/`。

## 依存を増やさない判断（#15151 実装時の照合結果）

MCP SDK を runtime dependency に追加**しない**。

- 照合した一次仕様: MCP `2025-06-18` revision の `basic/transports`（stdio framing）、
  `basic/lifecycle`（initialize / initialized / shutdown）、`server/tools`
  （`tools/list` / `tools/call` / protocol error と tool execution error の分離）。
- 照合した packaging 正本: `pyproject.toml` の `dependencies`（`build` / `PyYAML` /
  Python 3.10 向け `tomli` backport のみ）。
- 判断: 必要な surface は newline-delimited transport 上の 5 method
  （`initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`）
  であり、公開 CLI package に async client stack を持ち込む正当化に足りない。
- 代償: wire contract を repo 側に再記述する。これを **1 つの pure module**
  （`domain/jsonrpc.py`）に閉じ、test で pin することで代償を局所化する。

## Layers

```text
MCP client (spawns the server)
        |  stdio: newline-delimited UTF-8 JSON-RPC 2.0
        v
application/mcp_server.py        <- lifecycle, framing discipline, stdout規律
        v
application/tool_dispatch.py     <- 閉じた語彙 -> handler、2種の error channel分離
        v
application/read_plan_tools.py   application/unit_state_tool.py
        v
共有 application / core 処理（docs_tools resolver / glance pipeline /
resolve_workflow_step / lane lifecycle store）
```

`domain/` は pure（I/O なし）。`application/` だけが環境に触れる。

## Invariants

1. **read/plan のみ。** 公開 tool は 4 つで、mutating handoff / sublane 操作、任意
   command 文字列、shell argv、raw pane / tmux 操作を **schema 上表現できない**。
   これは散文の約束ではなく `catalog_surface_violations()` が構造検査する
   （input property 名と enum 値を禁止 token 集合に照合し、server 起動時に
   fail-closed）。
2. **MCP が CLI を subprocess 実行しない。** handler は CLI と同じ in-process entry
   を呼ぶ。client が server を spawn するのは stdio transport そのものであり、
   本条の対象ではない。
3. **CLI が通す gate を skip せず、CLI が通さない gate を足さない**
   （`cli-mcp-shared-application-api.md` Invariant 2 / 3 を継承）。例:
   `workflow_step_plan` の anchor 規則は CLI の `_anchor_from_args` と同一
   （`issue` 必須、`journal` 任意）。**backend 選択も判断であり複製しない**
   （`## Backend 選択の共有`）。
4. **stdout は MCP frame 専用。** 診断は stderr。frame は改行を含まないことを
   producer 側で検査し、書けない response は internal error frame へ degrade する。
5. **受理した request には必ず 1 応答。notification には 1 応答も返さない。**
   handler が例外を投げても tool execution error として応答する。notification か否かは
   `id` member の**存在**で決まり、値では決まらない（`## Lifecycle と id 契約`）。
6. **outcome は構造化。** caller に stdout prose の parse を要求しない。
   protocol error（unknown tool / schema 違反 / lifecycle 違反）は JSON-RPC error、
   tool execution error（source 読めず / selector 拒否 / path 契約違反）は
   `isError: true` の result。
7. **external plugin API を公開しない**（`plugin-ready-adapter-boundary.md` の
   非目標を継承）。tool catalog は import 時に決まる frozen table で、registration
   hook を持たない。
8. **tool result に private path と exception 本文を出さない。** 拒否理由は固定
   token とその固定文言で表す（`## Path 契約`）。

## Lifecycle と id 契約

（review j#102186 finding_1 / finding_4 で確定。）

lifecycle は **3 状態の phase machine** で持つ。bool は「initialize 未着」と
「initialize 済み・client 未確認」を区別できず、`notifications/initialized` 単独で
tool surface 全体が開いてしまった。

| phase | 応答する request |
| --- | --- |
| `uninitialized` | `initialize` / `ping` のみ。他は fail-closed |
| `initializing` | `ping` のみ（spec が許す唯一の先行 request） |
| `ready` | 全 surface |

- `initialize` は `protocolVersion` / `capabilities` / `clientInfo` の存在と型を検証する。
  欠落・型不一致は `ERROR_INVALID_PARAMS`。2 度目の `initialize` は拒否する
  （再 negotiation を許すと、既に応答済みの request がどの version で処理されたか
  client が決定できない）。
- version negotiation は未対応 version を error にせず server 側 version を返す
  （MCP lifecycle 仕様どおり。client が使えなければ切断する）。
- **notification 判定は `id` member の存在で行う。** JSON-RPC 2.0 は
  「A Notification is a Request object *without an "id" member*」「id ... MUST contain
  a String, Number, or NULL value *if included*」と定める。したがって明示
  `"id": null` は Request であり null id で応答する。member 不在のみが notification。
  値だけで判定すると、client が待っている call を黙って捨てる。
- `id` の値域は String / Number / NULL。**Boolean は拒否する**（Python では `bool` が
  `int` の subclass なので `isinstance(x, int)` を素通りする）。
- id を読めなかった frame（parse error / batch / 非 object / 過大）は null id で応答する。
  notification だったと仮定して黙るより、request を hang させない方を採る。

## Backend 選択の共有

（review j#102186 finding_2 で確定。）

`workflow step` は repo の `terminal_transport.backend` で lane 解決 rail を選ぶ。
MCP 側がこれを持たず tmux rail を無条件に呼ぶと、herdr backend の repo では CLI が
lane を解決できるのに MCP だけ `lane_unresolved` を返す — **backend を知らない第二の
状態機械**になる。

したがって選択は `f_140_.../application/workflow_step_plan_resolution.py` に 1 本化し、
`herdr_backend_active()`（CLI と同じ共有 selector）で rail を選んでから各 rail の
resolver へ委譲する。この entry は **resolution 専用**で、dispatch / delivery /
lifecycle mutation / durable write を一切行わない。CLI 側の実行経路（store reconcile、
startup-resume gate、disposition intake、forward leg）はそのまま CLI が持つ。

`resolve_herdr_step_outcome` が Namespace を取る点は CLI 隣接層で終端させ、MCP feature
へ Namespace を持ち込まない（`cli-mcp-shared-application-api.md`「Namespace はここで
終端」）。同 resolver が read するのは `repo` のみで、これは test で pin する。

## Path 契約

（review j#102186 finding_3 で確定。）

`docs_resolve` の `paths` は **repo-relative** であり、resolver に渡す前に
`domain/repo_path.py` が強制する。絶対 path（POSIX / Windows drive / UNC）、`..` で
repo root を出る path、非文字列、空を閉じた token（`absolute` / `escapes_repo` /
`not_text` / `empty`）で拒否する。正規化は **字句的**で filesystem を触らない。

拒否文言は **固定**であり exception 本文を使わない。以前は catalog resolver の
`ValueError` を全文返しており、caller が知らない **server 側の絶対 repo root** が
structured result に出ていた。catalog / overlay 読み取り失敗も同様に固定 reason と
exception **型名**のみを返す。

## Unit 状態 read model（#15162）

### なぜ 4 軸に分けるか

#15151 j#101743 の correction_basis: 先行 session が「Redmine journal 未更新」と
「worker 実装中」を **どちらも不在の観測** であるにもかかわらず単一の `blocked`
へ畳んだ。単一 bool には「判断できない」を置く場所が無く、読みが主張に化けた。

したがって 4 軸を **並置し、相互に畳まない**。

| 軸 | source | 所有するもの | 所有しないもの |
| --- | --- | --- | --- |
| `workflow` | Redmine durable record | issue status、latest gate / journal、next owner / action | runtime 観測 |
| `runtime` | herdr / terminal runtime | gateway / worker の観測状態 | workflow truth、review、owner approval、task completion |
| `delivery` | delivery ledger / durable record | gateway / worker への dispatch outcome | 受信側の処理完了、task completion |
| `health` | 上記3軸の観測品質 | anomaly / degraded / freshness | 他3軸が何を言うか |

`ack-completion-receiver-state.md` の layer 分離（layer 0 delivery ACK / layer 1
runtime receiver signal / layer 3 workflow truth）をそのまま軸に写したものであり、
新しい receiver-state 語彙を本 repo 側に生やしていない。

### 観測 envelope

報告する **すべての** field が `ObservedField` であり、`source` / `observed_at` /
`freshness` / `readability` を持つ。provenance の無い裸のスカラを表現する経路は無い。
`freshness` / `readability` / `source` の語彙は
`runtime-observability-boundary.md` の snapshot envelope 契約
（`domain/runtime_observation.py`）を再利用し、第二の語彙を作らない。

`unobserved` のみ本 Feature の追加値で、「その source を一度も参照していない」を
「参照したが何も無かった」と区別するために置く。

### 不在から状態を導出しない

`FORBIDDEN_INFERENCE_BASES`（journal 無更新 / pane 本文 / stdout silence /
turn ended / prompt idle / 出力なし）から `blocked` / `idle` / `completed` を
導出しない。read できなかった field は `unknown`、dispatch したが着弾を観測して
いない delivery は `unconfirmed` であり、いずれも状態ではない。

`idle` について: glance の共有 fold が返す `idle` は **可読な、gate の無い durable
record** から導出されたものであって沈黙からではない。durable facts が読めなければ
fold 自身が `unknown` を返す。したがって composer 側で `idle` を再度伏せない
（伏せると durable record が支持している状態を隠す）。

### `blocked` の admission と解除

`blocked` は次の 3 つが揃ったときだけ返す。

- **authoritative blocker source** — workflow 軸では Redmine durable record のみ
  （layer 1 / layer 2 の signal は権限を持たない）。delivery 軸では ledger も可。
- **reason**
- **resume condition**

加えて `durable_anchor` が **その宣言自身** を指すこと。欠けたものが 1 つでもあれば
claim ではなく、workflow state は `unknown` へ degrade し、何が欠けているかを note
で述べる。「おそらく blocked」のような hedge は作らない（同じ無根拠な主張に
前置きを付けただけになる）。

読み取り文法は既存の governed parked-state journal（skill `## Sublane 完了 guardrail`
の固定 field shape）の `governed_field` をそのまま使い、第二の文法を作らない。
ただし要求するのは **blocker 部分集合**（`state: blocked` / `blocked_by` /
`resume_condition` / `durable_anchor`）であり、`park_journal_gap` が追加で要求する
callback outcome 部分は含めない。後者は「誰かに伝えたか」という別の問いであり、
auto-hibernate の evidence bar としては正しいが、「この Unit は blocked か」の
bar としては過剰で、retry command が不完全な宣言まで `unknown` に落としてしまう。

**解除も同じ強さで扱う**（review j#102186 finding_5 で確定）。宣言を読むだけでは
「blocked が宣言された」ことしか分からず、それが今も継続しているかは別の事実である。
2 つの authority を連言する。

1. **journal 走査**（`latest_blocker_claim`）: 新しい順に読み、`blocked` 以外の値を
   持つ governed `state:` 宣言に当たったらそこで打ち切って `None` を返す。`state:` を
   持たない無関係な journal は skip する（進捗 log が standing block を隠さないため）。
   `state:` が相異なる値で重複する記録も打ち切る（2 つのことを言う記録は継続も解除も
   証明しない）。
2. **durable fold との一致**（`compose_unit_state`）: 現在の gate fold が `blocked`
   でなければ claim を落とす。fold が `unknown`（Redmine 読めず）の場合も落とす —
   継続を確認できない block を「現在の block」として報告しない。

解除済みの block を報告することは、本 US が防ごうとしている欠陥を逆方向に犯すこと
であり、docstring に設計意図を書いても出力がそう振る舞わなければ意味がない。

claim も他の field と**同じ観測 envelope**（`observed_at` / `freshness`）を持つ。
live adapter はその read の実時刻を stamp する。

### Unit identity

`unit-target-model.md` の `UnitRecord` に従う。selector の必須 3 要素は
`workspace_id` / `lane_id` / `project_id`（project/governance context）。
`host_id` / `repo_label` / `ticket_system` は narrowing のみ。

拒否は typed な閉語彙で返す。

| reason | 意味 |
| --- | --- |
| `missing` | 必須要素が無い（Unit が名指されていない） |
| `unknown` | 必須3要素に一致する Unit が無い |
| `ambiguous` | narrowing 適用後もなお複数一致（推測しない） |
| `mismatch` | Unit は在るが supplied narrowing 値と矛盾する |
| `foreign` | 権限 scope 外の Unit |

`unknown` と `mismatch` を分けるのは caller の次の行動が違うため。scope が解決
できない場合（`authorized_workspace_ids=None`）は wildcard ではなく **何も許可
しない**。

`UnitRecord` payload は Target を含まない。pane id / session / worktree path を
返さない。Unit は配送先ではなく、本 Feature の tool は read-only なので、
side effect の宛先を権限の無い surface へ渡さない。

## Shared glance source wiring（#15151 で追加した共有点）

`workflow glance` の 5 adapter（workflow-runtime store / reconcile store / herdr
delivery ledger / glance Redmine source / authority index）の構築を
`application/glance_source_wiring.py` に 1 本化し、CLI と MCP の双方がそれを呼ぶ。
判断ではなく adapter 構築だが、2 箇所で構築すると同じ repo から別 store を読み
別の projection を返しうるため、`cli-mcp-shared-application-api.md` が handoff
family に対して閉じた重複と同種の問題になる。

roster 解決（`enumerate_active_lanes_for_repo`）は元から共有 1 関数であり、
CLI の `_roster` はそれを直接呼ぶ形のまま残す。同 module-level 名は #14813 の
regression test が使う monkeypatch seam であり、4 行の重複排除のために test seam を
壊さない。MCP 側は同じ enumerator を `roster_for` 経由で呼ぶ。

behavior 差分（意図的、#15151）: workflow-runtime store の構築が fail-open に
なった（従来は例外が read-only な `workflow glance` を落としえた）。他 4 adapter
は従来から fail-open。

## Non-goals

- mutating MCP tool と durable authority 検証（#15152）。
- managed LLM の標準操作入口切替（#15150）。
- CLI の削除 / 維持境界（#15154）。
- external plugin API。
- receiver-state detector を本 repo に生やすこと
  （`ack-completion-receiver-state.md` `## 運用への帰結` 5）。
- 本 tool result shape の public ABI / 互換保証。internal であり変更しうる。

## Verification

- `tests/unit/.../f_180_llm_mcp_operation_entry/test_mcp_jsonrpc_framing.py`:
  framing / parse / encode の fail-closed、batch 拒否、改行埋め込み拒否。
- `.../test_mcp_tool_catalog.py`: 閉じた語彙、shipped catalog に違反ゼロ、
  **かつ** 禁止カテゴリ（arbitrary command / shell argv / raw pane / tmux /
  forbidden enum / non-read-only / 未実装 schema keyword）を注入した合成 catalog を
  guard が実際に捕まえること。
- `.../test_mcp_unit_selector.py`: missing / unknown / ambiguous / mismatch /
  foreign、scope 未解決が wildcard にならないこと、`unit_id` が cockpit read model
  と byte 一致すること。
- `.../test_mcp_unit_state_axes.py`: 全 field の provenance、`blocked` の
  admission（各 part 欠落・非権威 source）、journal 無更新 + worker 実装中が
  `blocked` にならないこと、runtime unknown が workflow を捏造しないこと、
  `unconfirmed` と `unknown` の区別、freshness 期限切れの degrade。
- `.../test_mcp_blocker_claim.py`: governed field 文法、conflict 重複、anchor 不一致。
- `.../test_mcp_read_plan_tools.py`: 構造化 outcome、AST 上で subprocess / process
  launcher / CLI entry 呼び出しが無いこと、共有 application 処理を呼んでいること。
- `tests/integration/.../test_mcp_stdio_session.py`: lifecycle、未初期化拒否、
  unknown method / tool / malformed 引数、stdout 規律、EOF での終了、
  installed package（`python -m mozyo_bridge mcp serve`）の stdio smoke。
- `tests/regressions/test_issue_15151_mcp_review_findings.py`: review j#102186 の
  5 finding を finding ごとの class として pin する（lifecycle bypass / backend
  選択 / path 契約と leak / id member 契約 / blocked claim の解除と envelope）。
- `mozyo-bridge health check`、`mozyo-bridge docs validate --repo .`。

## Next

1. #15152 変更操作の MCP tool へ durable authority 検証を接続する。
2. #15150 managed LLM の標準操作入口を MCP へ切り替える。
3. blocker claim の live producer 拡充: 現状 live adapter は governed parked-state
   宣言のみを claim として読む。それ以外の durable blocked 記録は
   `unknown` + note へ degrade する（fail-closed 側の残件）。
