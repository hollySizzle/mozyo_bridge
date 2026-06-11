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

## CLI ごとの event depth (実測方針)

各 CLI の OTel イベントが「入力待ち判定」に足る粒度かは実装初期に実測し、結果を Redmine journal に記録する (#11674)。深度不足の CLI は tmux 沈黙検知 (段階2 以降) へ縮退する。実測は `otel events --json` で受信イベント名の種類と頻度を観察する。

## 検証

- unit tests: `tests/test_otel_store.py` (decode 3 signal / allowlist + deny / body 非保存 / store round-trip / prune / corrupt degrade / activity 判定 / receiver e2e + healthz + gzip + 415 / CLI JSON)
- `python3 -m unittest discover -s tests`
- 受け入れ検証は #11674。
