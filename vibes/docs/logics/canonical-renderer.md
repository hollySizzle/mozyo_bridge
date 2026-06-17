# Canonical Single-Source Conditional Renderer

## Purpose

LLM ランタイム入口 (`AGENTS.md` / `CLAUDE.md`、preset workflow、skill router 等) は target / tool / preset の組み合わせで内容が分岐する。手作業で複数 file を並行に編集すると drift しやすく、しかも LLM は参照先が分散すると追加 file を読まないことがある。本 logic は次の三点を満たす rendering 系を定義する。

1. 正本は **1 つの canonical source** に置く。
2. tool / preset / distribution target などの **context** に応じて canonical source の fragment を選択 / 連結し、runtime 入口に必要な本文を inline で出力する。
3. 生成物が canonical source からずれたら `--check` で fail する。

最小導入は Redmine #10345 で `src/mozyo_bridge/scaffold/presets/_router/{AGENTS,CLAUDE}.md` の 2 file を対象として実装する。renderer の構造は新規 canonical source (skill router 雛形、governed preset workflow、README 抜粋 など) を追加できる形にしておく。

## Canonical Source Layout

```
src/mozyo_bridge/scaffold/canonical_sources/
├── <source_id>.yaml          # canonical source 宣言 (1 file = 1 source)
└── <source_id>/
    └── bodies/
        ├── <fragment>.md     # 各 fragment が出力する byte をそのまま保持
        └── ...
```

YAML 1 file が **1 canonical source** に対応する。本 logic では `id` は `kebab-case` を推奨する。YAML から参照される body file は `<source_id>/bodies/` に置き、fragment の `body_file` field は YAML から見た相対 path (`bodies/title_codex.md` 等) を指定する。

YAML の最小 shape:

```yaml
id: <source_id>
description: |
  この source が何を生成し、どの context 変数を使うかの 1〜数行説明。
outputs:
  - target: presets/_router/AGENTS.md
    context: {tool: codex}
  - target: presets/_router/CLAUDE.md
    context: {tool: claude}
fragments:
  - id: <fragment_id>
    when: {tool: codex}        # 省略可。省略時は常に出力。
    body_file: bodies/<file>.md  # body または body_file のどちらか
  - id: <fragment_id>
    body: |
      inline body も書けるが、複数行の Markdown は body_file 推奨。
```

`outputs[].target` は scaffold tree (`src/mozyo_bridge/scaffold/`) からの relative path として解釈する。`..` を含む path や absolute path は invalid。

## Context And `when` Matching

context は `Mapping[str, str]` を想定する。fragment は次の規則で発火する。

- `when` が省略されている / 空 mapping の場合: 常に発火 (shared fragment)。
- `when` が 1 つ以上の key/value を持つ場合: **すべての key について `context[key] == when[key]`** が成立するときだけ発火。
- `context` に key がない場合は match しない (誤って常に発火させない安全側)。
- `when` の key を context が含まない (= 当該 output が当該 axis を持たない) ケースは設計エラー扱い。
- `bool` / `None` を value に書くと load 時に die する。axis の値域は文字列で固定する。

複数 axis (例: `tool` × `preset` × `distribution`) を扱う場合も同じ規則で発火を絞れる。新 axis を導入する場合は context key を増やすだけでよい。canonical source の YAML 側に当該 axis を持つ fragment が存在しないなら、既存 fragment は影響を受けない。

## Rendering Algorithm

```text
for each output in source.outputs:
    rendered = ""
    for fragment in source.fragments:           # 宣言順を保つ
        if fragment_matches(fragment, output.context):
            rendered += fragment.body            # 区切り文字を勝手に挟まない
    if output.substitutions:                    # 任意。次節 Per-Output Substitutions
        rendered = apply_substitutions(rendered, output.substitutions)
    write_or_compare(rendered, resolve_output_path(output))
```

fragment body は **加工せずそのまま** 連結する。連結境界の改行・空行は body file 側で表現する。runtime entry が `${...}` 形式の変数 (例: `${rule_path}`、`${ticket_anchor_label}`) を含む場合、それは literal text として body file に書き、scaffold render 時の `Template.safe_substitute` に任せる。canonical renderer 自身は `${...}` を解釈しない。

## Per-Output Substitutions

fragment 連結だけでは表現しにくい inline 差分 (preset 名 / framework 別の path 例 / 1 行内の小さな phrase 差) を、output ごとの `substitutions: {NAME: value, ...}` で吸収する。body file に `{{NAME}}` 形式の placeholder を書き、render 時に output の substitutions map から literal 文字列に置換する。

```yaml
outputs:
  - target: presets/redmine-governed/agent-workflow.md
    substitutions:
      TITLE: "Redmine Governed Agent Workflow"
      PRESET_NAME: "redmine-governed"
      IMPL_FILE_PATTERNS: "    - src/**\n    - tests/**\n    - config/**\n    - lib/**\n    - docs/**"
  - target: presets/redmine-rails-governed/agent-workflow.md
    substitutions:
      TITLE: "Redmine Rails Governed Agent Workflow"
      PRESET_NAME: "redmine-rails-governed"
      IMPL_FILE_PATTERNS: "    - app/**\n    - spec/**\n    - test/**\n    - config/**\n    - db/**\n    - lib/**"
fragments:
  - id: workflow
    body_file: bodies/workflow.md
```

規約:

- placeholder 構文は `{{NAME}}` (`NAME` は `\w+`)。`${...}` (scaffold-time)、`«...»`、`<<...>>` 等とは衝突しない。
- 置換は **単一 pass**。substitution value 内の `{{...}}` は再展開しない。意図的に literal `{{X}}` を残せる。
- output に `substitutions` が無い場合: body に `{{...}}` が含まれていれば die する (literal placeholder を ship しないため)。
- output に `substitutions` がある場合: body 内に出現するすべての `{{NAME}}` の `NAME` が map に存在しないと die。complete map を要求することで、authoring miss を render 時に強制検出する。
- placeholder の値は **literal 文字列** として書き、改行は `\n` でエスケープする (YAML block scalar は indent 周りで混乱しやすいため double-quoted string 推奨)。

`{{NAME}}` vs fragment-level `when`:

- 同一 axis での **大きな構造差** (section が丸ごと有る/無い) は `when` で fragment を切り替える方が読みやすい。
- 同一 axis での **小さな phrase / path 差** (paragraph 内の単語差や 1 行内の値差) は `substitutions` で吸収する方が body の重複が消える。
- 両者は併用可能。同じ output に対して、まず `when` で fragment を選び、その後 substitutions で finishing する。

governed preset workflow (`governed-workflow.yaml`) は `redmine-governed` と `redmine-rails-governed` の `agent-workflow.md` を 1 つの shared body file + 約 17 の substitution key で render する事例。両 preset 間の手作業同期 drift を canonical layer で潰した最初の slice。

## CLI Surface

```bash
mozyo-bridge scaffold canonical [--repo <root>]            # 全 canonical source を render
mozyo-bridge scaffold canonical --check [--repo <root>]    # 全 canonical source を drift check
```

- `--repo` 省略時は cwd。`MOZYO_REPO` / `.git` parent 解決に従う。
- `--check` は再 render した内容と committed file を byte 比較し、差異があれば exit 1。stderr に対象 path と再 render 手順を出す。
- `render` (default) は **既存ファイルの内容に関わらず上書き** する。`scaffold apply` の `--backup` / `--force` のような保護はかけない。canonical source が source of truth であるため、render の結果は正解である。
- 1 source も見つからない場合は die する (`scaffold canonical` を間違った repo root に対して走らせた、scaffold の path が壊れている、等を早期検出するため)。

## Drift Detection Contract

`--check` は **2 種類の drift** を同じ exit 1 で報告する。

1. **on-disk file が canonical render と異なる** (`is out of date`): renderer が変わった / canonical source が変わった / だれかが committed template を手編集した。
2. **on-disk file が存在しない** (`is missing`): 生成 file が削除された / 新規 output を canonical source に追加して未 render。

両者とも `mozyo-bridge scaffold canonical` (`--check` を付けない既定動作 = render) で復旧する。

## Generated File Policy

canonical source から render された file は **generated artifact** として扱う:

- `Repo-Local Guardrail Autonomous Lane` (`vibes/docs/rules/**` 等) や `.mozyo-bridge/docs/file_conventions.generated.yaml` と同じ位置付け。手編集してはいけない。
- 変更したい場合は canonical source の YAML または body file を編集し、`render` で再生成する。
- CI / pre-release verification では `scaffold canonical --check` を docs `audit-impact` などと並列に走らせ、drift を gate にする。

ただし render 出力の `_router/AGENTS.md` / `_router/CLAUDE.md` は scaffold rendering の **template** であり、最終的に target repo に書き出される router 本体ではない。`scaffold apply` は引き続き `_router/` を読み込み、`apply_project_local_preservation` を含む既存 pipeline を通してから target repo に書く。canonical renderer はその一段上流に位置する。

## Project-Local Additions

`_router/AGENTS.md` / `_router/CLAUDE.md` には `<!-- mozyo-bridge:project-local-additions:begin -->` / `:end` の marker pair が canonical source 側に含まれている。下流の `scaffold.rules.apply_project_local_preservation` が target repo の on-disk router を読み、marker 間 content を template に substitute する。canonical renderer は template そのものを生成するだけで、target repo 側の preservation には関与しない。両 layer は独立に動く。

## Tests

`tests/test_docs_canonical_workspace.py::CanonicalRendererTest` で次を pin する:

- `_router/AGENTS.md` / `_router/CLAUDE.md` が canonical render と byte-equal。
- tool 別の conditional dispatch: `tool=codex` の render に Codex 固有 fragment だけが入り、Claude 固有 fragment は混入しない。逆も同じ。
- shared fragment (session-start opening) は両 render に同じ byte で現れる。
- Project-Local Additions marker pair は両 render に含まれ、`begin` が `end` より前に来る。
- CLI `scaffold canonical --check` が clean state で exit 0、drift で exit 1、missing で exit 1。
- 復旧 path: `render` で再生成すると `--check` が再び clean になる。
- canonical body file の編集が render 出力に伝播する (renderer が body file を都度読んでいる証明)。
- 復旧文言の regression: `--check` stderr に invalid sub-subcommand (`canonical render` / `canonical check`) が出ない。

`tests/test_docs_canonical_workspace.py::GovernedWorkflowCanonicalTest` で governed preset slice を pin する:

- `redmine-governed/agent-workflow.md` と `redmine-rails-governed/agent-workflow.md` が canonical render と byte-equal。
- 各 preset の render に framework-specific phrase (preset 名 / 実装 path 群 / Rails-only command `bundle exec rspec` 等) が正しく入り、相手 framework の phrase が混入しない。
- governance keyword (Implementation Done is not completion, owner_close_approval, Codex Direct Edit Gate, Repo-Local Guardrail Autonomous Lane, etc.) が両 render に残る。
- substitution engine の負け side: 未定義 placeholder と `substitutions` 不在 body で die する。substitution は単一 pass で、value 内の `{{X}}` を再展開しない。

## Extending To New Outputs

新しい canonical source を追加する手順:

1. `src/mozyo_bridge/scaffold/canonical_sources/<new_id>.yaml` を作る。
2. body 内容を `<new_id>/bodies/*.md` に置く。
3. `outputs[].target` を `scaffold/` 配下の相対 path として宣言する (`scaffold` outside の path は本 logic の対象外)。
4. `mozyo-bridge scaffold canonical` を走らせて render 出力を commit する。
5. `mozyo-bridge scaffold canonical --check` が clean になることを確認する。
6. 必要なら `tests/test_docs_canonical_workspace.py::CanonicalRendererTest` に新 source 用のアサーションを足す。

`scaffold/` の外 (例: `skills/**`、`plugins/**`、`README.md`) を canonical-render したい場合は本 logic の `SCAFFOLD_OUTPUT_ROOT_RELATIVE` を拡張するか、scaffold とは別の renderer を立てる判断を入れる。当面の `scaffold canonical` は scaffold tree の generator として位置付け、他 surface への拡張は別 issue で評価する。
