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

## Guardrail Health Check Scope

guardrail health check は lane の編集可否確認ではなく、guardrail とその配布 mirror / 隣接 spec が矛盾していないかを読む点検である。したがって、health check の読取対象は editable lane scope より広く取る。

次回以降の guardrail health check では、plain `rg --files` だけに依存しない。hidden directory を含む列挙 (`rg --files --hidden`) または明示 path list で、少なくとも次を対象に含める。

- `.mozyo-bridge/rules/**`
- `.mozyo-bridge/docs/catalog.yaml`
- `vibes/docs/rules/**`
- `vibes/docs/logics/**`
- `vibes/docs/specs/**`
- `skills/mozyo-bridge-agent/references/**`
- `src/mozyo_bridge/scaffold/presets/**`
- `src/mozyo_bridge/scaffold/presets/*/files/.mozyo-bridge/**`

`src/mozyo_bridge/scaffold/presets/*/files/.mozyo-bridge/**` は hidden directory 配下の preset-internal mirror である。health check では対象に含めるが、編集権限は lane scope へ昇格しない。変更が必要なら Claude handoff または `codex_direct_edit` gate を使う。

mirror / generated copy を重複として省く場合は、byte identity や generator check などの根拠を journal に残す。根拠なしに「mirror なので同等」と扱って読取対象から外さない。

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
  - `mozyo-bridge docs generate-file-conventions --check --repo .` → passed
  - `git diff --check` → clean
- commit_hash: `<hash>` (or `pending: staged-not-committed`)
- follow_up_review_required: false
```

`.mozyo-bridge/docs/catalog.yaml` を含む edit では追加で `mozyo-bridge docs generate-file-conventions --check --repo .` と `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .` を実行し、journal の `verification` フィールドにその結果を残す。drift があれば `mozyo-bridge docs generate-file-conventions --repo .` で regenerate して再 commit する。

## Lane を起動しない条件

正本は preset `#### lane を起動しない条件` (範囲超過 / 配布 surface 影響 / credential 接触 / owner 指示との矛盾 / 同一 path の `要修正`・`block` 歴)。該当時は Claude handoff または `codex_direct_edit` gate に escalate する。本 repo 固有の追加条件はない (#13028 で pointer 化)。

## Workflow-Change Verification

正本は preset `#### Workflow-Change Verification` (lane policy 変更後、lane を直接変更しない通常開発タスクで機能確認し、`codex_autonomous_edit` journal が破綻なく回ることを durable record に残す)。本 repo 固有の追加はない (#13028 で pointer 化)。

### 実施記録

- 2026-05-28: Redmine #10344 を通常開発 task として選び、Claude が test-only commit `5b6201b3` を実装、Codex が review し、本 doc への追記を autonomous lane 実演として実施した。durable record は #10344 review gate と #10338 `codex_autonomous_edit` journal を参照する。

## 関連 Doc

- distributed lane policy 本体: preset agent-workflow.md `### Repo-Local Guardrail Autonomous Lane`
- central preset Codex Direct Edit Gate: 同 file `### Codex Direct Edit Gate`
- project-local agent workflow: `vibes/docs/rules/agent-workflow.md`
- skill workflow: `skills/mozyo-bridge-agent/references/workflow.md` `## Policy / Skill Authoring Boundary`
- docs catalog governance: `.mozyo-bridge/rules/docs_catalog_governance.yaml`
- product owner 判断の durable anchor: Redmine #10338
