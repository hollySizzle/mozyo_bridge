# Codex Autonomous Guardrail Lane (mozyo_bridge 採用)

## 目的

mozyo-bridge は配布される `redmine-governed` / `redmine-rails-governed` preset 自体に `### Repo-Local Guardrail Autonomous Lane` を組み込んでいる (preset agent-workflow.md を読む)。これは **product-wide な配布方針** であり、mozyo-bridge を導入する任意の downstream repo が同じ lane を採用できる。

本 doc は **`mozyo_bridge` repo 自身がこの lane をどう採用しているか** を記録する project-local 正本である (Redmine #10338)。downstream repo のサンプル / reference としても機能する。

distributed lane policy の本体は preset 側 (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md`) を読む。本 doc は preset を上書きしない。preset で定義された default scope を **本 project がどう拡張 / 縮小 / 確認しているか** だけを書く。

## `mozyo_bridge` での Lane Scope

preset default の path 集合を **そのまま採用** する。

- `vibes/docs/rules/**`
- `vibes/docs/logics/**`
- `vibes/docs/specs/**`
- `.mozyo-bridge/docs/catalog.yaml`

これらは Codex が `codex_direct_edit` gate journal を作らずに自律的に編集してよい。代わりに edit と同時または commit 直後に `codex_autonomous_edit` journal (lane / changed_paths / intent / verification / commit_hash / follow_up_review_required) を active Redmine issue に残す。

preset が listing する **lane 不可** path を本 project でも継続して保護する:

- `AGENTS.md`, `CLAUDE.md`
- `.mozyo-bridge/rules/**`
- `.codex/skills/**`, `.claude/skills/**`
- `skills/mozyo-bridge-agent/**`, `plugins/mozyo-bridge-agent/**`
- `src/**`, `tests/**`, `docs/**`, `README.md`, `RELEASE_NOTES.md`
- `src/mozyo_bridge/scaffold/presets/**`
- generator 出力 (`.mozyo-bridge/docs/file_conventions.generated.yaml` 等)

これらに変更が必要な場合は `codex_direct_edit` gate (Codex が直接編集する場合) または Claude handoff (default) を使う。

## `mozyo_bridge` での `codex_autonomous_edit` 実行例

preset 側 schema をそのまま採用する。journal field の最小例:

```markdown
## Gate: codex_autonomous_edit

- actor: Codex (autonomous lane)
- lane: autonomous
- changed_paths:
  - vibes/docs/specs/project-map.md
- intent: spec-project-map に最新の重要 path を 1 件追記する。
- verification:
  - `mozyo-bridge docs validate --repo .` → passed
  - `mozyo-bridge docs validate --check-file-coverage --repo .` → passed
  - `git diff --check` → clean
- commit_hash: `<hash>` (or `pending: staged-not-committed`)
- follow_up_review_required: false
```

`.mozyo-bridge/docs/catalog.yaml` を含む edit では追加で `mozyo-bridge docs generate-file-conventions --check --repo .` と `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .` を実行し、journal の `verification` フィールドにその結果を残す。drift があれば `mozyo-bridge docs generate-file-conventions --repo .` で regenerate して再 commit する。

## Lane を起動しない条件 (preset 側と同じ)

- 変更が lane 範囲を超える。
- 変更が central preset / 配布 surface に影響する。
- 変更が credential / token / 個人情報 / 認証フローに触れる。
- product owner の以前の指示と矛盾する変更を入れる必要がある。
- 同一 issue で過去に同じ path について `要修正` / `block` review を受けている。

該当する場合は Claude handoff または `codex_direct_edit` gate に escalate する。

## Workflow-Change Verification

本 lane policy 自身が workflow / guardrail 変更であるため、policy 配布後の **次の通常開発タスク** で本 lane が想定通りに機能することを workflow-change verification として確認する。

- 検証 task は本 lane を直接変更しない通常開発タスクとする。
- 検証 task では Claude が実装し、Codex は lane を実際に 1 回以上利用して repo-local guardrail を更新する。`codex_autonomous_edit` journal が破綻なく回ることを durable record として残す。
- 結果を Redmine issue に記録する。lane policy 自身に gap が見つかれば preset / project-local doc / catalog のいずれかへ follow-up issue を起票する。

### 実施記録

- 2026-05-28: Redmine #10344 を通常開発 task として選び、Claude が test-only commit `5b6201b3` を実装、Codex が review し、本 doc への追記を autonomous lane 実演として実施した。durable record は #10344 review gate と #10338 `codex_autonomous_edit` journal を参照する。

## 関連 Doc

- distributed lane policy 本体: preset agent-workflow.md `### Repo-Local Guardrail Autonomous Lane`
- central preset Codex Direct Edit Gate: 同 file `### Codex Direct Edit Gate`
- project-local agent workflow: `vibes/docs/rules/agent-workflow.md`
- skill workflow: `skills/mozyo-bridge-agent/references/workflow.md` `## Policy / Skill Authoring Boundary`
- docs catalog governance: `.mozyo-bridge/rules/docs_catalog_governance.yaml`
- product owner 判断の durable anchor: Redmine #10338
