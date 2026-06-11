# OTel Event Store (段階1: 受け口 + ストア + activity 判定)

Redmine #11639 / #11672 / #11673。ユニット状態検知 (三層 join コックピット) の中核となる OTel イベントストアの設計正本。実装は `src/mozyo_bridge/otel_store.py` (store)、`src/mozyo_bridge/application/otel_receiver.py` (OTLP/HTTP 受け口)、`src/mozyo_bridge/domain/agent_activity.py` (activity / idle 判定)。owner 決定は #11639 journal #56088。

## 三層 join における位置づけ

```yaml
三層:
  redmine: gate 状態 (誰の番か) の正本。runtime heartbeat は書かない
  otel_store: 動いているか (telemetry 発火) — 本 logic。best-effort
  tmux: 生きているか — agents list / session inventory (正本は runtime)
不変条件:
  - otel_store は正本ではない。受け口停止中のイベントは lost で受容する (push 型)
  - OTel の沈黙は「待機」と「死亡」を区別しない。判定語彙は active / idle / unknown のみで、死亡判定は tmux 層へ縮退する
  - 公式 OTel Collector は立てない。受け口は mozyo-bridge 自前 (OTLP/HTTP)。同じ OTLP を話すため、将来 Collector へ env 不変のまま差し替え可能
  - SQLite single-writer。受け口 process が唯一の writer (HTTP server は single-thread 構成で直列化)。Postgres / MQ / 汎用 pipeline は導入しない
  - プロンプト本文・個人情報を保存しない。allowlist + deny token (deny 優先) で usage / event 種別 / 最小 identity のみ。log record body は読まない。opt-in 経路も作らない
```

## ストア (otel-events.sqlite, schema v1)

```sql
PRAGMA user_version = 1;
CREATE TABLE otel_events (
    id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,   -- 受け口時計 (UTC ISO)。判定の基準
    event_time TEXT,             -- payload 申告時刻 (あれば)
    signal TEXT NOT NULL,        -- logs | metrics | traces
    event_name TEXT NOT NULL,    -- eventName / metric 名 / span 名
    service_name TEXT,           -- resource attr (claude-code 等)
    session_id TEXT,             -- CLI session id (tmux session ではない)
    pid TEXT, cwd TEXT,          -- 段階2 pane_id join の match hints
    attrs_json TEXT NOT NULL     -- allowlist 通過後の scalar のみ
);
CREATE TABLE otel_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

- 置き場: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/otel-events.sqlite`。inventory.sqlite と同種の regenerable cache (消えても identity は壊れない)。
- 保持: 既定 7 日。受け口起動時に prune。
- read 側は missing / corrupt / 未知 schema を空として degrade。write 側の corrupt は退避 + 再起動 (regenerable のため)。

## 受け口 (OTLP/HTTP)

- `mozyo-bridge otel serve [--host] [--port] [--db]` — foreground。launchd 常駐化・upgrade/再起動手順は後続 task (構成は launchd 前提: foreground / clean shutdown / 単一 port)。
- bind は 127.0.0.1 のみ。port 既定 4318 (OTLP/HTTP 標準。agent 側は `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`)。
- `POST /v1/{logs,metrics,traces}`: `application/json` は stdlib decode。`application/x-protobuf` は optional extra `mozyo-bridge[otel]` (`opentelemetry-proto`) 導入時のみ decode し、未導入は 415 + 対処 (extra install または agent 側 `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`)。gzip Content-Encoding 対応。
- `GET /healthz`: store path / 件数 / last_write。`mozyo-bridge otel status` が doctor 連携の前段としてこれを叩く (doctor 本体への組込みと env 未注入検出は後続 task)。
- env 注入 (`OTEL_EXPORTER_OTLP_ENDPOINT` 等) の正本は session bootstrap (後続 task)。未注入ユニットは store に現れず activity=unknown → tmux 層縮退で安全側。

## Activity / idle 判定 (#11673)

- source key は `(service_name, session_id)`。pane_id ではない。段階2 join は `match_hints {pid, cwd}` を使い、#11628 の pane_id 同一性キーへ畳む (本 logic は pane identity を発明しない)。
- 判定: 最新 event が window (既定 120s) 以内 → `active`、超過 → `idle`、record なし / 時刻不明 → `unknown`。`idle` / `unknown` は死亡を意味しない (上記不変条件)。
- CLI: `mozyo-bridge otel activity [--window N] [--json]` / `otel events --limit N` (depth 実測・debug 用) / `otel status`。

## 段階2: bootstrap 注入と inventory join (#11675 / #11676 / #11677)

- **env 注入の正本は session bootstrap** (`mozyo` / `mozyo-bridge init` が agent window を作る経路)。`new_agent_session_window` / `new_agent_window` は agent command を `env OTEL_... <agent>` で wrap して起動する。注入内容: OTLP endpoint (`http://127.0.0.1:4318`) / `http/json` / logs+metrics exporter / `CLAUDE_CODE_ENABLE_TELEMETRY=1` / `OTEL_RESOURCE_ATTRIBUTES`。`OTEL_LOG_USER_PROMPTS` は**決して設定しない** (prompt OFF 固定)。
- **join key は `OTEL_RESOURCE_ATTRIBUTES` の `mozyo.session` / `mozyo.agent` / `mozyo.workspace_id`** (実測で CLI payload に pid/cwd が無いため)。値はすべて tmux-safe ASCII identifier で、path・自由文・個人情報を含めない。
- **join 規則** (`session_inventory.attach_activity`): OTel source の (mozyo.session, mozyo.agent) を pane の view sessions × agent_kind に照合。1-agent-1-window model で一意。同一 pair を複数 pane が持つ場合 (duplicate window) は誠実に attribution 不能として全員 `unknown`。store missing / receiver down / 未注入も `unknown` → tmux 生死層へ縮退。activity は query 時に store から計算し、**inventory cache には保存しない** (cache 独立性)。
- **出力互換**: `session list --json` の pane に `activity {state, last_event_at, last_event_name?, source}` を**追加** (既存 key 不変)。text は `ACTIVITY` 列を KIND の後に挿入。
- **doctor** (`otel` section): receiver /healthz 疎通 (down は error でなく "lost by design" と説明し tmux 縮退へ誘導)、store 状態、**観測漏れ検出** — telemetry を一度も発していない agent pane を `unobserved_agents` として列挙 (env 未注入 / 注入前起動 / CLI 未対応)。Redmine へ runtime heartbeat は書かない。
- 既存 startup flow 互換: env wrapper は process 連鎖 (`env` → agent) を変えるだけで、window 名 rail / preflight / `wait_for_agent_terminal_pane` の判定は不変。注入前に起動した既存 session は `unknown` のまま動き続け、`mozyo` / `init` での再起動で注入される。

## launchd 常駐 (段階横断: #11690)

- `mozyo-bridge otel launchd install / uninstall / status / restart` (macOS のみ)。最小管理面であり汎用 daemon manager にしない: start/stop は `RunAtLoad` + `KeepAlive` + `bootout` が担う。実装は `src/mozyo_bridge/application/otel_launchd.py`。
- plist: `~/Library/LaunchAgents/biz.asile.mozyo-bridge.otel.plist`。**EnvironmentVariables block を一切持たない** (secret が plist に乗る経路を構造的に排除 — test で pin)。`--host` を渡さないため receiver の loopback 既定 bind が適用。log は `~/Library/Logs/mozyo-bridge/otel-receiver.log`。
- 帰結 (明示): launchd 配下の receiver は env を持たないため、cockpit の **Redmine layer は `unconfigured`** になる。安全な key 受け渡し (keychain 等) は follow-up 設計事項。他 2 層 (OTel activity / tmux) は影響なし。
- `launchctl` は構造化 argv のみ (bootstrap / bootout / kickstart / print)。uninstall は自 label の plist だけを削除。
- launchd 状態 (`launchd status`) は配線の有無のみを答え、receiver health は従来どおり `otel status` / doctor が正本 (重複させない)。
- **upgrade / restart runbook**:
  1. `pipx upgrade mozyo-bridge` (または再 install)
  2. `mozyo-bridge otel launchd restart` — kickstart -k で旧 process を kill し新 binary で再起動。受け口停止中のイベントは best-effort lost (仕様)
  3. `mozyo-bridge otel status` で受け口疎通 / store を確認
  - plist の ProgramArguments は install 時の `which mozyo-bridge` 絶対 path を焼くため、**実行 path が変わる upgrade (pipx 再作成等) の後は `launchd install` を再実行**してから restart する
- 非常駐時の縮退: agent 不在 = receiver 停止と同じ (events lost by design / activity unknown / tmux 縮退)。既存 CLI / inventory / cockpit の動作は段階 1–3 の test で pin 済み。

## CLI ごとの event depth (実測方針)

各 CLI の OTel イベントが「入力待ち判定」に足る粒度かは実装初期に実測し、結果を Redmine journal に記録する (#11674)。深度不足の CLI は tmux 沈黙検知 (段階2 以降) へ縮退する。実測は `otel events --json` で受信イベント名の種類と頻度を観察する。

## 検証

- unit tests: `tests/test_otel_store.py` (decode 3 signal / allowlist + deny / body 非保存 / store round-trip / prune / corrupt degrade / activity 判定 / receiver e2e + healthz + gzip + 415 / CLI JSON)
- `python3 -m unittest discover -s tests`
- 受け入れ検証は #11674。
