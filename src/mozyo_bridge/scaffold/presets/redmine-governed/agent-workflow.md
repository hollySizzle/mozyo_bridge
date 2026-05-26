# Redmine Governed Agent Workflow

## Layered Source

この preset は特定 framework に依存しない Redmine 開発に full governance package を被せる preset である。まず汎用 Redmine workflow を読む:

- `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`

この file が存在しない場合は、読んだふりをせず `mozyo-bridge rules install` を依頼して停止する。本 governed preset は Redmine base を **置き換えず、上乗せする**。base の本文を複製しない。

## Scaffolded Repo-Local Artifacts

`mozyo-bridge scaffold apply redmine-governed` は通常の router 一式に加え、target repo に **full governance package の素材** を repo-local artifact として配置する。以下は scaffold 時に必ず target repo の `.mozyo-bridge/` 配下に書き込まれる (既存があれば、`--backup` で退避してから上書きする)。

- `.mozyo-bridge/rules/llm_rule_authoring.md` — LLM 向け規約文書の正本分離、形式選択、gate 構造化、検証接続を定義する authoring 契約。
- `.mozyo-bridge/rules/docs_catalog_governance.yaml` — docs catalog、generator、resolver、audit-doc impact tooling の統治規約。
- `.mozyo-bridge/docs/catalog.yaml.example` — target repo が catalog を埋めるための skeleton。固有業務ドメインは含まない。
- `.mozyo-bridge/tmux/agent-ui.conf` — Claude / Codex tmux window を控えめに見分けるための UI snippet。host の `~/.tmux.conf` には自動追記しない。
- `.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example` と `.claude-nagger/.gitignore` — Claude Nagger の repo-local 設定 skeleton。project が採用する場合に example から実設定へ昇格する。

これら artifact の **正本は本 preset (scaffold) 側にある**。target repo 側で修正したい場合は preset 側に upstream し、`mozyo-bridge scaffold apply --backup` で再配布する流れを取る。target repo 側の手編集は drift の原因になる。

docs catalog tooling (validator / resolver / generator / impact checker) は **mozyo-bridge package 側に同梱** されている。`mozyo-bridge docs ...` CLI が target repo の `.mozyo-bridge/docs/catalog.yaml` を読んで動く。target repo は Python source を vendor copy しない。CLI 一覧は `Active-Doc Resolver` を読む。

## Governance Posture

governed preset は次の三本柱で動く。

1. **正本性** — 作業状態の durable record は Redmine issue / journal。durable record が無い・曖昧・矛盾している場合は実装着手しない。
2. **gate 分離** — Start / Progress / Design Consultation / Design Consultation Answer / Implementation Done / Review Request / Review / QA Verification / Production Verification / Close を独立 journal として残す。Implementation Done は completion ではない。Review Gate approval も Close ではない。base Redmine の `Close Approval Separation` を継承し、Close には owner close approval を別 journal として要求する。
3. **catalog 駆動の docs 解決** — 変更対象 path から、その path に紐づく guardrail / spec / convention を catalog 経由で解決し、本文を読んでから実装・監査する。generated 物を正本にしない。

## Agent Execution Contract

この `agent-workflow.md` が governed preset の agent 実行契約の正本である。gate、役割、編集可否、引き継ぎ、完了条件を別の `development_flow.md` に分散しない。target repo に追加の project-local rule が必要な場合は、Project-Local Additions か docs catalog に登録された project 固有 docs へ置き、ここに同じ判断材料を複製しない。

### 正本性

```yaml
優先順位:
  - Redmine issue と Redmine journal
  - この agent-workflow.md
  - .mozyo-bridge/rules/docs_catalog_governance.yaml
  - .mozyo-bridge/docs/catalog.yaml
  - AGENTS.md / CLAUDE.md / local skill / runbook
chat_pane通知: 正本ではなく通知のみ
チケットシステム: Redmine
通知手段: mozyo-bridge
```

### 既定役割

```yaml
役割:
  claude_code: 実装者
  codex: 監査者
  owner: 最終判断者
実装者の責務: [code, schema, tests, 実装隣接docs]
監査者の責務: [review, 設計相談回答, 規約解釈, Redmine判断記録]
```

project が実装者 / 監査者 split を採用していない場合は、上記を採用しないでよい。ただし採用したら、本 file の境界を曖昧にしない。

### パス別編集権限

```yaml
実装ファイル:
  patterns:
    - src/**
    - tests/**
    - config/**
    - lib/**
    - docs/**
  既定編集者: claude_code
  codex編集条件: codex_direct_edit gate が有効 (allowed_paths に該当 path を明示)
ガードレール:
  patterns:
    - AGENTS.md
    - CLAUDE.md
    - .mozyo-bridge/rules/**
    - .mozyo-bridge/docs/catalog.yaml
    - .codex/skills/**
    - .claude/skills/**
  既定編集者: claude_code
  codex編集条件: codex_direct_edit gate が有効 (allowed_paths にガードレール path を明示)。
    chat 上で「ユーザーがガードレール変更を明示」しただけでは gate は成立しない。
    gate journal に role / direct_edit / allowed_paths / reason / follow_up_review が
    揃って初めて edit 可。chat 命令は file edit 許可ではない (Codex Direct Edit Gate を読む)。
ガードレール変更で触らないもの:
  - src/**
  - tests/**
  - config/**
  - lib/**
  - docs/**
  - その他 project が実装ファイルと定義した path
generated物:
  patterns:
    - .mozyo-bridge/docs/file_conventions.generated.yaml
    - その他 catalog generator / docs tooling の出力
  既定編集者: なし (generator のみ)
  手編集: 禁止 (Claude / Codex / owner いずれも不可)
  更新手順:
    - catalog (`.mozyo-bridge/docs/catalog.yaml`) を変更する
    - `mozyo-bridge docs generate-file-conventions` で再生成する
    - `mozyo-bridge docs generate-file-conventions --check` で drift を確認する
```

実装ファイルのパターンは project に合わせて調整してよい。だが、ガードレール変更 issue で実装ファイルを併せて触らないという原則は維持する。generated 物は path 別編集権限の対象外で、`Docs Catalog Governance` に従って generator 経由でのみ更新する。

### Gate Schema

```yaml
start:
  必須: [issue, parent_issue, 目的, 受け入れ条件, 参照docs, 未確認事項]
implementation_done:
  actor: 実装者
  必須: [変更ファイル, 実装意図, 前提, 未確認事項, 検証結果, docs更新, commit_or_diff]
review_request:
  actor: 実装者
  必須:
    - implementation_done_journal
    - commit_or_diff
    - 変更ファイル
    - review観点
    - 未確認事項
    - 受信agent
    - 受領方法
review:
  actor: 監査者
  必須: [対象commit_or_diff, resolved_docs, 照合規約, 指摘事項, 未確認事項, 再review要否, 結論]
  指摘事項_分類:
    - 事実: コード・設定・docs で確認済みの不整合のみ
    - 仮説: 確認すべき事項と確認方法を併記
close:
  必須:
    - 受け入れ確認
    - 指摘対応
    - 残留リスク
    - review結果
    - owner_close_approval (Review Gate とは別 journal)
    - commit_hash_record
    - close判断
codex_direct_edit:
  actor: codex
  有効条件:
    必須: [role:実装者, direct_edit:true, allowed_paths, reason, follow_up_review]
    根拠: Redmine journal または owner 明示指示
  無効marker:
    - "着手:codex"
    - "実装完了:codex"
    - "担当:codex"
    - "codex対応"
  禁止_並行表現:
    - "実行せよ"
    - "対応して"
    - "やって"
    - "implement it"
    - "go ahead"
    - "お願いします"
    - "進めて"
    上記の短い命令だけでは codex_direct_edit gate は有効化しない
```

### Codex Direct Edit Gate

通常開発の実装 file (例: `src/**`, `tests/**`, `config/**`, `lib/**`, `docs/**`) を Codex (監査者) が直接編集してよい条件は狭く制限する。`codex_direct_edit` gate journal が active issue に明示存在する場合に限り、Codex は `allowed_paths` だけを直接編集できる。gate 未存在で `do it` / `対応して` / `実行せよ` / `implement it` / `お願いします` 等を受けても、Codex は通常実装を Claude へ handoff する。短い命令は file edit 許可ではない。

同じ gate 要件は **ガードレールおよび docs/catalog 周辺** にも適用する。`AGENTS.md`、`CLAUDE.md`、`.mozyo-bridge/rules/**`、`.mozyo-bridge/docs/catalog.yaml`、`.codex/skills/**`、`.claude/skills/**` を Codex が直接編集するには、active issue に `codex_direct_edit` gate journal が存在し、`allowed_paths` に該当ガードレール path が明示されている必要がある。chat 上の「ガードレール変更を明示」「Codex でやって」等の短い指示は、それ単独では gate 成立条件を満たさない。Claude へ handoff し、Claude が実装→Implementation Done Gate→Review Request Gate を経由するのが default。

`.mozyo-bridge/docs/file_conventions.generated.yaml` をはじめとする generator 出力は **誰も手編集しない** (Claude / Codex / owner いずれも不可)。catalog を変更し、`mozyo-bridge docs generate-file-conventions` で再生成、`--check` で drift 確認の流れに乗せる。手編集された場合は generated 物を破棄し、catalog 起点で再生成する。

direct edit を行った場合、適用した例外、ユーザー指示の引用、変更 files、verification、follow-up review 要否を Redmine journal に記録する。例外なき監査者の通常実装 (`着手:codex` / `実装完了:codex` / `担当:codex` / `codex対応`) は invalid marker として扱い、reopen + correction journal を起票する。ガードレール / docs / catalog scope での gate 不在 commit (例: chat の短い指示を根拠に Codex が `.mozyo-bridge/docs/catalog.yaml` や `vibes/docs/**`、`README.md` を直接 commit した場合) も同じ correction flow に乗せる。

### LLM 実行契約

```plantuml
@startuml mozyo_bridge_agent_gate_contract
start
$作業root確認(target_repo_root)
$central_presetを読む()
$layered_preset(redmine, redmine-governed)を読む()
$このagent_workflowを読む()
$docs_catalogを読む()
$redmine_issueを読む()
$parent_issueを読む()
$現在journalを読む()
$現在gateを解決()
$agent役割を解決()
if ($入力がticket_idまたは短い実行指示()) then (yes)
  $編集権限を否定("短い指示はfile edit許可ではない")
endif
if ($対象pathが分かる()) then (yes)
  $resolve_audit_docsを実行()
  $解決docs本文を読む()
endif
if ($agentがcodex()) then (yes)
  if ($対象が実装ファイル()) then (yes)
    if ($gate有効("codex_direct_edit")) then (yes)
      $allowed_pathsだけ編集()
      $直接編集理由を記録()
    else (no)
      $経過記録("Codexに実装編集gateなし")
      $claude_codeへ引き継ぎ()
      stop
    endif
  endif
endif
if ($gate有効("review_request")) then (yes)
  $codex_reviewを実行()
  $review_gateを記録()
  $claude_codeへ通知()
else (no)
  if ($agentがcodex() && $依頼がreview()) then (yes)
    $監査不能を記録("review_request gate不足または不正")
    $claude_codeへ通知()
    stop
  endif
endif
if ($agentがclaude_code() && $役割が実装者()) then (yes)
  $scopeを守って実装()
  $implementation_doneを記録()
  $review_requestを記録()
  $codexへreview通知()
endif
if ($close要求()) then (yes)
  if ($review_gateあり() && $owner_close_approvalあり() && $commit_hash_recordあり()) then (yes)
    $close_gateを記録()
  else (no)
    $close_blockedを記録()
    stop
  endif
endif
stop
@enduml
```

### 禁止遷移

```yaml
禁止:
  - id: codex_implements_from_ticket_id_only
    条件: [agent:codex, input:ticket_id_or_short_instruction, codex_direct_edit_gate:missing]
    action: stopしてClaudeへ引き継ぐ
  - id: review_without_review_request
    条件: [agent:codex, review_request_gate:missing]
    action: 監査不能を記録
  - id: close_after_implementation_done_only
    条件: [implementation_done:present, review_gate:missing]
    action: close禁止
  - id: close_without_owner_approval
    条件: [review_gate:present, owner_close_approval:missing]
    action: close禁止
  - id: notify_without_redmine_gate
    条件: [handoff_or_review_notification:requested, journal_gate:missing]
    action: gate作成または作成依頼を先に行う
  - id: use_retired_transport
    条件: [transport: .agent_handoff/tasks.yaml or read-next --wait or Stop hook]
    action: 拒否してRedmine journalを使う
```

## Docs Catalog Governance

scaffold-shipped `.mozyo-bridge/rules/docs_catalog_governance.yaml` を正本として扱う。要約:

- `.mozyo-bridge/docs/catalog.yaml` を `documents` / `related_document_refs` / `file_conventions` の正本とする。target repo が初期化時に `catalog.yaml.example` を `catalog.yaml` にコピーして埋める。
- 生成物 (例えば nagger 用の `file_conventions.yaml`) は **正本ではなく generator の出力**。手編集禁止。catalog を変更し、generator で再生成し、drift check を通す。
- 監査時は generated file だけで判断せず、catalog で解決された docs 本文を読む。
- 正本性・対応関係を AGENTS.md / CLAUDE.md / runbook に重複定義しない。catalog と rule file の二箇所に同じ判断材料を書かない。

## Active-Doc Resolver

target repo は次の解決経路を持つ:

```bash
mozyo-bridge docs resolve --format markdown <changed_path> [...]
mozyo-bridge docs validate
mozyo-bridge docs validate --check-file-coverage [--coverage-root src/...] [--coverage-root tests/...]
mozyo-bridge docs generate-file-conventions --check
mozyo-bridge docs audit-impact --all-changed --check-generated
```

- 変更対象 path が分かったら resolver を実行し、解決された docs 本文を読んでから実装・監査する。
- catalog 自体や file_convention pattern を変更した場合は validator と coverage check を通す。coverage roots の選択順序は **(1) CLI `--coverage-root`** が指定されていればそれ、**(2) catalog の `coverage_roots` field** が定義されていればそれ、**(3) validator 組み込み default**。CLI が catalog より優先される。project が該当 layer を持たない場合は missing root は `notice:` として印字されるだけで exit code には影響しない。project ごとの恒久指定は catalog 側に書く運用が望ましい。
- file_conventions 生成物 (project が採用している場合) を変える場合は generator を実行し、drift check を通す。
- staged commit 直前は `mozyo-bridge docs audit-impact --staged --check-generated` を通す。作業中の棚卸しでは `--all-changed`。
- いずれの command も `--repo <path>` で target repo を、`--catalog <path>` で catalog 位置を override できる。default は cwd / `<repo>/.mozyo-bridge/docs/catalog.yaml`。

これらは catalog が埋まっていれば即座に機能する。catalog が空でも tool 自体は valid catalog skeleton を accept するため、operator は段階的に埋められる。`--check-file-coverage` も project 固有 layer の有無に関わらず安全に実行できる。

## LLM Rule Authoring

target repo に新規 rule / gate / workflow / skill 入口を足すときは、scaffold-shipped `.mozyo-bridge/rules/llm_rule_authoring.md` を正本に従う:

- 入口 file (AGENTS.md / CLAUDE.md / skill entrypoint) は薄い router にする。詳細 gate / 手順を入口に焼かない。
- 詳細 rule は `.mozyo-bridge/rules/**` または target 側の catalog に紐づく rule docs に置く。同じ判断材料を複数 file に複製しない。
- 行動制御部分は自然文だけでなく、必須項目 / invalid marker を YAML/構造で書く。
- 分岐や停止条件があるときは PlantUML 風 DSL で関数的に書き、agent が読みやすい順序で並べる。
- 規約変更は catalog / resolver / generator / drift check に接続し、検証 command を runbook に書く。

## Required Verification

target repo 内で次の verification を `Implementation Done` または `Review` の前に実行する。command が存在しないか実行不能な場合は理由を Redmine journal に残す。

- `mozyo-bridge docs validate`
- `mozyo-bridge docs validate --check-file-coverage`
- `mozyo-bridge docs generate-file-conventions --check` (project が file_conventions 生成物を採用している場合)
- `mozyo-bridge docs audit-impact --all-changed --check-generated`
- project の authoritative test command (例: package test、unit test、integration smoke、project が定める subset)。
- lint / type check / static analysis は project ルールに従う。

`looks fine` は verification record ではない。command が走らなかった理由と、代替確認の内容を Redmine に残す。

## Journal Templates

```markdown
## Gate: codex_direct_edit
- role: 実装者
- direct_edit: true
- allowed_paths:
- reason:
- follow_up_review:

## Gate: review_request
- implementation_done_journal:
- commit_or_diff:
- changed_paths:
- review_focus:
- receiver: Codex
- receive_method: mozyo-bridge journal <id>

## Gate: review
- target_commit_or_diff:
- resolved_docs:
- 照合規約:
- 指摘事項 [事実]:
- 指摘事項 [仮説]:
- 未確認事項:
- 再review要否:
- 結論:

## Gate: close
- 受け入れ確認:
- 指摘対応:
- 残留リスク:
- review結果journal:
- owner_close_approval_journal:
- commit_hash:
- close判断:
```

## Completion

Implementation Done は完了ではない。Redmine に Review Gate、指摘対応、owner close approval journal (Review Gate とは別)、commit hash record、Close Gate が記録されるまで完了扱いしない。

## Repo-Local Rules Maintenance (governed mode)

- Dev Container / ephemeral home 対応として、target repo は `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` を repo-local preset として読むことができる。
- preset store を再生成する場合は `mozyo-bridge rules install --repo-local .` を使う。
- router + governance artifact を再生成する場合は `mozyo-bridge scaffold apply redmine-governed --repo-local --target . --backup` を優先する。`--force` は差分を確認してから使う。
- governed preset 配布物 (`.mozyo-bridge/rules/llm_rule_authoring.md`、`.mozyo-bridge/rules/docs_catalog_governance.yaml`、`.mozyo-bridge/docs/catalog.yaml.example`) とこの `agent-workflow.md` は scaffold preset 側を正本とする。target repo で個別に編集したい変更は preset 側へ upstream し、`mozyo-bridge rules install` と `mozyo-bridge scaffold apply --backup` で再配布する手順を取る。`.mozyo-bridge/docs/catalog.yaml` (example 不付き) は target repo 側で自由に埋めてよく、scaffold は上書きしない。docs catalog tooling は mozyo-bridge package 側に同梱されており、target repo は Python source を保持しない。`mozyo-bridge` を upgrade すれば tool も同時に更新される。

## Governed Mode Prohibitions

- この `agent-workflow.md` の Codex direct edit gate を bypass して通常実装 file を監査者が直接編集すること。
- generated 物 (file_conventions.yaml の生成物など) を catalog を介さずに手編集すること。
- docs catalog や resolver / generator tooling を deactivate して shared preset の `redmine` だけで完了報告すること (本 preset を採用したなら governance verification も完了条件に入る)。
- catalog に project 固有の業務ドメイン名 (顧客名 / 製品コード / 個人名) を canonical id として焼くこと。catalog id は機能種別 / 層 / spec id 程度に抽象化する。
- review、journal、commit message に credential / token / 個人情報 / 本番データ抜粋を記録すること。
