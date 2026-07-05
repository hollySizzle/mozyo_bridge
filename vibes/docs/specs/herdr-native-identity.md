# herdr-native identity と target 解決 (Redmine #13261)

純 herdr セッション (tmux server なし / `TMUX` 未設定 / 隔離 socket) で `mozyo-bridge`
の registry / pane 解決と handoff target 解決を成立させるための contract。tmux pane
user-options を projection の唯一の source にしていた #13253 の target 識別を、herdr の
**assigned name (durable identity)** + **live inventory (`agent list`)** + **launch-time
sender env** に置き換える。

この doc は `spec-route-identity-ledger` (tmux 側 route identity contract) の herdr 対応版
であり、その fail-closed 姿勢 (pane id は cache/snapshot のみ、authority は stable identity)
を踏襲する。

## 1. Identity model

- **durable identity** = herdr **assigned name** の `mzb1_<workspace>_<role>_<lane>` scheme
  (`domain/herdr_identity.py` の `encode_assigned_name` / `decode_assigned_name`, Redmine
  #13247)。PoC #13175 E10 実測で `agent rename` 付与名は `server stop`/restart を越えて永続。
  - `workspace` = mozyo workspace_id (registry / anchor が持つ workspace identity)。
  - `role` = agent kind / runtime provider token = `claude` / `codex` (tmux 側 role resolver の
    `agent_kind` と同一語彙)。mzb1 の "role" field は **workflow role ではなく provider** である。
  - `lane` = checkout-local lane id (未設定は `default`)。
- **transient locator** = herdr の `pane_id` (`agent list` row の `pane_id`、alias `pane` /
  `location`)。per-process 再生成される使い捨て値であり、**identity として persist しない**。
  target への「今」の到達には `rebind_by_name` で live snapshot から都度復元する。
- **workspace_registry は無変更 (#11425)。** herdr anchor に新 schema は足さない。純 herdr
  session の identity anchor は既存の `workspace_id` (registry / `.mozyo-bridge/workspace-anchor.json`)
  + assigned-name scheme で完結する。registry には runtime/pane state を持ち込まない不変条件を維持する。

## 2. Sender self-identity (launch-time env, PRIMARY path)

session-start helper (§5) が launch 時に sender agent process の環境へ自己識別を注入する。
resolver はこれを fail-closed に読む。

- env var (terminal-runtime domain, `domain/herdr_target_resolution.py`):
  - `MOZYO_WORKSPACE_ID` — sender の workspace_id。
  - `MOZYO_AGENT_ROLE` — sender の provider token (`claude` / `codex`)。
  - `MOZYO_LANE_ID` — sender の lane id (空 → `default`)。
- fail-closed rule (`resolve_sender_identity`):
  - `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` が欠落 / 空 → die (`missing_sender_env`)。
  - `MOZYO_AGENT_ROLE` が `claude` / `codex` 以外 → die (`invalid_sender_role`)。
  - repo anchor (`read_anchor`) が読めない / workspace_id を持たない → die (`missing_anchor`)。
  - env の `MOZYO_WORKSPACE_ID` が anchor の `workspace_id` と不一致 → die
    (`env_anchor_workspace_mismatch`)。checkout を跨いだ env leak で別 workspace の名前を
    mint するのを防ぐ。
- **sender env は target authority ではない。** sender env は (a) coordinator pseudo-target の
  provider 解決の workspace scope、(b) lane context の 2 用途に限る。target の identity は必ず
  live inventory + assigned-name decode で決める (§3)。

## 3. Target-resolution semantics

入力: `receiver` (`claude` / `codex` / `coordinator`)、検証済み `SenderIdentity`、live
`agent list` rows、coordinator の provider binding。出力: 単一 target agent の assigned name +
transient locator (`resolve_herdr_target`)。

手順:

1. `receiver` → target role:
   - `coordinator` → `RoleProviderBinding.provider_for("coordinator")` (既定 `codex`, #13174 /
     #12673)。binding が coordinator を bind しない → fail (`coordinator_binding_unresolved`)。
   - `claude` / `codex` → その値を target role とする。
   - それ以外 → fail (`unknown_receiver`)。
2. rows を走査し `decode_assigned_name(row.name)` で decode。decode 不能な row は
   mozyo 管理外 agent とみなし skip する。
3. `decoded.workspace_id == sender.workspace_id` かつ `decoded.role == target_role` の row を
   candidate とする (**workspace + role scope**)。lane は本 US の target 一致キーに含めない —
   純 herdr の単一 session (workspace あたり role ごとに 1 agent) を対象とし、multi-lane cross
   routing は後続 US。
4. fail-closed case (full case list):
   - candidate 0 件 → `no_match` (role 不在 = role mismatch、別 workspace の row 除外 =
     workspace mismatch はいずれも本 reason に畳む。detail で区別する)。
   - candidate が複数の distinct name、または同一 name の重複 row (duplicate assigned name) →
     `multiple_matches` (herdr name uniqueness 違反を推測せず fail)。
   - candidate 1 件だが row に usable locator (`pane_id`/`pane`/`location`) が無い →
     `missing_locator` (空 target への送信を拒否)。
5. 成功時は matched row の assigned name + locator + decoded identity を返す。呼び出し側は
   その assigned name を `rebind_by_name` で fresh snapshot に再照合してから port に渡す。

## 4. Discovery-port boundary (core vs provider)

- **core が所有** (`domain/herdr_target_resolution.py`): sender identity contract、target role
  語彙、fail-closed reason 語彙、`resolve_herdr_target` の pure projection、discovery Port
  Protocol (`HerdrAgentDiscoveryPort`)。ルーティング権限 (どの label が誰に解決されるか) は core。
- **provider が所有** (`infrastructure/herdr_discovery.py`): `herdr agent list` の subprocess
  実行と row 抽出 (`_extract_list_rows` を #13246 と共有)。binary は trusted env
  (`MOZYO_HERDR_BINARY`) からのみ解決し、repo config は backend 選択のみ (#13245 と同一姿勢)。
- `TerminalTransportPort` は **拡張しない**。discovery は send-safety port と別の listing/preflight
  Protocol として同 bounded context に追加する (auditor 回答 j#72519)。
- herdr は `BUILTIN_PROVIDER_REGISTRY` に TERMINAL_RUNTIME provider として登録しない
  (#13245 default-selection ambiguity 回避)。`terminal_transport.backend: herdr` flag が唯一の
  selector。

## 5. session-start one-command (write side)

`mozyo-bridge herdr session-start` (`application/herdr_session_start.py`)。明示 opt-in。backend
flag には結合しない (別々に選べる) が、純 herdr 運用では両者を併用する。

flow:

1. herdr binary を trusted env から解決 (未設定 / 未解決 → fail-closed)。
2. workspace を registry へ確保 (`register_workspace` / `read_anchor` を再利用) し workspace_id を得る。
3. mint durable name: `encode_assigned_name(workspace_id, role, lane)` で mzb1 名を作る。
4. 要求 agent (`claude` / `codex`) を herdr 管理 agent として **durable 名を start 時に付与**して
   launch する (下記 launch contract)。self-identity (`MOZYO_WORKSPACE_ID` /
   `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID`, §2) は `--env KEY=VALUE` で spawn 先へ渡す。
5. idempotency: 対象 slot の mzb1 名を既に持つ live agent があれば **adopt** (再 launch しない)。
   slot に別 locator の同名 agent が複数ある (duplicate) → fail-closed。
6. slot-uniqueness (要求側、#13261 j#72532): 要求された `(provider, lane)` slot が重複する場合は
   **いかなる side effect (binary 解決 / registration / inventory snapshot / launch) より前に**
   fail-closed で拒否する (silent 正規化しない)。同一 slot を二重に prepare すると同じ mzb1 名を二度
   mint し read side が `multiple_matches` で落ちるため。CLI の `--agent` は repeatable のままでよい
   (重複入力を die で弾く)。

### launch contract (herdr 0.7.1 live-measured, coordinator pre-smoke)

staged assumption は解消済み。実 herdr 0.7.1 で計測した確定仕様:

```
herdr agent start <NAME> [--cwd PATH] [--env KEY=VALUE]... [--no-focus] -- <argv...>
```

- `<NAME>` は **必須 positional** で start 時に直接適用される (probe: `result.agent.name == <NAME>`)。
  mozyo は mzb1 durable 名をここで付与し、**別途 `agent rename` を発行しない**。
- self-identity var は client process env では **spawn 先に届かない** (server-spawned agent は
  client env を継承しない、実測)。よって `--env MOZYO_WORKSPACE_ID=...` / `--env MOZYO_AGENT_ROLE=...`
  / `--env MOZYO_LANE_ID=...` で渡す。
- `--no-focus` で operator focus を奪わない。
- 出力は stdout 上の単一 JSON object。rebind/read 用 transient locator は
  `result.type == "agent_started"` envelope 下の `result.agent.pane_id`
  (`_parse_started_locator`、type 不一致 / pane_id 欠落は fail-closed)。

自動テストは injected runner で argv + JSON parse を検証する (live binary は不使用)。end-to-end
live smoke は coordinator の post-review step。

## 6. Close-evidence contract (pure-herdr round trip)

close 判定には次の durable evidence を要求する:

- `TMUX` 未設定、または tmux server 不在 / 隔離 socket であること (session が純 herdr である証跡)。
- `mozyo-bridge herdr session-start` が claude / codex の mzb1 assigned name を mint した log
  (assigned name literal を含む)。
- handoff send が sender env + live inventory から target を解決した log (resolved assigned name /
  locator、fail-closed reason が出ていないこと)。
- 上記を記録した Redmine journal id。

live smoke (実 herdr binary + 実 agent) は coordinator の post-review 実機 acceptance で行う。
本 US の自動テストは全て fake runner で fail-closed 経路を網羅する。

## 7. tmux path freeze

backend=`tmux` の挙動は byte 不変: `tmux_client.py` / `pane_resolver.py` / `commands.py` の
send pipeline / `handoff_transport_wiring` の tmux 経路に behavior change を入れない。
herdr-native 解決は backend=`herdr` 選択時のみ有効。

### orchestrate-entry の backend-aware target 解決 (increment 2)

`orchestrate_handoff` は backend=`herdr` のとき、send target を tmux `pane_info` ではなく本 spec の
herdr-native 解決 (launch-time sender identity + live inventory) で解決し、downstream pipeline が
消費する pane record を synthesize する。synthesize record は **normal_window** projection
(role を `window_name` に載せ `@mozyo_agent_role` は付けない) とし、cockpit 前提の main-lane guard を
不活性にしつつ `binds_receiver` の strong role 判定は成立させる。tmux 専用 side step は backend=`herdr`
で明示 no-op にする (each に rationale コメント + テスト):

- `require_tmux()` — herdr では skip (tmux server 前提を課さない)。
- queue-enter の tmux-session binding gate / cross-session `--to claude` gate — herdr では no-op
  (tmux session 概念が無く、audit boundary は workspace-scoped inventory decode が担保)。
- same-lane duplicate pane snapshot — herdr では明示的に空 (tmux pane snapshot 依存)。identity uniqueness は
  assigned-name decode (duplicate name は fail-closed) が担保。
- select-pane 起点の target activation — #13253 shim の no-op 経由 (herdr は pane focus 不要)。

backend=`tmux` 経路は byte 不変 (全 gate は `if not herdr_send` で strict guard)。fail-closed
(un-attested sender env / unavailable inventory / 単一 live agent に解決しない) は structured
`blocked` / `target_unavailable` outcome を emit して die する。

### 残 residual (未確認事項)

- **live smoke 未実施**: 実 herdr binary + 実 agent での **end-to-end** round-trip (session-start →
  handoff marker landing / turn-start) は coordinator の post-review 実機 acceptance に委ねる。
  `agent start` の CLI 仕様自体は herdr 0.7.1 で実測済み (§5 launch contract)。本 US の自動テストは
  fake herdr runner のみ。
- coordinator pseudo-target (`--to coordinator`) は `resolve_herdr_target` が解決可能だが、
  `orchestrate_handoff` の `RECEIVERS` は `claude`/`codex` のみのため entry からは claude/codex を扱う。
  coordinator callback 経路の herdr 対応は別 surface。
