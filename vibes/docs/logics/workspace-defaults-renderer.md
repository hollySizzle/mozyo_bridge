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
  - target: .mozyo-bridge/redmine-defaults.md   # 必須 (repo-relative)
  # 追加 output を生やす場合はここに重ねる
```

### バリデーション規則

- `schema_version` は固定 `1`。将来の breaking change は schema bump を経て移行する。
- `outputs[].target` は repo-relative。`..` を含む path や absolute path は invalid。
- 同一 target を 2 度宣言できない。
- URL は `http://` または `https://` のみ。`file://` や JavaScript URL は invalid。
- `verification.verified: true` でも `verification_date` / `verified_by` のいずれかが空文字なら **unverified 扱い** で render する。「verified と書いてあるが date 空欄」は agent が事実として扱えないため。
- credential-shape key (`api_key`、`access_token`、`refresh_token`、`client_secret`、`password`、`cookie`、`bearer_token`、`session_cookie`、`auth_token` 等) を含む YAML は die。
- 値が credential 代入形 (`API_KEY=...` / `REDMINE_TOKEN=...` 等) でも die。

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

新規 output (例: project-local doc snippet、`.mcd.json` の verified-only 生成、Codex 向け JSON config) を追加する手順。

1. 本 logic doc の Schema / Rendered Output Shape / `.mcd.json` Deferral 等の関係節を update する。
2. `workspace_defaults.py` に新 output の render 関数と `collect_render_results` への wiring を追加する。
3. test を追加し、output が byte-equal で再現できることと、drift gate が機能することを pin する。
4. 既存 workspace の YAML を更新するときは renderer が graceful migration できる (`outputs: [...]` の追加だけで足りる) ように schema を保つ。breaking change は schema_version bump を経る。
5. catalog の fc-workspace-defaults エントリに新 path を追加する。

`outputs` を YAML 側で簡単に増やせる shape にすることで、output 追加は config-driven にとどまり、renderer code 改修なしで済む slice もある (target target を増やすだけで shared body を再利用する場合)。本 PR の v1 では `redmine-defaults.md` 1 output だが、複数 output を YAML から指定する余地は schema に残してある。
