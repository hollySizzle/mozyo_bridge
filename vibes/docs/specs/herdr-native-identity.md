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

> **Redmine #13305 で route authority を収束 (design record #13305 j#73008)。** 本節の
> lane-less `(workspace_id, role)` projection (`resolve_herdr_target`) は **実 `handoff send`
> path の route authority ではなくなった**。実 send path は §3.1 の backend-neutral route
> authority (lane-in-match `(workspace_id, lane_id, role, pane_name)`) を経由する。
> `resolve_herdr_target` は **legacy compatibility adapter** として残し (translator fallback
> `handoff_transport_wiring._herdr_native_assigned_name` 用)、下記手順はその legacy adapter の
> 仕様として保持する。

入力: `receiver` (`claude` / `codex` / `coordinator`)、検証済み `SenderIdentity`、live
`agent list` rows、coordinator の provider binding。出力: 単一 target agent の assigned name +
transient locator (`resolve_herdr_target`, **legacy adapter**)。

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

## 3.1 実 send path route authority (Redmine #13305 収束)

実 `handoff send` path (`orchestrate_handoff` → `application/herdr_send_entry.resolve_herdr_send_target`)
は §3 の lane-less match ではなく、**単一の backend-neutral route authority** を経由する
(`application/herdr_route_authority.resolve_herdr_route_target`)。tmux path が使う route-identity
ledger と同じ match key `(workspace_id, lane_id, role, pane_name)` に収束させ、route authority を
両 backend で単一化する。

- **route authority = lane-in-match。** canonical assigned name (`encode_assigned_name`) が
  `pane_name` を担い、live `agent list` row は #13247 decode で ledger row 形へ正規化してから
  `backend_neutral_resolver.resolve_route_neutral(..., backend=herdr)` で再照合する。herdr locator /
  pane_id は cache/evidence のみで authority に昇格しない。
- **lane は決定的に導出、全 lane scan しない。** lane 未指定 send は先に単一 lane を導出してから
  その slot を照合する。precedence (最優先から): **explicit lane > sender same-lane (peer
  `claude`/`codex` receiver は sender の lane) > coordinator default (`coordinator` は workspace
  default lane) > legacy default (sender lane 不明/`default`)** (`derive_target_lane`)。導出 lane の
  slot が live でなければ `target_unavailable` / `target_ambiguous` / `route_locator_missing` で
  fail-closed し、`(workspace_id, role)` の全 lane scan に fallback しない。
- **fail-closed 語彙 = #13302 ledger 語彙。** 新 reason token は増やさない (必要時は再 consultation)。
- **gateway-route enforcement gate との関係。** cross-lane worker 送信 (governed
  `implementation_request` `--to claude` を別 lane worker へ) は、lane-in-match により
  **target 解決の時点で `target_unavailable` に落ち**、gateway-route gate に到達する前に
  fail-closed する (同一 invariant を上流で enforce)。gate 本体は tmux path 用に byte 不変で残す。
- **tmux path は byte 不変。** tmux は従来どおり `pane_info` で解決する。`resolve_route_neutral(tmux)`
  が `pane_info` の target と一致することは characterization test で pin する
  (`tests/unit/.../test_herdr_route_authority.py::TmuxByteInvarianceCharacterizationTest`)。

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

### 空 base pane の回収 (cold start、#13330)

herdr workspace は生成時に必ず `root_pane` (agent 無しの空 base shell) を 1 個持つ (実測:
`workspace create` 応答 = `result.type == "workspace_created"` に `result.workspace.workspace_id`
+ `result.root_pane.pane_id`、`pane_count: 1`)。cold start で初回 `agent start` を `--workspace`
無しで呼ぶと herdr が workspace を暗黙生成し、この root pane が使われない残骸として agent pane の横に
残る (dogfood 発見 #12)。回収は次の決定的手順で行う (auditor ruling #13330 j#73225、対処 (a) 採用):

1. 全 slot を launch 前に分類する (adopt / launch / dry-run plan)。
2. launch する slot があり、かつ adopted agent が既存 workspace を pin していない (pure cold start) 場合は
   **明示的に** `herdr workspace create --cwd <repo> --no-focus` を呼び、応答の `workspace_id` と
   `root_pane.pane_id` を保持する。応答が parse 不能なら fail-closed (推測で pane を閉じない)。
3. 各 launch slot を `agent start --workspace <workspace_id>` で起動する (herdr が second workspace を
   暗黙生成しない)。
4. **全 launch 成功後に限り** `herdr pane close <root_pane_id>` で、この run が生成した root pane
   **のみ**を閉じる。

fail-closed / safety 不変条件:

- 閉じる対象は **この run が `workspace create` で得た `root_pane.pane_id` 一点のみ**。scan で「空
  shell らしき pane」を探して閉じることは禁止 (user 自身の shell を誤 close しない構造的保証)。
- launch 失敗は reclaim より前に raise する (created workspace / root pane は残骸として残し、実装失敗
  として扱う。blind close しない)。
- `pane close` 失敗は **non-fatal** (agent slot は既に live で、空 base pane は cosmetic 残骸)。
  `SessionStartResult.base_pane_detail` に記録し、session-start 全体を hard-fail しない。
- all-adopt / 既存 workspace への launch は base pane を新規生成しないため byte-invariant。
- workspace_registry schema は無変更 (§2 invariant 維持)。herdr terminal workspace id は
  `SessionStartResult.herdr_workspace_id` (created / adopted prefix) として観測用に運ぶだけで、mozyo
  registry には持ち込まない。mixed adopt+launch では adopted locator の `wN` prefix から launch target を
  導出し、複数 workspace prefix が混在する場合は fail-closed。

live smoke (cold start bare `mozyo` 後に root pane が残らないこと、adopt 経路が byte-invariant で
あること) は coordinator の post-review 実機 acceptance で確認する。

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
- **gateway-route enforcement gate の sender lane 解決 (increment 4)** — 従来は
  `current_pane_lane_unit()` → `pane_lines()` (tmux `list-panes -a`) を **backend 非依存で無条件に**
  呼んでおり、純 herdr で (a) no-tmux 契約違反、(b) sender identity unknown → gate が silent skip
  (cross-lane worker 送信が素通り) の 2 問題があった (実機 smoke で検出、j#72517 系)。increment 4 は
  backend=`herdr` のとき **env 由来 SenderIdentity** (`MOZYO_WORKSPACE_ID` / `MOZYO_LANE_ID`、target 解決で
  既に resolve 済みを thread) を `enforce_gateway_route(sender_lane_unit=...)` に渡し、gate は **env
  sender lane で enforce し tmux call を 0 にする**。tmux backend では `sender_lane_unit=None` で
  `current_pane_lane_unit()` 経路が byte 不変。純 herdr では sender lane は常に既知 (欠落は target 解決で既に
  fail-closed) なので gate は skip せず enforce する。

herdr send path の tmux-capable call site 全数監査 (increment 4): `require_tmux` / `pane_info` /
same-lane duplicate / queue-enter・cross-session session gate / gateway-route sender lane はいずれも
herdr-branch or gated no-op。send/capture/Enter (`run_tmux` / `capture_pane`) と select-pane activation は
#13253 shim 経由 (herdr port / no-op)。`window_active_pane_id` (pane_lines 読み) は send path から未到達。

backend=`tmux` 経路は byte 不変 (全 gate は `if not herdr_send` / `sender_lane_unit=None` で strict guard)。
fail-closed (un-attested sender env / unavailable inventory / 単一 live agent に解決しない) は structured
`blocked` / `target_unavailable` outcome を emit して die する。

### 残 residual (未確認事項)

- **live smoke 未実施**: 実 herdr binary + 実 agent での **end-to-end** round-trip (session-start →
  handoff marker landing / turn-start) は coordinator の post-review 実機 acceptance に委ねる。
  `agent start` の CLI 仕様自体は herdr 0.7.1 で実測済み (§5 launch contract)。本 US の自動テストは
  fake herdr runner のみ。
- coordinator pseudo-target (`--to coordinator`) は `resolve_herdr_target` が解決可能だが、
  `orchestrate_handoff` の `RECEIVERS` は `claude`/`codex` のみのため entry からは claude/codex を扱う。
  coordinator callback 経路の herdr 対応は別 surface。
