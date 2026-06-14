# Workspace Registry (home-registry-first identity)

Redmine #11425 / #11429。workspace identity を path からの毎回導出 (workspace-local first) から、home registry を正本とする登録モデル (home-registry first) へ移行するための設計正本。実装は `src/mozyo_bridge/workspace_registry.py`。

## 目的

- 複数 workspace / 非 git workspace / dev container 環境で一貫した workspace identity を扱う。
- session name を毎回 path から再生成せず、初回登録された canonical identity を優先する。導出入力 (workspace-defaults の identifier、path 自体) が後から変わっても session 名が動かない。
- home registry が消えても workspace-local anchor から同一 identity を復元できる。

## 正本と層

```yaml
identity_layers:
  home_registry:
    path: "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/registry.sqlite"
    role: workspace identity の正本 (workspace id / canonical path / display path / readable name / canonical session / preset version)
  workspace_anchor:
    path: "<repo>/.mozyo-bridge/workspace.json"
    role: 最小復元 record。registry 喪失時に同一 identity を再登録する種
  path_derivation:
    impl: domain/session_naming.derive_session_name (Redmine #10796)
    role: 初回登録時の名前決定と、未登録 workspace の fallback
解決順序: home_registry -> workspace_anchor -> path_derivation
```

Naming note: `workspace.json` は実態として workspace identity recovery anchor である。
rename 方針は `workspace-anchor-project-defaults-migration.md` を正本とする。runtime
実装が入るまでは現行 path を維持し、docs-only decision を code の事実として
先取りしない。

設計上の不変条件:

- **tmux runtime state を DB に置かない。** live な window / pane / process 情報は tmux が正本。registry が持つ runtime 隣接 field は `last_seen` のみで、identity table (`workspaces`) から分離した cache table (`workspace_activity`) に置く。cache table を失っても identity は壊れない。
- **読み取りは read-only / 書き込みは `register_workspace()` 経由。** `resolve_canonical_session()` (および `session name` / bare `mozyo` / `status` / smart `init` の session 解決ステップ) は registry を作らず、`last_seen` も更新せず、anchor にも書かない。registry / anchor への書き込みは `register_workspace()` のみが行い、呼び出し元は (1) 明示的な `workspace register` CLI (手動・idempotent) と (2) smart `init` (#11427) の guarded adoption (fail-closed preflight の後・tmux/vscode mutation の前に、未登録 workspace を登録) の 2 つ。`init` の session 解決自体は read-only で、登録は別の明示的 write step。
- **anchor は path を持たない。** anchor の置き場所そのものが path であり、copy / move されても stale path を主張できない。
- **anchor は workspace root marker である。** `shared/paths.py` の `WORKSPACE_MARKERS` に `.mozyo-bridge/workspace.json` を含め、登録済み非 git workspace の subdirectory からの root 推測が登録 root に解決されるようにする (review #54760)。`.mozyo-bridge/scaffold.json` (#11301) と同じ「workspace identity を確立する narrow marker」の扱い。
- **特定 VS Code extension / tmux-integrated を公式 backend にしない。** 既存の `.vscode/settings.json` 連携 (#10796) は維持するが、registry の正本性はそれに依存しない。

## SQLite schema (registry v1)

```sql
PRAGMA user_version = 1;
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,      -- uuid4 hex。anchor が運ぶ durable id
    canonical_path TEXT NOT NULL UNIQUE,
    display_path TEXT NOT NULL,         -- $HOME を ~ に縮めた表示用
    project_name TEXT NOT NULL,         -- readable name (非 ASCII 可)
    canonical_session TEXT NOT NULL,    -- 初回登録時に derive、以後不変
    preset TEXT,                        -- .mozyo-bridge/scaffold.json から best-effort
    preset_version TEXT,
    created_at TEXT NOT NULL,           -- UTC ISO8601
    updated_at TEXT NOT NULL
);
CREATE TABLE workspace_activity (       -- cache。identity と分離
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    last_seen TEXT NOT NULL
);
```

- 既存 registry の `user_version` が未知の値なら write 側は die する (silent migration しない)。corrupt な registry も write 側は die し、復旧 (退避して anchor から再登録) を operator 判断に残す。read 側 (解決) は corrupt registry を空扱いし、anchor / derivation へ degrade する。

## Anchor schema (v1)

```json
{
  "schema_version": 1,
  "workspace_id": "<uuid4 hex>",
  "canonical_session": "mozyo-...",
  "project_name": "...",
  "created_at": "...",
  "updated_at": "..."
}
```

- 構造不正・schema 不一致・tmux-unsafe な session 名 (`[A-Za-z0-9][A-Za-z0-9_-]*` 以外) は anchor 全体を無効として無視する。解決は derivation へ落ち、次の `workspace register` が正しい anchor を書き直す。

## 登録 (`workspace register`) の identity 優先順位

1. **anchor が存在する** — anchor の workspace id / canonical session を正本として registry へ upsert する。registry に同 id の row があれば update (path が変わっていれば move として canonical path を更新)、無ければ restore。
2. **anchor が無く registry row (path 一致) がある** — row の identity を維持し、anchor を書き直す。
3. **どちらも無い** — 新規 identity を mint する。workspace id は uuid4、canonical session はこの一点でのみ `derive_session_name` から確定する。

同一 path を別 workspace id の stale row が占めている場合 (backup 復元等)、anchor 側 identity が勝ち、stale row は削除して note に残す。

## CLI surface

- `mozyo-bridge workspace register [--repo PATH] [--name NAME] [--json]` — 明示的・手動の書き込み。idempotent。registry / anchor への書き込み関数 `register_workspace()` を呼ぶ。
- `mozyo-bridge workspace list [--json]` — read-only。
- `mozyo-bridge workspace inspect [--repo PATH] [--json]` — registry row / anchor / derived fallback / 効いている解決を並べて表示。drift の可視化用。
- read-only consumer (`session name`, bare `mozyo`, `status`, `session vscode-settings`) は `resolve_canonical_session()` 経由で、書き込みを伴わない。
- smart `init` (#11427) は解決自体は `resolve_canonical_session()` 経由 (read-only) だが、未登録 workspace のときは guarded adoption の一部として `register_workspace()` を呼んで登録する (`workspace register` と同じ write 関数)。これは `workspace register` 以外の唯一の write 呼び出し元。
- 未登録 workspace の解決は従来の導出と byte 一致で後方互換。

## 検証

- unit tests: `tests/test_workspace_registry.py` (登録 / 再利用 / anchor 復元 / 移動 / 日本語 path / 長 path / 非 git / corrupt degrade / JSON schema)。
- `python3 -m unittest discover -s tests`
- `mozyo-bridge docs validate --repo .` ほか catalog 検証一式 (catalog 変更時)。
