# Workspace Defaults Renderer

## Purpose

Redmine default project の解決規約は Codex / Claude / local docs / verification すべてが必要とする。手作業で複数 file に書くと drift し、しかも project 固有値 (識別子、URL、parent label) を distributed mozyo_bridge code に直書きすると、別 workspace へ配布された時に事故る。

本 logic は次を実現する。

1. **正本は workspace-local YAML 1 枚** (`<workspace>/.mozyo-bridge/workspace-defaults.yaml`)。distributed code は contract と renderer だけを持つ。
2. **生成出力は workspace-local generated artifact** (default: `.mozyo-bridge/redmine-defaults.md`)。Codex / Claude session 入口から reference される。
3. **`--check` で drift gate**。`mozyo-bridge workspace-defaults --check` が CI / pre-commit / release で機械的に走る。
4. **secret rejection by construction**。credential-shape key / value は load 時に die する。renderer 経由で secret が出力されることはない。

`mozyo-bridge scaffold canonical` (Redmine #10345 / #10426) や `sync_plugin_skill.sh` (Redmine #10663) と同じ思想だが、対象が **distributed** ではなく **per-workspace** である点が異なる。

## Source-of-Truth Layering

```
distributed (mozyo_bridge package)
├── src/mozyo_bridge/workspace_defaults.py       # renderer code (contract)
├── vibes/docs/logics/workspace-defaults-renderer.md  # 本 logic (contract)
└── (no project-specific values)
                ▲
                │ load_workspace_defaults / render
                │
workspace-local (target workspace の git repo に commit する)
├── .mozyo-bridge/workspace-defaults.yaml        # 正本 (project-specific values)
└── .mozyo-bridge/redmine-defaults.md            # generated (do not hand-edit)
```

distributed code には特定 project の identifier を書かない。customer / project 固有値 (Redmine slug、project name、URL の末尾 etc.) は workspace-local YAML、もしくは test fixture (`WorkspaceDefaultsRendererTest`) のみで使う。distributed 側 (`src/**`、`skills/**`、`plugins/**`、`vibes/docs/**`、`.mozyo-bridge/redmine-defaults.md`) に acceptance fixture の identifier が流出していないことを `test_distributed_source_does_not_carry_cloud_drive_identifier` が pin する。

## Schema (`.mozyo-bridge/workspace-defaults.yaml`)

```yaml
schema_version: 1

redmine:
  default_project:
    identifier: <redmine slug>           # 必須
    name: <human-readable name>          # 必須
    url: https://.../projects/<slug>     # 必須 (http(s) のみ)
    parent_label: <display only>         # 任意

  verification:
    verified: true | false               # 必須
    verification_date: "YYYY-MM-DD"      # 必須 (verified=true でも文字列必須)
    verified_by: <handle / actor>        # 必須

outputs:
  - kind: redmine_markdown                       # 必須 (renderer kind)
    target: .mozyo-bridge/redmine-defaults.md    # 必須 (repo-relative)
  # 追加 output kind を生やす場合は code 改修が必要。詳しくは Extending To New Outputs 節。
```

### バリデーション規則

- `schema_version` は固定 `1`。将来の breaking change は schema bump を経て移行する。
- `outputs[].kind` は必須かつ `KNOWN_OUTPUT_KINDS` のいずれか。現状は `redmine_markdown` のみ。未知の kind は load 時に die し、stderr に supported set を列挙する。これは Codex review #50989 が捕捉した「`.codex/config.toml` を target に書くと Markdown が `.toml` に書き出される」footgun を schema 層で塞ぐためのもの。
- `outputs[].target` は repo-relative。`..` を含む path や absolute path は invalid。
- `outputs[].target` の **suffix は kind が許可する set のいずれか** でなければならない (`KIND_ALLOWED_SUFFIXES`)。例: `redmine_markdown` は `.md` または `.markdown` のみ。`kind: redmine_markdown` + `target: .codex/config.toml` のような組み合わせは load 時に die する。Codex correction review #50995 が捕捉した「typed kind だけでは `.toml` / `.json` target に Markdown が書き出される」残存 footgun を schema 層で塞ぐためのもの。非 Markdown 出力を必要とする場合は新 kind を追加する (Extending To New Outputs 節を読む)。
- 同一 target を 2 度宣言できない (kind が違っても重複扱い)。
- URL は `http://` または `https://` のみ。`file://` や JavaScript URL は invalid。
- `verification.verified: true` でも `verification_date` / `verified_by` のいずれかが空文字なら **unverified 扱い** で render する。「verified と書いてあるが date 空欄」は agent が事実として扱えないため。
- credential-shape key (`api_key`、`access_token`、`refresh_token`、`client_secret`、`password`、`cookie`、`bearer_token`、`session_cookie`、`auth_token` 等) を含む YAML は die。
- 値が credential 代入形 (例: `API_KEY=<value>` / `REDMINE_TOKEN=<value>` のような `<NAME>=<value>` シェイプ) でも die。プレースホルダの `<value>` は説明用で、release-flow の Source Tree Hygiene gate が検知する `<KEY>=<実値>` 形式そのものを committed docs に貼らないための表記。

### 現状の supported output kinds

| kind | renderer | output 形式 | allowed target suffixes |
| --- | --- | --- | --- |
| `redmine_markdown` | `render_redmine_defaults_markdown` | Markdown snippet (Codex / Claude 両用)。Resolution Priority / Verification / Constraints セクションを含む。| `.md`, `.markdown` |

`KNOWN_OUTPUT_KINDS` と `KIND_ALLOWED_SUFFIXES` は `src/mozyo_bridge/workspace_defaults.py` で定義される。`tests/test_mozyo_bridge.py::WorkspaceDefaultsRendererTest::test_supported_kinds_list_is_pinned` と `test_kind_allowed_suffixes_table_is_pinned` がそれぞれ verbatim で pin する。set / suffix を増やすときは両 test、本表、`_render_for_kind` dispatch を **同一 commit で同期** する。

target suffix の判定は lowercase。`.MD` 等は `.md` と同一視される。kind が allowed suffix を持たない (= 空 set) ことを意味する宣言は禁止 — 新 kind を追加する場合は必ず allowed suffix を 1 つ以上指定する。

## Rendered Output Shape

`render_redmine_defaults_markdown` は次の section を持つ Markdown を出す。

- **Default Project** — identifier / name / url / parent_label を箇条書き。`verified=false` または verification が不完全なら header に `(UNVERIFIED)` suffix が付く。
- **Resolution Priority** —
  1. **Explicit project id wins**: user / ticket / MCP / session が project_id を名指したら、default にフォールバックしない。
  2. **Verified default**: 明示が無く verification が完全なら default を使う。issue 作成前に MCP / API 上で到達確認する。
  3. **Resolution failure**: verified default が reject されたら silent retry せず、operator に escalate する。
- verification が不完全な場合、(2) は **NOT yet verified** メッセージに置き換わり、Verification section で手順を案内する。
- **Verification** — verified flag、date、actor、agent restart / MCP reload 後の再確認指示。
- **Constraints** — generated file の hand-edit 禁止、secret 禁止、distributed mozyo_bridge には固有値を持たせない方針。

## CLI Surface

```bash
mozyo-bridge workspace-defaults [--repo <root>]            # 全 output を render
mozyo-bridge workspace-defaults --check [--repo <root>]    # drift check (exit 1 on drift)
```

- `--repo` 省略時は cwd。default で `<repo>/.mozyo-bridge/workspace-defaults.yaml` を読む。
- `--check` は generated output を rerender し on-disk と比較。差異があれば exit 1、stderr に出力 path + 復旧 command (`mozyo-bridge workspace-defaults` no `--check`, from the repo root) を verbatim で出す。#10345 / #10663 correction の precedent に従う。
- input YAML が無い場合は die し、schema doc (本 file) を参照する hint を出す。

## LLM Startup Placement Guidance

LLM startup の責務は、workspace-local default project の **存在確認、
配置先確認、検証** である。どの業務をどの Redmine project に置くか、project
が無い場合どの parent の下へ作るか、Epic / Feature / UserStory をどう切るかは
distributed `mozyo_bridge` 側で決めない。そこは target workspace / owner の
判断であり、agent は未設定なら推測せず operator に確認する。

Startup sequence:

1. `<repo>/.mozyo-bridge/redmine-defaults.md` があれば読む。verified default が
   ある場合でも、明示 `project_id` が user / ticket / MCP call にあればそれを
   優先する。
2. generated snippet が無い場合は `<repo>/.mozyo-bridge/workspace-defaults.yaml`
   の有無を確認する。YAML があれば `mozyo-bridge workspace-defaults` と
   `--check` で snippet を生成・検証する。
3. YAML も無い場合は、project identifier / display name / URL / optional parent
   label を operator に確認する。Redmine project を推測で作成しない。
4. credential / token / API key / cookie / password は workspace-local config、
   generated snippet、ticket journal、chat output に書かない。認証は user-level
   tool config または secret store に残す。
5. agent / MCP runtime を再起動または reload した後、Redmine MCP を `project_id`
   なしで呼び、default project が期待 identifier に解決されることを確認する。
   比較として明示 `project_id` 付きでも呼び、同じ project になることを確認する。

### `.codex/config.toml` example

Codex workspace が Redmine MCP を使う場合は、target repo の
`.codex/config.toml` に verified default project があるか確認する。存在しない、
または Redmine default が未設定なら operator に確認してから次のような TOML を置ける。
これは `workspace-defaults` renderer の出力ではなく、LLM startup / operator
向けの checkpoint / 配置例である。

```toml
[redmine]
default_project = "<project-identifier>"
default_project_name = "<project display name>"
default_project_url = "https://redmine.example.invalid/projects/<project-identifier>"

[mcp_servers.redmine_epic_grid]
url = "https://redmine.example.invalid/mcp/rpc"
http_headers = { X-Default-Project = "<project-identifier>" }
```

この file に API key、OAuth token、cookie、password、client secret を置かない。
authenticated Redmine MCP server の credential は user-level Codex config または
system-managed secret store に残す。agent は `.codex/config.toml` を作成・更新した
場合、Codex restart / reload 後に `project_id` なしの Redmine MCP call で default
解決を検証する。

`.codex/config.toml` を自動生成したい場合は、新しい output kind (例:
`codex_toml`) を追加する必要がある。既存 `redmine_markdown` kind で
`.codex/config.toml` を target にすることは禁止されており、suffix gate が reject
する。新 kind は TOML parse test、secret rejection、allowed suffix `.toml`、
byte-equal / drift tests を同一 commit で追加する。

## `.mcd.json` Deferral

acceptance criteria は次を要求する。

> `.mcd.json` is not generated as an authoritative runtime config unless verified to be read by the target Claude/MCP environment; otherwise it is only documented as an example or deferred.

現時点で mozyo_bridge repo は target Claude / MCP runtime が `.mcd.json` を読むことを **検証していない**。よって本 PR では `.mcd.json` の自動生成を **deferred** とし、本 renderer は emit しない。

将来 `.mcd.json` を generated output に追加する場合は以下を満たしてから行う。

- 対象 Claude / MCP runtime が当該 path から default project を実際に読み込む経路を docs (公式 or 検証済 issue) で立証する。
- workspace YAML に `outputs.mcp_config: enabled: true` を追加するための schema 拡張 (schema bump or 後方互換オプション) を本 file で定義する。
- secret rejection を含む同じ load-validate-render pipeline を通す。`.mcd.json` に server credentials を書かないことを assert する test を追加する。
- 検証手順を本 file に追記する (agent restart / MCP reload / 再現可能な確認 step)。

検証なしの段階で `.mcd.json` を emit すると、agent が「実は読まれていない config」を fact として扱う risk がある。本 renderer はそれを避ける設計を選ぶ。

## Tests

`tests/test_mozyo_bridge.py::WorkspaceDefaultsRendererTest` で次を pin する。

- **round-trip**: 本 workspace (`mozyo_bridge`) 自身の `.mozyo-bridge/workspace-defaults.yaml` が `.mozyo-bridge/redmine-defaults.md` と byte-equal に再現される。
- **CLI**: clean / drift / missing-output / 復旧 path。stderr に runnable recovery command が出る。
- **schema 違反**: missing input / required field 欠落 / 不正 schema_version / non-http url / `..` を含む output target。
- **secret rejection**: top-level credential key / nested credential key / 値の credential 代入形。
- **verified vs unverified rendering**: `(UNVERIFIED)` の出現、警告の有無、`verified: true` でも date 空欄なら unverified 扱い。
- **acceptance fixture**: acceptance criteria が指定した cloud-drive-management 系 fixture は test 内でのみ使用し、distributed source (`src/**`、`skills/**`、`plugins/**`、`vibes/docs/**`、本 workspace の generated artifact) に identifier が混入していない (`test_distributed_source_does_not_carry_cloud_drive_identifier`)。fixture の literal identifier は本 logic doc に書かず、test fixture 側 (`CLOUD_DRIVE_FIXTURE`) を正本とする。

## Integration With Other Surfaces

- **Skill workflow (`skills/mozyo-bridge-agent/references/workflow.md`)**: Ticket System Conventions の Redmine 系 entrypoint で、`<repo>/.mozyo-bridge/redmine-defaults.md` を読んで default project を解決する旨を 1 段落で言及する。distributed skill には project-specific identifier を書かない。
- **scaffold preset の `agent-workflow.md`**: 今回 workspace defaults renderer に新規 binding は追加しない。preset workflow は引き続き Redmine issue / journal を主軸とし、default project 解決は workspace-local snippet に委ねる。
- **release helper**: `mozyo-bridge release check drift` (#10688) は canonical + plugin mirror を bundling する。`workspace-defaults --check` は worktree (workspace-local) を対象とするため、release helper 経由ではなく **CI unittest 経由** で gating する (`WorkspaceDefaultsRendererTest::test_committed_repo_renders_byte_equal`)。drift があれば毎 push / PR で fail する。
- **docs catalog**: 新 logic doc (`logic-workspace-defaults-renderer`) と新 file pattern (`.mozyo-bridge/workspace-defaults.yaml` / `.mozyo-bridge/redmine-defaults.md`) を catalog に登録する。fc-governance-artifacts に追加する。

## Extending To New Outputs

新規 output kind (例: project-local doc snippet、`.mcd.json` の verified-only 生成、Codex 向け TOML config) を追加する手順。**output kind を増やすことは config change ではなく code change である**。schema は kind を明示要求し、未知の kind は load 時に die する。これは Codex review #50989 が捕捉した、generic な `target` から非 Markdown content target に Markdown が書き込まれる footgun を schema 層で塞ぐためのものである。

新 kind を追加する commit に含めるべきもの (5 点とも同 commit で同期):

1. **dispatch arm**: `src/mozyo_bridge/workspace_defaults.py` の `_render_for_kind` に新 kind の分岐と専用 render 関数 (例: `render_codex_toml(defaults) -> str`) を追加する。
2. **typed renderer の content 形式**: 生成する文字列は kind が含意する format (Markdown / TOML / JSON / etc.) として valid であること。test で format validation を pin する (例: TOML 出力は `tomllib.loads` でパース可能であることを assert)。
3. **set 拡張と test**: `KNOWN_OUTPUT_KINDS` に追加し、`test_supported_kinds_list_is_pinned` を新 set で更新する。新 kind に対する render 結果 byte-equal / drift / 不正 schema reject の各 test を足す。
4. **allowed-suffix 表**: `KIND_ALLOWED_SUFFIXES` に新 kind の許可 suffix set を 1 つ以上指定する。`test_kind_allowed_suffixes_table_is_pinned` を更新し、新 kind に対する mismatch reject / accept 双方の test を足す。
5. **本 logic doc**: Schema, Rendered Output Shape (kind 別の section), Supported Kinds 表 (renderer + allowed suffixes 列), Extending To New Outputs を更新する。`.mcd.json` Deferral の 4 条件 (runtime 立証 / schema 拡張 / secret reject 維持 / 検証手順) を満たす場合は同 commit で deferral を解除する。

config-driven な多重 target 追加 (= 既存 kind で別 path に書く) は YAML だけで足りる。例: 既に `redmine_markdown` kind を持ち、追加で `vibes/docs/temps/redmine-defaults.md` にも同じ Markdown snippet を写したい場合、`outputs:` リストに同 kind + 別 target を追加するだけで済む。新しい target の suffix は **既存 kind の allowed suffix set** に含まれていなければならず、`.codex/config.toml` のような非 Markdown path は schema 層で reject される。新しい format / config を要求する場合は新 kind を追加するルート (上記 5 点) を踏む。

catalog 関連: 新 path を `fc-governance-artifacts` か新規 fc エントリに追加し、generator を回して `file_conventions.generated.yaml` を再生成する。
