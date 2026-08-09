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
    path: "<repo>/.mozyo-bridge/workspace-anchor.json"
    legacy_path: "<repo>/.mozyo-bridge/workspace.json"
    role: 最小復元 record。registry 喪失時に同一 identity を再登録する種
  path_derivation:
    impl: domain/session_naming.derive_session_name (Redmine #10796)
    role: 初回登録時の名前決定と、未登録 workspace の fallback
    bounded_variant: domain/session_naming.derive_session_name_without_defaults
      は path-hash のみで導出し、workspace-local defaults を一切読まない。hot
      discovery path 用 (Redmine #12038、後述「未登録 fallback の degrading mode」)。
解決順序: home_registry -> workspace_anchor -> path_derivation
```

Naming note: anchor は実態として workspace identity recovery anchor である。
Redmine #11920 / #11921 で primary 名を `workspace-anchor.json` に rename 済み。
旧名 `workspace.json` は read-only fallback として残し、新規 write は新名のみ。
両名が存在する状態は mutating command で fail closed / doctor red になる。rename
方針と runtime contract の正本は `workspace-anchor-project-defaults-migration.md`。

設計上の不変条件:

- **terminal runtime state を DB に置かない。** live agent / process 情報は選択中providerのlive inventoryが正本であり、Herdr rolloutではglobal `agent list`、tmux backendではtmux観測を使う。registry が持つ runtime 隣接 field は `last_seen` のみで、identity table (`workspaces`) から分離した cache table (`workspace_activity`) に置く。cache table を失っても identity は壊れない。
- **通常のidentity登録は `register_workspace()`、missing-path rowの退役は専用 `workspace retire` adapterだけが書く。** `resolve_canonical_session()` (および `session name` / bare `mozyo` / `status` / smart `init` の session 解決ステップ) は registry を作らず、`last_seen` も更新せず、anchor にも書かない。登録側の呼び出し元は (1) 明示的な `workspace register` CLI (手動・idempotent) と (2) smart `init` (#11427) の guarded adoption (fail-closed preflight の後・terminal/vscode mutation の前に、未登録 workspace を登録) の 2つ。退役側はcurrent identity / path / exact record+`updated_at` / global Herdr inventory / plan digestをfenceし、verified independent backup後にexact rowだけをtransactional deleteする。一般delete APIやraw SQLite writeは公開しない。詳細は [[logic-managed-state-model]] の writer rulesを正本とする。
- **anchor は path を持たない。** anchor の置き場所そのものが path であり、copy / move されても stale path を主張できない。
- **anchor は workspace root marker である。** `shared/paths.py` の `WORKSPACE_MARKERS` に `.mozyo-bridge/workspace-anchor.json` (新名) と `.mozyo-bridge/workspace.json` (旧名 fallback) を含め、登録済み非 git workspace の subdirectory からの root 推測が登録 root に解決されるようにする (review #54760, rename #11920 / #11921)。`.mozyo-bridge/scaffold.json` (#11301) と同じ「workspace identity を確立する narrow marker」の扱い。
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

- 既存 registry の `user_version` が未知の値なら write 側は拒否する (silent migration しない)。`workspace retire` はbackup作成時と `BEGIN IMMEDIATE` 後の双方でexact schemaを再検証し、action-time driftではzero-delete。corrupt な registry も write 側は拒否し、復旧 (退避して anchor から再登録) を operator 判断に残す。read 側 (通常解決) は corrupt registry を空扱いし、anchor / derivation へ degrade するが、退役authority readは空へdegradeしない。

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
- `mozyo-bridge workspace retire [--repo PATH] --workspace-id ID [--execute --expect-plan-digest SHA256] [--json]` — dry-run既定。missing path、current workspaceでないこと、exact row digest/`updated_at`、lossless global Herdr inventoryでtarget agent 0をplanへ拘束する。executeは同じauthorityをaction-time再読し、live registryとinodeを共有しないprivate backup (single-link regular file / mode 0600) を検証後、rowとactivity cascadeのreadbackを同一write transaction内で証明する。replayもrowだけでなくorphan activity不在を要求する。rollback/recovery境界は [[logic-managed-state-model]] を読む。
- read-only consumer (`session name`, bare `mozyo`, `status`, `session vscode-settings`) は `resolve_canonical_session()` 経由で、書き込みを伴わない。
- smart `init` (#11427) は解決自体は `resolve_canonical_session()` 経由 (read-only) だが、未登録 workspace のときは guarded adoption の一部として `register_workspace()` を呼んで登録する (`workspace register` と同じ登録関数)。これは `workspace register` 以外の唯一の**登録**呼び出し元であり、専用退役writerとは別である。
- 未登録 workspace の解決は、既定 (`derive_unregistered=True`) では従来の導出と byte 一致で後方互換。

## 未登録 fallback の degrading mode (Redmine #12038)

`resolve_canonical_session(repo_root, *, derive_unregistered=True)` は registry / anchor のどちらも無い未登録 workspace のときだけ path_derivation へ落ちる。既定はフル `derive_session_name` (workspace-local `project-defaults.yaml` / 旧 `workspace-defaults.yaml` の Redmine identifier を読む) で、後方互換のため byte 一致。

`derive_unregistered=False` を渡すと、未登録 branch は `derive_session_name_without_defaults` (path-hash のみ) へ degrade し、workspace-local defaults を**一切読まない**。これは read-only な discovery hot path 用の安全弁である:

- `agents targets` / attention projection (`application/commands._agents_target_candidates`) は無関係 workspace の pane も列挙する。その workspace の defaults file が CloudStorage の dataless placeholder だと、`read()` が hydration 待ちで無限 block し、listing 全体が固まる (#12038 の再現)。
- registered workspace は home_registry / anchor で解決され derivation に到達しないため、この flag の影響を受けない。degrade するのは「無関係かつ未登録」の workspace の表示用 session 名だけで、path-hash fallback (`mozyo-<slug>-<hash>`) に落ちる。
- workspace_id は元々未登録なら `None` で、flag の有無で変わらない。target identity gate / same-lane narrowing / coordinator pseudo-target / cross-project Codex gateway は pane_id / workspace_id を見るため、表示名の degrade では弱まらない。

canonical session / defaults 解決そのものが目的の command (`session name`、smart `init` の adoption など) は既定 (`derive_unregistered=True`) のまま、フル derivation を使う。`session_inventory.collect_runtime_inventory(..., derive_unregistered=False)` も同じ degrading 契約 (lightweight inventory、#12032) を共有する。

## Nested workspace の launch routing (Redmine #15190)

同一 Git repository 内に canonical repo root workspace と nested application root
workspace の両 anchor が実在しうる (観測事例: repo root が default coordinator pair を
持ち、その配下 `Source/rails` が独自 scaffold/anchor を持つ)。通常の cwd 解決は
Git-root-first (#13641) なので `cd <nested>` は repo root を adopt する。穴は**明示 root**
側にある: `resolve_repo_root()` は `--repo` / `MOZYO_REPO` を canonicalization の**前**に
short-circuit するため、nested anchor が独立 workspace として解決し、1 repository に
2 組目の default Codex/Claude pair を `planned` にできる。

`workspace retire` は missing-path registry row 専用で、実在する nested path には使えない。
そこで **workspace-local な宣言 file** を rail として追加する。

```yaml
declaration:
  path: "<nested-workspace>/.mozyo-bridge/workspace-alias.json"
  schema_version: 1
  modes:
    alias:    canonical parent workspace へ解決する
    disabled: fixed typed reason で zero-launch する
  writer: "mozyo-bridge workspace alias set / disable / clear"
  reader: "mozyo-bridge workspace alias show" と launch chokepoint
```

- **格納先が workspace-local である理由**: registry 喪失・復旧を跨いで宣言が生き残る必要が
  ある。`registry.sqlite` の row は、その宣言が耐えるべき復旧手順そのもので消える。identity
  store への schema 追加も不要になる。
- **anchor と分離する理由**: anchor は identity recovery record (#11429) であり意味を 1 つに
  保つ。本 file は launch authority routing の宣言で、identity provenance に触れずに
  追加・削除できる。
- **chokepoint は 2 箇所**であり、これは意図的な重複である (#15190 / review j#102107 F4):
  - `herdr_session_start.prepare_session` の最初 — public entry。request validation・
    home lock・binary 解決・capability probe より**前**に評価するので、拒否は zero-launch
    かつ zero-side-effect になる。
  - `herdr_session_start._prepare_session_locked` の最初 — **実際に全 launch が到達する
    境界**。v1 replacement driver (`sublane_actuator_herdr_ops`) は
    `prepare_actuator_lane_session(admission_lock_held=True)` 経由で public wrapper を
    通さず本 entry を直接呼ぶため、public entry だけに rail を置くと live replacement 経路が
    素通りする。
  再適用は idempotent (canonical root は宣言を持たないので、既に畳まれた root は自分自身へ
  解決する)。後続の読者が「重複」と見なして 2 つ目を削除すると bypass が復活するため、
  docstring と本節の双方に理由を残す。`--dry-run` と live action-time は同一 rail。
- **`resolve_repo_root()` を書き換えない理由**: 同 resolver は約 57 箇所から呼ばれ、release
  tooling・doctor・discovery・config load を含む。nested root を *それ自体として* 報告するのが
  仕事の read-only surface (`workspace inspect` / `docs resolve` / `scaffold status`) まで
  巻き込むと、nested tree を code/docs 作業 root として使えるという受入条件を壊す。#15190 は
  default coordinator pair の *launch* が repository 単位で重複しないことだけを要求する。
- **fail-closed 条件** (いずれも typed reason 付き zero-launch。nested root への degrade は
  しない — その degrade が除去対象の欠陥そのもの):
  `alias_target_missing` / `alias_target_not_directory` / `alias_target_is_self` /
  `alias_target_not_ancestor` / `alias_target_identity_unresolved` /
  `alias_target_identity_mismatch` / `alias_target_cross_repository` /
  `alias_target_declares_alias` / `declaration_unreadable` / `declaration_invalid` /
  `declaration_unsupported_schema` / `declaration_not_regular_file`。
- **未知 field は拒否する** (`declaration_invalid`)。schema v1 が mode ごとに定義する key
  集合は exact であり、余剰 key を無視して部分解釈しない。将来の schema 拡張は
  `schema_version` の bump で表現する。無版の余剰 key を旧 reader が黙って落とすと、
  「起動を gate する」という宣言の目的そのものが部分適用になる (review j#102104 F3)。

### 宣言 file の filesystem 安全性 (review j#102104 F1 / F2)

宣言 file は repository が内容を支配する path にあるため、writer / reader は path 解決を
follow-through helper に任せない。`.mozyo-bridge` を `O_NOFOLLOW` で開いた **directory file
descriptor** に固定し、判定は全て `lstat` で行う。

- **書き込みは symlink を辿らない**。`Path.write_text` は symlink を辿るため、
  workspace 外を指す `workspace-alias.json` symlink があると `workspace alias disable` が
  workspace 外の file を上書きできた。現在は「既存 entry が通常 file でない」「hard link が
  複数」「親が実 directory でない (symlink 含む)」をいずれも zero-mutation で拒否する
  (`declaration_not_regular_file` / `declaration_multiple_links` /
  `declaration_parent_unsafe`)。
- **書き込みは同一 directory 内の private temp file → `os.replace` → readback 照合**で行う。
  途中のどの失敗でも既存宣言は変化しない (`declaration_write_failed` /
  `declaration_readback_failed`)。
- **「不在」と「存在するが通常 file でない」を分離する**。`is_file()` は directory / FIFO /
  dangling symlink でも false になるため、破損・すり替えられた宣言が「宣言なし」と読まれ
  nested root の起動を許していた。現在は不在のみ `no_declaration`、非通常 entry・stat/read
  失敗は typed zero-launch。
- **`clear` は「存在するが削除不能」を成功と報告しない** (`declaration_remove_failed`)。
  symlink の `clear` は link だけを外し、その target には触れない。
- alias cycle 判定は「読める宣言」ではなく **entry の存在**で行うので、target 側の宣言が
  壊れている場合も fail closed する。
- **cross-repository 判定は `git_common_dir` の一致**で行う。linked worktree は main checkout と
  共通なので同一 repository (sublane worktree は従来どおり launch 可能)、**submodule** は
  親の tree 内に物理的に存在しても別 repository として拒否する。観測事例の
  `projects/nihonidenshi` が submodule であるため、path 包含だけでは不十分。
  両者とも非 git の場合のみ包含関係が binding を担う (#11301 の非 git scaffolded workspace)。
- **identity binding**: 宣言は canonical の `workspace_id` を記録する。同一 path で identity が
  再発行・復元された場合は `alias_target_identity_mismatch` で fail closed し、alias が別
  workspace へ黙って向き直ることを防ぐ。
- alias chain は 1 hop に固定 (`alias_target_declares_alias`) するので cycle は成立しない。
- canonical 側の role binding / workspace id / live attestation は本 rail では一切変更しない。
  `clear` は宣言 file だけを削除し、anchor・registry row・nested の tracked scaffold /
  catalog / skills には触れない。

## 検証

- registry tests: `tests/integration/e_110_execution_platform/f_110_workspace_session_identity/test_workspace_registry.py` と `test_workspace_retirement_store.py`、`tests/unit/e_110_execution_platform/f_110_workspace_session_identity/test_workspace_retirement.py`。
- `python3 -m unittest discover -s tests`
- `mozyo-bridge docs validate --repo .` ほか catalog 検証一式 (catalog 変更時)。
