# Session Inventory (runtime-first, SQLite cache)

Redmine #11421 / #11422。workspace 横断で起動中の mozyo session / agent pane を operator・外部 UI が安全に発見するための inventory の設計正本。実装は `src/mozyo_bridge/session_inventory.py`、CLI は `mozyo-bridge session list [--json]`。

> 正本境界 (registry=identity / live tmux=liveness・handoff / inventory=projection / event log=desired-state) の集約は `managed-state-model.md` (#11695)。本 doc の inventory は projection 層であり単独正本ではない。

## 目的

- 複数 workspace で起動している mozyo session と作業 path を一覧化する。
- 外部 UI / 将来の連携機能が機械可読 (JSON) に既存 session を発見できるようにする。
- 特定 VS Code extension / tmux-integrated の公式 backend にはしない。一般的 inventory として提供する。

## 正本と層

```yaml
inventory_layers:
  tmux_runtime:
    impl: infrastructure/tmux_client.try_pane_lines
    role: session / window / pane / process / cwd の正本。毎回の listing で再取得する
  workspace_identity:
    impl: workspace_registry (registry -> anchor -> derivation)
    role: pane の repo root を workspace identity (workspace_id / canonical session / 名前) に解決する
  sqlite_cache:
    path: "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite"
    role: durable cache / index。tmux 不在時の degraded 表示にのみ使い、stale を明示する
解決順序: tmux_runtime (+ workspace_identity) -> sqlite_cache(stale)
```

設計上の不変条件:

- **SQLite cache を正本にしない。** runtime listing が成功するたび snapshot を全置換する。cache は読めなくても・消えても inventory 機能は失われない (次の runtime listing が再構築する)。stale cache を返すときは `stale: true` と note を必ず付け、誤誘導 (issue #11422 記載 risk) を防ぐ。
- **registry.sqlite に runtime state を足さない。** workspace registry (#11425/#11429) の identity / cache 分離不変条件を維持するため、inventory cache は別 file `inventory.sqlite` に置く。
- **identity key は `pane_id`** (Redmine #11628, owner 合意 2026-06-11)。tmux session group では同一 pane が複数 session に所属する。session は pane の属性 (view) であり、複数所属は `views` 配列に畳み、1 pane = 1 行とする。正準 view は workspace の resolved canonical session と一致する session を優先し、無ければ session 名 sort 順の先頭で決定的に選ぶ。単一 tmux server 前提。複数 server 対応時は `(socket, pane_id)` を複合キーとする。
- **path 同一性は Unicode 正規化差を吸収する** (Redmine #11625)。registry の `canonical_path` と pane 由来の repo root は `shared/paths.py` の `normalize_path_unicode()` (NFD 固定) を通してから比較する。macOS readdir は NFD、文書 / agent 経由の path は NFC で、raw byte 比較は同一 workspace を取り逃す。session 名 hash 導出 (`domain/session_naming.py`) と handoff `--target-repo` gate も同じ helper を通る (#11625 で修正済み。NFD 固定の理由は helper docstring を正本とする)。
- **home 消失 fallback は identity 層が担う。** home registry / inventory cache が消えても、runtime listing は各 workspace の local anchor (`.mozyo-bridge/workspace.json`) または path derivation から同じ identity を再構築する。inventory 自体の復元手順は不要 (cache は regenerable)。

## SQLite schema (inventory v1)

```sql
PRAGMA user_version = 1;
CREATE TABLE panes (
    pane_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,          -- 正準 view の session
    window_index TEXT NOT NULL,
    window_name TEXT NOT NULL,
    pane_index TEXT NOT NULL,
    pane_active INTEGER NOT NULL,
    process TEXT NOT NULL,
    cwd TEXT NOT NULL,
    repo_root TEXT,
    agent_kind TEXT NOT NULL,       -- claude / codex / unknown (window 名 rail)
    workspace_id TEXT,              -- registry / anchor 由来のみ。derivation は NULL
    canonical_session TEXT,
    project_name TEXT,
    identity_source TEXT,           -- home-registry / workspace-anchor / derivation markers
    views_json TEXT NOT NULL        -- PaneView 配列 (canonical flag 含む)
);
CREATE TABLE inventory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- inventory_meta('collected_at') = snapshot UTC ISO8601
```

- cache は regenerable なので、registry と違い corrupt file は write 側が退避 (削除) して再作成する。ただし **未知の (新しい) schema version は壊さず write を skip** し note を返す。read 側は missing / corrupt / version 不一致をすべて「cache なし」として degrade する。
- snapshot は全置換 (DELETE + INSERT, 単一 transaction)。部分更新はしない。

## CLI surface

- `mozyo-bridge session list [--json]` — 既存 `session` サブコマンド体系 (issue #11422 指定) に追加。runtime 収集成功時は cache を更新して `source: runtime` / `stale: false`。tmux 不在時は cache から `source: cache` / `stale: true` (text 出力では stderr に stale 警告)。cache も無ければ空の stale snapshot を返す (exit 0)。
- JSON payload: `schema_version` / `collected_at` / `source` / `stale` / `inventory_path` / `notes` / `panes[]`。pane は `pane_id` / 正準 view の `session`・`window_*`・`pane_*` / `process` / `cwd` / `repo_root` / `agent_kind` / `workspace{workspace_id, canonical_session, project_name, source}` / `views[]` / `activity{state, last_event_at, source}` (#11675 追加。OTel store からの query 時 join で、inventory cache には保存しない。詳細は `otel-event-store.md` 段階2)。
- `agents list` (#10332) も #11628 で同じ identity model に統一済み: 1 行 = 1 `pane_id`、grouped 所属は `views` 配列、text 出力は末尾 `OTHER_VIEWS` 列。**folding の実装は `domain/agent_discovery.fold_agents_by_pane()` を両 surface で共有**し、`session list` は workspace identity 層 (registry → anchor → derivation) をその上に重ねる。`agents list --json` の既存 field は正準 view の値として意味を維持し、`views` が追加 (grouped pane の重複行は廃止 = #11628 が修正した bug)。`--session` filter は正準 / grouped どちらの所属でも match する。

## 検証

- unit tests: `tests/test_session_inventory.py` (runtime 収集 / grouped folding / NFC-NFD 吸収 / registry・anchor・derivation 解決 / cache 初回作成・再利用・corrupt 再作成・新 version skip / tmux 不在 degrade / JSON schema)。
- `python3 -m unittest discover -s tests`
- 受け入れ検証 (初回作成 / 再利用 / 復元 / JSON schema) は #11423 が担当する。
