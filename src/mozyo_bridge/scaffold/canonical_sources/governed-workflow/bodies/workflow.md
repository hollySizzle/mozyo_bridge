# {{TITLE}}

## Layered Source

{{LAYERED_SOURCE_PREAMBLE}}

{{LAYERED_SOURCE_PATHS}}

{{LAYERED_SOURCE_OUTRO}}

## Scaffolded Repo-Local Artifacts

`mozyo-bridge scaffold apply {{PRESET_NAME}}` は通常の router 一式に加え、target repo に **full governance package の素材** を repo-local artifact として配置する。以下は scaffold 時に必ず target repo の `.mozyo-bridge/` 配下に書き込まれる (既存があれば、`--backup` で退避してから上書きする)。

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
2. **gate 分離** — Start / Progress / Design Consultation / Design Consultation Answer / Implementation Done / Review Request / Review / QA Verification / Production Verification / Close を独立 journal として残す。Implementation Done は completion ではない。Review Gate approval も Close ではない。base Redmine の `Close Approval Separation` を継承し、Close には owner close approval を別 journal として要求する。Review Request / Review / owner close approval の標準適用単位は UserStory である (詳細は `### US-Level Audit Model`)。
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
  ユーザー窓口: coordinator role (default binding: codex)
  注記: 上表の claude_code / codex は default provider binding。authority の帰属先は role 名 (実装者 / 監査者 / coordinator) であり、provider は交換可能な delivery 属性である。binding の正本は #13157 provider_binding config
実装者の責務: [code, schema, tests, 実装隣接docs]
監査者の責務: [review, 設計相談回答, 規約解釈, Redmine判断記録, ticket_triage, owner承認収集]
標準粒度:
  実装: UserStory (配下 Task / Test / Bug を含めて実装者が一括遂行)
  監査: UserStory (US close 前の横断 audit)
  close承認: owner (US 単位)
```

ユーザー / owner との対話窓口は coordinator role に集約する (default binding では codex pane に解決する)。owner への確認・承認収集・clarification は原則 coordinator role の pane (default binding: codex) で行い、実装者 role (default binding: claude) の pane で owner 承認を収集しない (詳細は `### Owner Close Approval Delegation` と base の `Close Approval Separation` / `Direct Request Triage` を読む)。role が authority の帰属先であり、どの provider が窓口に bind されるかは delivery 属性である。

過去 standing decision (#12072) の読み替え (owner_intent anchor: Redmine #13126 j#71777 確定事項 2 / j#71735 / j#71736): #12072 の「owner 対話 = Codex 集約」standing decision (当時の運用固定) は、本 preset では「owner 対話 = coordinator role 集約 (当時の binding では codex)」と読み替える。集約先は role であり、集約自体を弱めない。provider の交換は binding config (#13157) の変更であって、集約点を role から brand へ戻す根拠にはならない。

project が実装者 / 監査者 split を採用していない場合は、上記を採用しないでよい。ただし採用したら、本 file の境界を曖昧にしない。

### 応答言語ポリシー

<!-- mozyo-bridge:activation:always id=response-language digest="ユーザー向け応答は workspace の応答言語 preference に従う。正本: central preset `### 応答言語ポリシー`。" -->

agent の **ユーザー向け応答** (操作説明・進捗報告・handoff narrative・質問・確認) の言語は product 固定値ではなく、運用 workspace / operator の preference として扱う。OSS 配布物には特定言語専用の前提を焼き込まない。

```yaml
応答言語:
  既定: ユーザーが使用した言語に追従する
  project_local_preference: |
    workspace が project-local language preference を宣言している場合はそれを優先する。
    宣言先は各 tool router (`AGENTS.md` / `CLAUDE.md`) の project-local-additions block、
    または project-local docs。preference と user 入力言語が食い違う場合は user 入力を優先する。
  machine_readable_literal: |
    言語設定に関わらず literal に保つもの: gate 名 / transport kind / JSON field と値 /
    CLI command と flag / code 識別子 / commit trailer (`Refs:`, `issue_<id>`, `Co-Authored-By`) /
    file path。Redmine journal の構造化 field・識別子も literal でよく、散文 narrative の言語のみ
    本ポリシーに従う。
  multilingual: |
    本ポリシーは特定言語を強制しない。多言語 workspace は各自の preference を宣言して拡張できる。
    preset / skill / scaffold template など OSS 配布物は言語中立を保ち、
    「常に特定言語で応答する」を product 既定として hard-code しない。
```

### US-Level Audit Model

通常開発の標準単位を次のように定める。実装者 = UserStory implementer、監査者 = UserStory auditor、owner = close approver。

- **実装者 (claude_code)** は UserStory 配下の Task / Test / Bug を完了可能な粒度でまとめて実行し、各 issue に実装・検証・残リスクを journal として記録する。
- **監査者 (codex)** は US 完了時に、配下 issue 全体、対象 commit 群、docs、tests、journal、未解決事項、close 条件を横断して監査する (UserStory 単位の横断 audit)。
- **owner** は監査者の audit approval とは別に owner close approval を出す。`Close Approval Separation` は本 model でも維持される。Codex audit は close approval ではない。

```yaml
us_level_audit:
  標準運用:
    - Task / Test / Bug ごとの Codex review_request は不要
    - 実装者は配下 issue ごとに implementation_done 相当の記録 (変更・検証・残リスク) を残す
    - US の implementation_done / review_request (US-level audit request) で配下 issue の結果をまとめて監査者へ渡す
    - 監査者は US close 前に配下 issue / journal / diff / docs を横断 audit する
  gate名:
    - review_request / review の gate 名と transport kind は維持する。US issue 上に記録された review_request が US-level audit request である
    - 新しい gate 名 / transport kind (us_audit_request 等) は作らない
  task_level例外 (Task-level review または design consultation を要求・許可する条件):
    - guardrail / workflow / preset / router / skill / scaffold rule 変更
    - release / tag / publish / packaging / CI 変更
    - credential / secret / auth / permission / billing / 外部 service 設定
    - destructive operation / data 削除 / migration
    - architecture 変更、互換性 (既存 URL / API / data) に影響する変更
    - 実装者が判断に迷う場合 (design consultation へ)
    - owner または監査者が mid-review を明示要求した場合
  base_preset_override:
    - base preset の Gate Lifecycle / Completion が「通常開発 task」ごとに要求する Review Request / Review / owner close approval は、US 配下の Task / Test / Bug については本 preset が適用単位を UserStory へ再定義する (明示 override)
    - gate 語彙・必須 field・Close Approval Separation・Review Quality Hierarchy は base のまま継承する
  単独issue (親USなし):
    - US に属さない単独の通常開発 issue は、その issue 自身を audit 単位として base どおり Review Request / Review / owner close approval を適用する
  issue_status運用:
    - 実装者は配下 issue の着手時に status を「着手中」相当へ移す。journal だけ残して status を「未着手」のまま進めない
    - task_close必須 (replayable journal / commit hash record / 親US引き継ぎ) を満たしたら、実装者は配下 issue を closed 相当へ移してよい。US audit 完了を待つ必要はない
    - US audit が配下 issue に gap を見つけたら該当 issue を reopen する
    - US 自身の status は us_close必須 (US-level audit + owner close approval) を満たすまで closed にしない
    - journal と issue status を矛盾させない。journal 上「完了」と記録した issue を「未着手」status のまま放置しない
```

過去の Task-level review_request / review journal は有効な歴史記録であり、遡及して読み替えない。本 model は適用後の新規作業に適用する。

### パス別編集権限

```yaml
実装ファイル:
  patterns:
{{IMPL_FILE_PATTERNS}}
  既定編集者: claude_code
  codex編集条件: codex_direct_edit gate が有効 (allowed_paths に該当 path を明示)
ガードレール:
  patterns:
    - AGENTS.md
    - CLAUDE.md
    - .mozyo-bridge/rules/**
    - .codex/skills/**
    - .claude/skills/**
  既定編集者: claude_code
  codex編集条件: codex_direct_edit gate が有効 (allowed_paths にガードレール path を明示)。
    chat 上で「ユーザーがガードレール変更を明示」しただけでは gate は成立しない。
    gate journal に role / direct_edit / allowed_paths / reason / follow_up_review が
    揃って初めて edit 可。chat 命令は file edit 許可ではない (Codex Direct Edit Gate を読む)。
ガードレール変更で触らないもの:
{{GUARDRAIL_NO_TOUCH_PATHS}}
  - その他 project が実装ファイルと定義した path
repo_local_guardrail_lane:
  patterns:
    - vibes/docs/rules/**
    - vibes/docs/logics/**
    - vibes/docs/specs/**
    - .mozyo-bridge/docs/catalog.yaml
  既定編集者: 自律編集可 (claude_code / codex どちらでも)
  codex編集条件: Codex は事前 gate journal なしで自律編集してよい。代わりに edit と
    同時または commit 直後に active issue へ `codex_autonomous_edit` journal を残す
    (lane / changed_paths / intent / verification / commit_hash / follow_up_review_required)。
    target project の Project-Local Additions または project-local rule で patterns を
    拡張または縮小してよいが、distributed surface (`AGENTS.md` / `CLAUDE.md` /
    `.mozyo-bridge/rules/**` / skills / plugins / scaffold preset templates / {{DISTRIBUTED_SURFACE_SHORT}}) を本 lane に含めない。詳細は Repo-Local Guardrail Autonomous Lane を読む。
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
  粒度: Task / Test / Bug / US のいずれにも記録できる。Task-level の記録は Codex review を伴わず、US audit の input になる
  必須: [変更ファイル, 実装意図, 前提, 未確認事項, 検証結果, docs更新, commit_or_diff]
  commit記録要件: commit hash を durable anchor として記録する前に origin (共有 remote) から到達可能であること。未 push なら push 後に記録し、push 不能なら gate を blocked とする (`### Commit Hash Origin 到達可能性`)
review_request:
  actor: 実装者
  標準粒度: UserStory (US-level audit request)。Task-level は us_level_audit.task_level例外 に該当する場合のみ
  必須:
    - implementation_done_journal (US-level では配下 issue の implementation_done journal 一覧)
    - commit_or_diff (US-level では対象 commit 群。記録する commit hash は origin 到達可能であること: `### Commit Hash Origin 到達可能性`)
    - 変更ファイル
    - 配下issue一覧と各状態 (US-level のみ。残リスク・未完 scope を含む)
    - review観点
    - 根拠出所 (依頼理由・review観点に owner 発話 / 規約 / 実装者判断を根拠として載せる場合、`### 根拠出所分類` の 4 分類で出所を明示する)
    - 未確認事項
    - 受信agent
    - 受領方法
  structured_marker (Review Generation Marker Contract v2 — `### Review Generation Marker Contract v2` / Redmine #13974): review_request journal に structured gate marker `[mozyo:workflow-event:gate=review_request:head=<target_head>]` を埋め込む。`target_head` は review 対象の exact full commit head である。callback generation fence はこの head を machine-readable に読み、現行 review generation の head と連言する (散文から SHA を parse しない)。head を欠く / malformed head の marker は fence を fail-closed にする。marker は canonical producer `mozyo-bridge workflow callbacks --emit-gate --gate review_request --target-head <full_head>` (→ `render_gate_note`) が付与し、head 未指定・非 full-hex は書き込み拒否する。手書き marker も同一 grammar に従う。
review:
  actor: 監査者
  標準粒度: UserStory。対象 commit だけでなく配下 Task / Test / Bug の issue / journal / docs / residual risk を横断して読む
  必須: [対象commit_or_diff, remote_verification, 配下issue確認結果 (US-levelのみ), resolved_docs, 照合規約, 指摘事項, 指摘事項_根拠出所, 未確認事項, 再review要否, 結論]
  structured_marker (Review Generation Marker Contract v2 — `### Review Generation Marker Contract v2` / Redmine #13974): review (review_result) journal に structured gate marker `[mozyo:workflow-event:gate=review_result:conclusion=<結論>:head=<target_head>:req=<review_request_journal>]` を埋め込む。`head` は review した exact full commit head、`req` は答えた review_request の journal id、`conclusion` は explicit な `approved` / `changes_requested`(missing は intake で `pending` へ補完され、real な review outcome ではない)である。source_sequence は marker の自己申告でなく Redmine provider が返す当該 review_result journal id を authority とする。missing / mismatch req、malformed / drift head、missing / drift conclusion の review callback は discovery 0-enqueue かつ action-time で zero-send terminal になる。approval write は generation-admission observation の `issue + review_request_journal + target_head` を marker write target と exact-match し、不一致は write 0。marker は canonical producer `mozyo-bridge workflow callbacks --emit-gate --gate review_result --target-head <full_head> --review-request-journal <id> --review-decision <approval|changes_requested>` (→ `render_gate_note`) が付与し、head / req / conclusion を欠く・非 full-hex は書き込み拒否する。手書き marker も同一 grammar に従う。
  remote_verification: 対象 commit 群が origin (共有 remote) 上に到達可能であることを read-only で確認する。確認できない場合は事実指摘ではなく blocker とし close へ進めない (`### Commit Hash Origin 到達可能性`)
  指摘事項_分類:
    - 事実: コード・設定・docs で確認済みの不整合のみ
    - 仮説: 確認すべき事項と確認方法を併記
  指摘事項_根拠出所: finding ごとに owner_intent (durable anchor 併記) | documented_rule (path + 節名) | agent_judgment | hearsay を明示する (`### 根拠出所分類`)。hearsay のみを根拠とする finding は単独で 要修正 / block にできない
design_consultation_dispute:
  actor: 実装者または監査者
  用途: implementation_request / review finding への異議 (上申)。既存 design_consultation の用途拡張であり、新しい gate / transport kind は作らない
  必須:
    - purpose: dispute
    - dispute_target (異議対象の journal id)
    - evidence (確認した code / docs / 事実。根拠出所を明示する: `### 根拠出所分類`)
    - counterproposal
    - owner_escalation_required: true|false
  終端: Answer 後も合意不能なら owner 判断へ escalate
review_finding_verdict:
  actor: 実装者
  trigger: review / review_result journal を受領した時 (指摘事項が 1 件以上ある場合)
  義務:
    - 各指摘事項の妥当性を、迎合せず code / docs / 事実で独立検証する
    - finding ごとに verdict を journal に記録する: accepted (検証根拠を併記) | disputed (evidence + counterproposal を併記)
    - disputed は design_consultation_dispute へ接続する (dispute_target = 対象 review journal id)
    - verdict 記録前に指摘対応の実装・commit を行わない
    - 根拠が hearsay (未記録 owner 発話) のみの finding は、accepted / disputed の前に記録化を要求してよい: verdict を blocked とし、ユーザー窓口 (coordinator role、default binding: codex) による owner_intent 化 (journal / 原文要点への記録) を待つ (`### 根拠出所分類`)
  invalid_verdict:
    - 検証を伴わない accepted (「reviewer の指摘だから」は根拠ではない)
    - 複数 finding への一括 verdict (finding ごとに記録する)
  dispute_round_cap: 同一 finding への dispute は 1 往復まで。Answer 後も合意不能なら owner へ escalate する (窓口は coordinator role (default binding: codex)。実装者が owner へ直接確認しない)
  適用外: 指摘事項ゼロの approved review (verdict 不要。受領 ack のみでよい)
owner_close_approval:
  actor: coordinator role (ユーザー窓口。default binding: codex) または owner
  必須:
    - approval_source: standing_delegation | direct_owner
    - delegation_scope: normal_development (standing_delegation の場合)
    - carve_out_check: none | <該当理由>
    - review_journal
    - qa_journal / production_verification_journal (該当 gate がある場合)
    - commit_hash (origin 到達可能であること: `### Commit Hash Origin 到達可能性`)
  制約: Review Gate とは別 journal。standing_delegation は `### Owner Close Approval Delegation` の発動条件をすべて満たす場合のみ
close:
  us_close必須 (UserStory および親USを持たない単独issue):
    - 受け入れ確認
    - 指摘対応
    - 残留リスク
    - review結果 (US では US-level audit)
    - owner_close_approval (Review Gate とは別 journal)
    - commit_hash_record (origin 到達可能であること。local-only commit では close 不可: `### Commit Hash Origin 到達可能性`)
    - close判断
  task_close必須 (US 配下の Task / Test / Bug):
    - implementation_done journal (検証結果・残リスクを含む)
    - commit_hash_record (commit を伴う場合。origin 到達可能であること: `### Commit Hash Origin 到達可能性`)
    - 未完 scope / 残リスクの親US引き継ぎ記録
    - per-issue Codex review: 不要 (us_level_audit.task_level例外 に該当する場合を除く)
    - 制約: US audit が journal から replay できない task close は invalid。US audit で gap が見つかれば reopen する
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

### Review Generation Marker Contract v2

review-gate の structured marker が callback generation fence へ供給する review generation identity の producer 契約 (Redmine #13974)。この契約の exact な field 定義 — review_request / review (review_result) marker が持つ `head` (reviewed target_head) / `req` (答えた review_request journal) / `conclusion` (explicit approved|changes_requested)、source_sequence authority (Redmine provider が返す review_result journal id)、missing / mismatch / malformed / drift / 旧 generation の zero-send terminal disposition、approval write の observation↔marker identity exact-match — は上記 `### Gate Schema` の `review_request` / `review` gate の `structured_marker` を正本とし、本節はそれを複製しない。本節はその契約の named anchor であり、次の不変条件を固定する:

- generation identity を散文 (commit prose 等) から組み立てない。fence は structured marker の machine-readable field のみを読む。
- callback は lane generation だけでなく review generation の head / req / conclusion を連言する。missing / mismatch / malformed / drift / 旧 generation の review callback は discovery 0-enqueue かつ action-time で zero-send terminal (retry 0)。
- approval write は generation-admission observation と marker write target の identity を exact-match し、不一致は write 0。
- marker は canonical renderer (`render_gate_note` / `render_workflow_event_marker`) が付与する。手書き marker も同一 grammar に従う。
- skill reference は本節への pointer のみを持ち、field key・必須 gate 組合せ・disposition・source_sequence authority を複製しない。

### Hibernate Evidence Marker Contract

lane を自動で hibernate してよいかの basis conjunct が読む durable evidence marker の producer 契約 (Redmine #14219)。auto-hibernate の根拠は coordinator による全条件の再宣言ではなく、**各 authority が自分の権限で書いた durable event** である。したがって evidence marker は、どの lane の・どの generation の・どの commit についての主張なのかを machine-readable に自己束縛しなければならない。

- **共通 lane envelope。** evidence を成すすべての marker は `workspace=<workspace id>` / `lane=<lane id>` / `lane_generation=<正の整数>` を持つ。commit について述べる evidence はさらに `head=<full commit SHA>` を持つ。issue id だけの correlation や現行 lifecycle row からの補完で lane / generation を補わない — それは旧 generation の evidence を現 generation へ昇格させる。
- **evidence 種別 (closed vocabulary)。** `review_result` (既存 review gate marker へ envelope を additive 付与) / `integration_disposition` / `required_ci_green` / `dogfood_delegated` / `park_declared`。
- **integration_disposition は source head と integration head を分離する。** `head` は review 対象の lane / source head、`integration_head` は integration branch 上で統合を証明した exact commit、`integration_branch` は統合先 ref、`disposition` は `merge` | `patch_equivalent`。patch-equivalent / cherry-pick では両者が別 commit になるため、単一の head では統合を証明できない。`explicit_deferral` / `integration_blocked` は正当な durable disposition だが、統合済みの主張ではないので evidence にはならない。
- **CI / dogfood / park は callback-required gate ではない。** generic `workflow-event` evidence として記録し、callback を要する gate 語彙を拡張しない (余計な callback を生む)。`required_ci_green` は `workflow=<workflow / check identity>` と `run=<run id>` と `conclusion=success`、`dogfood_delegated` は委譲先 `release_issue` と `acceptance=<acceptance / resume anchor>`、`park_declared` は envelope に加えて **同一 journal に governed な fixed-field parked-state journal が完全な形で記録されていること**を要する。ここでいう完全な形とは skill `## Sublane 完了 guardrail` の固定 field shape のうち parked-state が使う subset — `state: blocked` / `durable_anchor` / `callback_result` / `blocked_by` / `resume_condition` / `resume_owner` — の全部であり、**field 名の存在だけでなく同 shape が定める値域に従っていること**を要する: `callback_result` は `sent | blocked | not-attempted`、`resume_owner` は `coordinator`、`durable_anchor` は **当該 park 宣言 journal 自身**を指す `#<issue_id> j#<gate_journal_id>` である (同じ issue の別 journal では「この park state の callback outcome」にならない)。任意の文字列を許すと、field 名が揃っているだけの記録が park basis になる。
- **callback outcome は token ではなく記録で確認する。3 outcome すべてに記録義務がある。** 正本は skill `### Callback outcome journal テンプレート` であり、field 名もそこに定義済みである — `target` / `result` (parked-state 側の畳み込み綴りは `callback_result`) / `on sent` / `on blocked` / `on not-attempted`。auto-hibernate evidence として読むときは、この template の綴りで次を確認する。**各 field / 各部が「何を証明するか」を固定し、record 全体を横断検索しない** — 部を分けただけで検査が横断のままだと、retry command 中の pane が candidate 行の代わりを務めてしまう。
  - **全 outcome (outcome 分岐の前に 1 度だけ適用する)**: `target` が **coordinator を名指した上で**、natural token `coordinator` そのものか、解決済み pane 1 つを指すこと。部分文字列一致 (`noncoordinator`) や、coordinator と名乗らない任意 pane (`same-lane worker w3F:p3`)、複数 pane は通さない
  - **`on sent`**: delivery command を **1 つの invocation そのものとして読む** (存在確認ではない)。**prefix wrapper・shell control operator (`&&` `||` `;` `|` redirect / command substitution)・前後の別 command は fail-closed** — 包まれた command は実行されなかった command である。許すのは template が定める **exact な label 語彙** (`retry command:` / `command:` / `retry:`) のみで、**任意の語列 + colon を label と見なさない** (`echo command:` は wrapper である)。label を剥がした command component は **その先頭 token が CLI entry point** であり `handoff send` が続き、かつ **canonical CLI grammar で実行可能な 1 invocation** であること — required option (`--to` / `--source` / `--kind`)、choices、未知 argument、値の型、`--flag value` と `--flag=value` の両 spelling、canonical anchor 規則 (redmine は `--issue`+`--journal`、asana は `--task-id`+`--comment-id`|`--anchor-url`、cross-source field 禁止) を含む。**CLI が実行を拒否する token 列は、何も配送していない**。command の字句解釈は shell と同じで、**tokenization と boundary 認識は単一の字句 authority で行う** — record 全体を 1 度 lex し、部の境界は bare な separator token であって、quoted / escaped separator は token 内容に残る (quoted argument は 1 値、unclosed quote と command token 中の control operator / substitution は fail-closed)。**argparse 通過は delivery ではない**: send 時の zero-send 前提条件 (custom→summary / `--select`↔明示 `--target` 排他 / `--target-project`→`--target-repo` 必須) は **単一の共有 authority (`handoff_send_semantics.send_semantic_gap`)** が担い、canonical call site と evidence 読取の両方がそれを呼ぶ — 個別条件を読取側で列挙し直さない (列挙は drift する)。`--mode pending` は composer に置くだけで submit しないため `sent` の証跡にならず (retry の replay も同様)、`--kind custom` は `--summary` を伴って初めて実行可能である。long option の **abbreviation は evidence では fail-closed** — CLI 自体は受理するが、略記は同一 logical option の相反重複 (`--ki A --kind B`) を conflict 検出から隠すため、evidence は unabbreviated 形で書く。`--to` の実効値が `codex`、`--target` の実効値が `target` field と一致すること。**同じ flag が相異なる値で繰り返された command は fail-closed** (`--to codex --to claude` の実効 receiver は claude である)。さらに **command の anchor / kind / receiver から canonical `build_marker` が合成する marker と、観測した landing marker が全 field exact 一致**すること (実行可能でも別 anchor / 別 kind の command は別の marker を生成するため、他 handoff の marker 描画を本 callback の証跡にできない)。観測 marker 自体も canonical producer の必須 field (`source` / anchor / `kind` / `to`) をすべて持ち、**当該 issue + 当該宣言 journal + `to=codex`** であること (角括弧 token の形だけでは配送証跡にならない)。**marker 内で同じ key が相異なる値で 2 度宣言されている場合、および同一 record 内に相異なる handoff marker が併記されている場合は fail-closed** — 2 つのことを言う token は何も証明しない (**完全に同一の marker の重複は 1 件に畳んでよい**。governed field と同じ collapse / conflict 規則)。`source` は当該 durable source、`kind` は canonical handoff kind 語彙、`to` は receiver 語彙に **exact literal で**照合する — canonical vocabulary は lowercase literal であり CLI 自身が `--to CODEX` を invalid choice で拒否するため、case を畳んで受理すると **producer が生成し得ない token** を証跡にしてしまう
  - **`on blocked`**: `/` 区切りちょうど 3 部。第 1 部=理由 (evidence がそれを兼ねない)、第 2 部=candidate pane 行、第 3 部=**実際に replay される retry command** — sent と同じ invocation 規則 (wrapper / shell control 不可、実行可能 grammar) で `handoff send` かつ `--to codex`、**anchor (`--source` / `--issue` / `--journal`) が当該 park 宣言そのもの**を指し (別 anchor の retry は別 ticket への handoff であって本 callback の replay ではない)、`--target` の実効値が **第 2 部の candidate のいずれか**、かつ `--target-repo auto`。ここも同じ flag の相異なる重複は fail-closed とする
  - **`on not-attempted`**: 明示理由
  `sent` を token だけで通すと「誰にも渡されないまま park された」状態を `sent` 経由で再導入することになる (この節の以前の版は `sent` に追加要求無しと書いていたが、正本 template と矛盾するため撤回する)。**正本に綴りが定義されている以上、実装側で独自 alias を作らない** — 独自 alias は正本どおりの記録を落とし、正本に無い落書きを通すという反転を招く。
- **governed field は exactly-one。** 同一記録内で同じ field が相異なる値で複数回宣言された場合、どちらが authoritative かの順序根拠が無いため typed conflict として拒否する (完全に同一の重複は 1 件に畳んでよい)。marker の重複解決と同じ規則である。部分集合で足りるとしない: parked state は handoff-worthy な `blocked` state であり、`callback_result` を欠く記録は同 guardrail が防ごうとした「誰にも渡されないまま park された」状態そのものだからである。
- **verdict だけでなく、その verdict を後から検証できる記録を指す。** run id だけでは「どの required check が green だったか」を述べておらず、無関係な green run が条件を満たしてしまう。同様に delegation は `release_issue` 側に **source issue と exact SHA を伴う受領記録**があって初めて委譲であり、それが無ければ委譲元の一方的な意思表明にすぎない。auto-hibernate の evidence は、発行者が単独で書けてしまう主張ではなく、**照合先のある記録**を指すこと。
- **review_result は「どの review generation に答えたか」を伴って初めて evidence になる。** `### Review Generation Marker Contract v2` の `req` が必須である。答えた request の同定は既存の canonical 規則と同一とする: **result journal より厳密に前にある最大の `review_request`**。同一 journal は含めない (id は単調であり、答えが問い自身と同じ記録を共有することはない)。`req` はその request を指し、両者の `head` が一致すること。さらに **result より後に新しい `review_request` があれば、その approval は自分の round では真正でも現在の evidence ではない** (再 review が開いている)。**同一 journal に result と `review_request` が併記されている記録は、旧 round の結論と新 round の開始を同時に主張しており順序で解けないため、それ自体を contradictory として拒否する** (既存 glance の canonical review outcome と同じ扱い。前後どちらの規則でも捕まらない位置にあたるため、明示的に閉じる)。req を欠く / 別の request を指す / 後から現れた request で遡って有効化される / head が食い違う approval は fail-closed とする。相関規則を第二定義として作らず、既存 canonical 規則と同じ条件を使うこと。
- **issuer は権限を持つ actor に固定し、marker の自己申告ではなく記録の author から解決する。** `review_result`=same-lane reviewer / gateway、`integration_disposition` / `required_ci_green` / `dogfood_delegated`=coordinator、`park_declared`=当該 lane の implementation worker。他 actor が代理で合成しない。delegation を成果 (dogfood 成功、CI green) と読み替えない。**marker が gate 名を名乗ること自体は権限の証明にならない** — 誰でも同形の marker を書けるため、authority は durable record の author 側から取り、解決できない author は typed zero とする。さらに **role token だけでは契約を表現できない**: `review_result` は *same-lane* gateway、`park_declared` は *当該 lane の* worker という束縛なので、解決した issuer 自身が `workspace` / `lane` / `lane_generation` を持ち、evidence が宣言する envelope と exact 一致することを要求する (別 lane・旧 generation の同role writer は mismatch)。role の解決根拠となった durable record を anchor として **role を問わず全 issuer に**要求する — source system の author id だけでは actor を一意に定められない運用があるため (同一 user が複数 role の journal を書く構成)。lane exact-match が要るのは lane-scoped role (`review_result` / `park_declared`) のみだが、これは **権限の scope の話であって「書き手が誰か特定できたか」の話ではない**。coordinator は workspace 級の権限なので lane 一致は課さないが、anchor 無しの bare role token を解決済みとしては扱わない (integration / CI / dogfood の 3 gate が丸ごと素通りする)。
- **candidate head は basis に依らず full commit SHA である。** hibernate の action intent は exact head へ束縛されるため、head-bearing conjunct を持たない basis (dependency park) でも malformed head は zero-actuation とする。「他の conjunct と突き合わせた結果たまたま弾かれる」ことに依存しない。
- **fail-closed。** missing / malformed / 非 full-hex head / 非正 generation / cross-lane / 旧 generation / head mismatch / 相反する重複 marker / 未相関の review conclusion / 権限のない actor による発行 / 解決できない author は、「条件が未充足」ではなく typed unknown-or-stale として zero-actuation とする。散文からの補完は禁止。同一種別の読めない marker を読み飛ばして別の marker を採ることもしない — 新しい記録が古い記録に負けるため。
- **renderer は parser が拒否するものを書かない。** 非正 generation / 非 full-hex head / 空の workspace・lane / marker separator (`:` `]` `[` 空白) を含む値は、書き込み時点で producer error として拒否する。読めない marker を durable に残すと「evidence が黙って成立しない」状態になり、separator を含む値は別 field へ分裂して marker を切り詰める。
- **additive であり既存 projection を変えない。** envelope / 追加 field は既存 consumer (review generation fence、workflow glance の integration disposition projection) から不可視であり、marker を持たない従来の記録の解釈も変えない。従来 marker は既存用途では有効なままで、auto-hibernate evidence としてのみ不成立となる。

### Review Finding Verdict Obligation (迎合禁止)

<!-- mozyo-bridge:activation:always id=no-sycophancy-evidence-provenance digest="迎合せず結論を述べ、review finding には根拠の出所を明示する。正本: central preset `### Review Finding Verdict Obligation (迎合禁止)` / `### 根拠出所分類`。" -->

review は正しさの最終保証ではない。誤った指摘を検証なしに実装することは、正しい指摘を無視することと同種の欠陥である。実装者は review / review_result の指摘事項を **必ず** 独立検証し、finding ごとの verdict (accepted / disputed) を durable record に残してから対応する。本 preset の要求は「上申してもよい」(許可) ではなく「妥当性判断を記録せよ」(義務) である。

- 検証は code / docs / 事実に基づく。reviewer の権威・言い回しの強さ・修正の手軽さは verdict の根拠にならない。
- disputed の上申経路と必須 field は既存の `design_consultation_dispute` gate をそのまま使う。新しい gate 名 / transport kind は作らない。
- dispute は同一 finding につき 1 往復まで。合意不能は owner 判断へ escalate し、その窓口は `### Claude Owner-Question Bypass Prohibition` に従い coordinator role に集約する (default binding: codex)。
- 逆振れの抑制: evidence を欠く dispute、taste の相違のみを理由とする dispute は invalid。正しい指摘への再反論で owner 判断コストを浪費しない。

### Late-Finding Full-Surface Adversarial Sweep Escalation

`### Review Finding Verdict Obligation` は 1 finding の妥当性を規律するが、**同一 subsystem で late authority finding が round をまたいで反復する** 場合を扱わない。per-finding の再 review を続けると、同じ authority 面で毎 round 別の欠陥が漏れる whack-a-mole になる (差分修正を 6+ round 重ねてから不変条件を 1 構造で強制へ切替えた事例)。owner decision (Redmine #13967) はこの escape を標準化する: 同一 subsystem で late authority finding が反復したら、次 round を per-finding 再 review から **full-surface adversarial sweep** へ deterministic に昇格する。本規則は review / close authority を **緩めない** — escalation は review scope を*足す*だけである。

```yaml
late_finding_escalation:
  用語:
    late_authority_finding: |
      blocking かつ authority-bearing (workflow / routing / approval / send-safety /
      fail-closed authority に触れる) な finding で、当該 subsystem の先行 round の後に
      surface したもの (per-finding 再 review が取りこぼした late defect)。taste / style
      の finding、初回 round で捕捉された finding は late authority finding ではない。
  deterministic_trigger:
    - subsystem ごとに late authority finding を持つ **distinct round 数** を数える
      (1 round は複数 finding があっても 1 と数える)
    - その count >= threshold (default 2 = 「反復」) なら、次 round を
      full_surface_adversarial へ昇格する。それ未満は per_finding_rereview のまま
    - history が読めない subsystem は escalation 側へ倒す (full-surface は stricter で
      あり bypass ではない)。qualifying finding ゼロから escalation を捏造しない
  review_mode:
    - per_finding_rereview: 指摘 finding だけを再 review する通常 round
    - full_surface_adversarial: 当該 subsystem の全面を、finding 単位でなく敵対的に
      掃く round。不変条件の全 edge を列挙してから審査する
  record: full_surface_review_escalation journal (下記 Journal Templates)
  projection: `mozyo-bridge workflow review-escalation` (read-only。subsystem ごとの
    round history から escalate 要否と next_round_mode を deterministic に算出する)
  authority不変:
    - escalation は review scope を足すのみで、Review Gate / Close Approval Separation /
      dispute round cap / owner close approval を緩めない
    - full-surface へ昇格しても、finding ごとの verdict 義務 (`### Review Finding Verdict
      Obligation`) と根拠出所 (`### 根拠出所分類`) は維持する
```

### 根拠出所分類 (Evidence Provenance)

gate journal に記録する根拠 — review finding、implementation_request / dispatch decision に載せる指示の背景、design consultation の判断材料 — は、確からしさ (`指摘事項_分類` の 事実 / 仮説) とは別に、**誰の権威に基づくか (出所)** を明示する。出所軸と確からしさ軸は直交する: 「事実だが agent 判断」も「仮説だが owner intent 由来」も成立する。ラベルではなく **durable anchor の有無が証拠の重みを決める**。

```yaml
根拠出所:
  分類 (4種で固定。細分化しない):
    owner_intent: owner の意思。durable anchor 必須 (journal id または issue description の原文要点)
    documented_rule: 文書化された規約。path + 節名を併記
    agent_judgment: reviewer / 実装者自身の判断・推論。反論可能な主張として扱う
    hearsay: 未記録の owner 発話 (伝聞)。伝聞であることの明示必須
  伝聞降格:
    - hearsay は単独で 要修正 / block / gate 成立の根拠にならない
    - hearsay を根拠に使う場合は先に記録する: ユーザー窓口 (coordinator role、default binding: codex) が owner に確認し、journal または issue description 原文要点へ記録してから owner_intent として使う
    - anchor を欠く owner_intent 主張は hearsay として扱う (ラベルではなく anchor が重みを決める)
  一般化:
    - `### Claude Owner-Question Bypass Prohibition` の「pane 観測の owner 生回答は未確定 input」を、close approval に限らず全 agent・全 gate の根拠へ一般化した規則である
    - agent が自 pane で観測した owner 発話も、引用 + 出所を durable record に記録して初めて owner_intent になる
```

### Commit Hash Origin 到達可能性

Implementation Done / Review Request / Review / owner_close_approval / Close の各 gate に記録する commit hash は、durable anchor として扱う前に **origin (共有 remote) から到達可能でなければならない**。未 push の local-only commit はサーバー側から原理的に検出できず、後続の監査・close・引き継ぎが replay できない anchor になる。

push は 2 層に分かれる。**実装者の push は issue / lane branch に限る** (anchor 到達性はそれで満たされる)。**integration branch (origin/main / release branch) を前進させるのは review 承認後の coordinator** であり、その統合判断を integration disposition として記録する。実装者の「記録前に push せよ」を integration branch への直 push と読み替えない。

```yaml
origin到達可能性:
  対象gate: [implementation_done, review_request, review, owner_close_approval, close]
  実装者責務:
    - Implementation Done / Review Request で commit hash を記録する前に、その commit が共有 remote へ push 済みで到達可能であることを確認する
    - push する ref は issue / lane branch に限る。integration branch (origin/main / release branch) を実装者が直接前進させない (統合は coordinator の integration disposition)
    - 確認は read-only (例: `git fetch` 後の `git branch -r --contains <hash>`、または `git merge-base --is-ancestor <hash> origin/<branch>`)
    - 到達不能なら記録前に push する。push できない場合は gate を blocked とし理由を journal に残す。未 push の hash を anchor として記録しない
  main_unit例外実装:
    - main lane / main-unit での例外実装 (dispatch decision に例外理由が記録された場合) でも、実装者は primary checkout の integration branch 上で直接 commit せず、issue branch を切って作業し branch を push する
    - coordinator と実装者が同一 checkout を共有する場合、checkout の branch 切替が衝突しうる。commit 前に current branch を確認し、可能なら専用 worktree を使う。誤って integration branch に乗った commit は push せず issue branch へ移し、correction を journal に残す
  統合責務 (integration disposition):
    - review 承認後、coordinator が integration branch への統合を merge | patch_equivalent | explicit_deferral のいずれかとして判断し、統合 commit 群・merge 方式・検証結果を integration journal に記録する
    - merge の標準は ff-only (`git merge --ff-only`)。non-ff (merge commit / rebase 統合) を使う場合は理由を integration journal に記録する
    - 統合後の Review Gate 済み commit hash が rebase 等で origin 到達不能になった場合は、re-anchoring correction journal で新 hash へ再接続する (silent edit をしない)
    - integration journal を自動判断の evidence として使う場合は、`### Hibernate Evidence Marker Contract` の `integration_disposition` marker を additive に埋め込む (source head / integration_head / integration_branch / disposition を分離した machine-readable 形)。marker を持たない従来の記録は既存 projection では有効なままで、自動判断の evidence としてのみ不成立とする
  監査者責務:
    - Review Gate で対象 commit 群が origin 上に到達可能であることを remote verification として確認し、結果を review journal に残す
    - 到達性が確認できない場合は事実指摘ではなく blocker として扱い、close へ進めない
  close制約:
    - owner_close_approval / Close gate は origin 到達不能な commit hash では成立しない
    - local-only commit に対する close は invalid。reopen + correction journal を起票する
  禁止:
    - 自動 push/pull 機構の導入。push は実装者の明示操作のままとし、gate 検証は read-only な到達性確認に限る
    - 自動 merge / auto-integration 機構の導入。統合は coordinator の明示操作と integration journal 記録のままとする
```

### Codex Direct Edit Gate

通常開発の実装 file (例: {{IMPL_FILE_PATH_EXAMPLES}}) を Codex (監査者) が直接編集してよい条件は狭く制限する。`codex_direct_edit` gate journal が active issue に明示存在する場合に限り、Codex は `allowed_paths` だけを直接編集できる。gate 未存在で `do it` / `対応して` / `実行せよ` / `implement it` / `お願いします` 等を受けても、Codex は通常実装を Claude へ handoff する。短い命令は file edit 許可ではない。

同じ gate 要件は **ガードレールおよび docs/catalog 周辺** にも適用する。`AGENTS.md`、`CLAUDE.md`、`.mozyo-bridge/rules/**`、`.codex/skills/**`、`.claude/skills/**` を Codex が直接編集するには、active issue に `codex_direct_edit` gate journal が存在し、`allowed_paths` に該当ガードレール path が明示されている必要がある。chat 上の「ガードレール変更を明示」「Codex でやって」等の短い指示は、それ単独では gate 成立条件を満たさない。Claude へ handoff し、Claude が実装→Implementation Done Gate→Review Request Gate を経由するのが default。

ただし `### Repo-Local Guardrail Autonomous Lane` で定義する path 集合 (default では `vibes/docs/rules/**` / `vibes/docs/logics/**` / `vibes/docs/specs/**` / `.mozyo-bridge/docs/catalog.yaml`) は本 gate の例外として **Codex 自律編集を許可する carve-out** である。`codex_direct_edit` gate journal は不要。代わりに `codex_autonomous_edit` journal を edit と同時または commit 直後に残す。distributed surface (上述の `AGENTS.md` 等) は引き続き本 gate の対象。

`.mozyo-bridge/docs/file_conventions.generated.yaml` をはじめとする generator 出力は **誰も手編集しない** (Claude / Codex / owner いずれも不可)。catalog を変更し、`mozyo-bridge docs generate-file-conventions` で再生成、`--check` で drift 確認の流れに乗せる。手編集された場合は generated 物を破棄し、catalog 起点で再生成する。

direct edit を行った場合、適用した例外、ユーザー指示の引用、変更 files、verification、follow-up review 要否を Redmine journal に記録する。例外なき監査者の通常実装 (`着手:codex` / `実装完了:codex` / `担当:codex` / `codex対応`) は invalid marker として扱い、reopen + correction journal を起票する。ガードレール / docs / catalog scope での gate 不在 commit (例: chat の短い指示を根拠に Codex が `AGENTS.md` / `CLAUDE.md` / `.mozyo-bridge/rules/**` / `README.md` を直接 commit した場合) も同じ correction flow に乗せる。autonomous lane の path はこの correction の対象外だが、`codex_autonomous_edit` journal を欠いた commit は監査記録不足として follow-up correction journal を起票する。

### Codex Pre-Edit Classification Gate

Codex は `apply_patch`、新規 file 作成、既存 file 更新、git commit の前に、対象変更がどの実装主体に属するかを分類する。分類を作業後に思い出して correction する運用を標準にしない。

- repo 内の正本成果物を作成・更新・削除する作業は、拡張子や内容種別に関係なく **実装成果物** と扱う。Markdown、HTML、調査メモ、ドラフト、表、taxonomy、report、runbook、設定例も、repo に置かれて後続 agent / user / release が参照するなら実装成果物である。
- 「コードではない」「一時メモに見える」「文章だけ」「commit hash を journal に書く必要がある」という理由は、Codex direct edit の根拠にならない。commit 要件は実装主体の分類を通過した後にだけ発動する。
- Codex が直接編集できるのは、対象 path が `### Repo-Local Guardrail Autonomous Lane` に入っている場合、または active ticket に `codex_direct_edit` gate があり `allowed_paths` に対象 path が列挙されている場合だけである。
- ユーザーが `mozyo-bridge`、Claude 協業、handoff、agent 分担を話題にした場合は、Codex direct edit を default にしない。default は Claude handoff とし、autonomous lane または `codex_direct_edit` gate が確認できた場合だけ direct edit に切り替える。
- Codex が direct edit 例外を使う場合は、edit が land する前、または autonomous lane では edit と同時 / commit 直後に durable record を残す。record には例外種別、対象 file、理由、検証方法、follow-up review 要否を含める。
- Codex が誤って先に成果物を作った場合、その成果物を完了扱いにしない。correction として事実、影響範囲、採用・修正・破棄の判断を durable record に残し、Claude 実装 / 採否判断から Codex audit へ戻す。

### Repo-Local Guardrail Autonomous Lane

repo-local guardrail の育成は project の価値そのものであり、毎回 owner pre-approval や個別 `codex_direct_edit` gate を要求する運用は UX と growth を阻害する。本 preset は **Codex Direct Edit Gate の carve-out** として **Repo-Local Guardrail Autonomous Lane** を定義する。lane 内の path は Codex 自律編集を許可し、edit と同時または commit 直後の durable journal で監査可能性を担保する。

#### 既定 path 集合

```yaml
repo_local_guardrail_lane_defaults:
  - vibes/docs/rules/**
  - vibes/docs/logics/**
  - vibes/docs/specs/**
  - .mozyo-bridge/docs/catalog.yaml
```

target project は Project-Local Additions または project-local rule (例: `vibes/docs/rules/codex-autonomous-guardrail-lane.md`) で patterns を **拡張または縮小してよい**。ただし以下は本 lane に含めない:

- `AGENTS.md`, `CLAUDE.md` (Codex / Claude entrypoint routers)
- `.mozyo-bridge/rules/**` (distributed governance package artifacts)
- `.codex/skills/**`, `.claude/skills/**` (skill 配布先)
- `skills/**`, `plugins/**` (canonical skill + marketplace mirror)
- {{LANE_DISTRIBUTED_SURFACE_FULL}} (implementation lane)
- `src/mozyo_bridge/scaffold/presets/**` (packaged preset / router templates)
- generator 出力 (`.mozyo-bridge/docs/file_conventions.generated.yaml` 等)

これらを lane に含める project-local override は preset 提供責任の範囲外として **明確に reject** する (target project は本 preset の `### Codex Direct Edit Gate` をそのまま適用する)。

#### `codex_autonomous_edit` Journal

lane 内で Codex が edit した場合、active Redmine issue に以下を記録する。pre-approval は不要 (post-or-concurrent 記録で足りる)。

```yaml
codex_autonomous_edit:
  actor: codex
  必須:
    - lane: autonomous
    - changed_paths
    - intent
    - verification
    - commit_hash (commit 後; staging で止めた場合は pending: staged-not-committed)
    - follow_up_review_required: true|false
```

journal を欠いた lane 内 commit は監査記録不足として correction journal を起票する。

#### 必須検証 Command

lane 内 edit では **commit 前** に以下を実行し、結果を `codex_autonomous_edit` journal の `verification` フィールドに残す。

共通:

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `git diff --check`

`.mozyo-bridge/docs/catalog.yaml` を変更した場合は追加:

- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`

drift が出たら `mozyo-bridge docs generate-file-conventions --repo .` で regenerate してから再 commit する。検証 command がいずれか fail したら commit せず、journal に `verification_failed` を記録し、Claude / owner に escalate する。

#### lane を起動しない条件

以下が一つでも該当する場合、Codex は lane を使わず Claude handoff または `codex_direct_edit` gate に escalate する。

- 変更が lane 範囲を超える (例: `vibes/docs/rules/foo.md` を直すついでに {{LANE_BREAKAWAY_EXAMPLE_PATH}} も触る必要がある)。
- 変更が central preset / 配布 surface に影響する。
- 変更が credential / token / 個人情報 / 認証フローに触れる。
- product owner の以前の指示と矛盾する変更を入れる必要がある。
- 同一 issue で過去に同じ path について `要修正` / `block` review を受けている。

#### Workflow-Change Verification

本 lane policy 自体は workflow / guardrail 変更であるため、policy 確立後の **次の通常開発タスク** で本 lane が想定通りに機能することを workflow-change verification として確認する。

- 検証 task は本 lane を直接変更しない通常開発タスクとする。
- 検証 task では Claude が実装し、Codex は lane を実際に 1 回以上利用して repo-local guardrail を更新する。`codex_autonomous_edit` journal が破綻なく回ることを durable record として残す。
- 結果を Redmine issue に記録する。lane policy 自身に gap が見つかれば follow-up issue を起票する。

### Owner Close Approval Delegation

base の `Close Approval Separation` を、窓口 = coordinator role (default binding: codex) として運用する。適用単位は `### US-Level Audit Model` に従い UserStory (または単独 issue) である。Review Gate approval (US では US-level audit approval) 後の owner クローズ可否確認、owner_close_approval journal の記録、Close Gate までは **coordinator role 側で完結** させる (default binding では codex 側)。実装者 role (default binding: claude) は Review Gate approval を受領したら close 条件の充足状況を Progress Log に記録して待機し、owner 承認を自分の pane で収集しない。

owner は通常開発タスクの close approval を coordinator role (default binding: codex) へ **事前委任 (standing delegation)** できる。委任の正本は本 preset であり、target project は採用可否だけを Project-Local Additions に記録する (詳細規則を workspace 側へ複製すると drift する)。

```yaml
owner_close_delegation:
  scope: normal_development
  発動条件 (すべて必須):
    - review_gate: approved かつ open findings なし
    - required_verification: green (Required Verification を満たす)
    - commit_hash: 記録済み
    - carve_out: 非該当 (下記一覧)
    - residual_risk: owner 判断を要するものなし
  記録: owner_close_approval journal に approval_source: standing_delegation を明示
  禁止: Review Gate journal と同一 journal にまとめること
```

以下の **carve-out** に一つでも該当する issue は standing delegation の対象外であり、owner の直接承認 (`approval_source: direct_owner`) を要求する:

- release / tag / publish / package distribution
- guardrail / preset / router / skill / scaffold rule 変更
- credential / secret / auth / permission / billing / 外部 service 設定
- destructive operation / data 削除 / migration
- production verification または外部副作用を伴う操作
- legal / compliance / security-sensitive な変更
- 仕様・scope・stakeholder 判断が未確定な issue
- cross-project / cross-workspace ownership や session registry の正本変更
- issue または parent に owner_approval_required 相当が明示されたもの

carve-out 該当性の確認結果は owner_close_approval journal の `carve_out_check` field に残す (`none` または該当理由)。該当か判断に迷う場合は delegation を使わず owner 直接承認に escalate する。

### Direct Request Triage (governed)

base の `Direct Request Triage` を、窓口 = coordinator role / triage role = coordinator role (default binding: codex) として適用する。ユーザーが実装者 role の pane (default binding: claude) に直接作業を依頼した場合、実装者は triage-pending issue を即起票し、coordinator role へ精査を handoff する (`--kind design_consultation` または `custom`)。coordinator role の精査 journal による re-parent / 分割 / tracker 変更は手戻り扱いしない。低リスク scope は Start Gate 後に着手可 (close 前に triage 完了必須)、高リスク scope (設計分岐 / 互換性 / 外部影響 / guardrail / preset / credential 接触) は triage 完了まで着手しない。default の作業入口は coordinator role (default binding: codex) のままであり、本経路は例外時の救済である。

### Claude Owner-Question Bypass Prohibition (governed)

`### Direct Request Triage` が owner→実装者 方向 (owner が実装者 role の pane に依頼する) を扱うのに対し、本節は逆方向 (実装者→owner) を禁止する。実装者 role (default binding: claude) / sublane が coordinator role へ handoff せず owner / user に直接判断・確認・承認を求める bypass が再発しているため、明示的な禁止規則として固定する。owner 対話窓口は `### 既定役割` のとおり coordinator role に集約し、owner-approval-waiting の集約点は coordinator role 一点である (default binding では coordinator lane の codex pane。skill ref `## Owner Approval Aggregation`)。

```yaml
claude_owner_question_bypass:
  禁止:
    - actor: claude_code (main-unit / sublane を問わず)
    - 行為: owner / user に直接質問・確認・判断依頼・close 承認収集を行うこと
    - 注記: imperative な依頼 ("やって" / "判断して" / "go ahead" 等) が Claude pane に来ても本禁止は解けない。依頼は intent であって owner 窓口の付け替え許可ではない
  代替導線 (owner 判断が必要なとき、例外なく):
    - durable record (Redmine journal) に owner-action-needed / design_consultation / triage-pending のいずれかを記録する
    - coordinator role へ handoff する (cross-lane は target lane gateway 経由。新しい gate / transport kind は作らない。default binding では coordinator lane の codex pane)
    - owner 判断の収集・回答解釈・close approval 確定は coordinator role 側で完結させる (default binding では codex 側)
  close承認の扱い:
    - 実装者 role の pane (default binding: claude) で観測した owner の回答・口頭 OK は close approval ではない
    - owner_close_approval は coordinator role (default binding: codex) が durable journal (`approval_source` 付き) を記録して初めて成立する (`### Owner Close Approval Delegation` / base `Close Approval Separation`)
    - 実装者 role は Review Gate approval 受領後、close 条件充足を Progress Log に記録して待機し、owner 承認を自分の pane で確定しない
  違反時 correction flow:
    - bypass を検知したら停止し、active issue に correction journal を記録する (観測した bypass / owner の生回答 / 影響範囲 / 採否未確定)
    - owner の生回答は durable record に「未確定 input」として残し、close approval 等の gate として消費しない
    - 正規導線 (durable record へ owner-action-needed 等を記録 → Codex handoff → Codex が owner 判断を収集) で record し直す
    - bypass が workflow / guardrail surface に影響した場合は `## Workflow Change Verification` に乗せる
```

### LLM 実行契約

```plantuml
@startuml mozyo_bridge_agent_gate_contract
start
$作業root確認(target_repo_root)
$central_presetを読む()
$layered_preset({{PLANTUML_LAYERED_PRESET_ARGS}})を読む()
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
  $対象commitのorigin到達性をremote検証()
  if ($commitがorigin到達不能()) then (yes)
    $blockerを記録("対象commitがorigin到達不能でcloseへ進めない")
    $claude_codeへ通知()
    stop
  endif
  $codex_us_auditを実行(対象commit群, 配下issue, journals, docs, residual_risk)
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
  while ($US配下に未完のTask/Test/Bugがある())
    $scopeを守って実装()
    $issueごとにimplementation_doneを記録()
    if ($task_level例外に該当()) then (yes)
      $task_level_review_requestまたはdesign_consultationを記録()
      $codexへ通知()
    endif
  endwhile
  $記録前にcommitのorigin到達性を確認()
  $USのimplementation_doneを記録()
  $US_audit_requestを記録(review_request gate)
  $codexへaudit通知()
endif
if ($close要求()) then (yes)
  if ($対象がUS配下のTask/Test/Bug()) then (yes)
    if ($implementation_done_journalあり() && $残リスク引き継ぎあり()) then (yes)
      $task_closeを記録()
    else (no)
      $close_blockedを記録("US auditがreplayできない")
      stop
    endif
  else (no)
    if ($review_gateあり() && $owner_close_approvalあり() && $commit_hash_recordあり() && $commit_hashがorigin到達可能()) then (yes)
      $close_gateを記録()
    else (no)
      $close_blockedを記録()
      stop
    endif
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
  - id: us_close_after_implementation_done_only
    条件: [issue:user_story_or_standalone, implementation_done:present, review_gate:missing]
    action: close禁止 (US-level audit が先)
  - id: close_without_owner_approval
    条件: [issue:user_story_or_standalone, review_gate:present, owner_close_approval:missing]
    action: close禁止
  - id: record_unreachable_commit_as_anchor
    条件: [gate:implementation_done_or_review_request, commit_hash:未push_origin到達不能]
    action: anchor記録禁止 (push後に記録、push不能ならblocked)
  - id: implementer_advances_integration_branch
    条件: [agent:実装者, push先:origin/main または release/integration branch]
    action: push禁止 (issue/lane branch へ push し、統合は review 承認後の coordinator の integration disposition に委ねる)
  - id: review_without_remote_verification
    条件: [agent:codex, gate:review, 対象commit:origin到達不能_または未確認]
    action: blocker記録 (事実指摘扱いしない)しcloseへ進めない
  - id: implement_review_finding_without_verdict
    条件: [agent:claude_code, review指摘事項:あり, review_finding_verdict_journal:missing]
    action: 指摘対応の実装・commit禁止 (finding ごとの verdict 記録が先)
  - id: accept_review_finding_without_verification
    条件: [verdict:accepted, 独立検証記録:なし]
    action: invalid verdict として correction journal を起票 (迎合は欠陥)
  - id: gate_on_hearsay_only
    条件: [gate根拠またはreview指摘: 未記録owner発話のみ, durable_anchor:missing]
    action: 単独根拠として扱わない (coordinator role 窓口 (default binding: codex) で記録して owner_intent 化するか、agent_judgment として再分類する)
  - id: close_on_local_only_commit
    条件: [gate:close_or_owner_close_approval, commit_hash:origin到達不能]
    action: close禁止 (local-only commit を anchor にしない); reopen+correction
  - id: claude_asks_owner_directly
    条件: [agent:claude_code, owner判断:必要, codex_handoff:missing]
    action: stopしdurable recordにowner-action-needed/design_consultation/triage-pendingを記録しcoordinator roleへ集約 (default binding: codex)
  - id: close_approval_from_claude_pane
    条件: [approval_source:claude_pane_observation, owner_close_approval_journal:missing]
    action: close approvalとして扱わない (coordinator role (default binding: codex) の durable journalが先)
  - id: task_close_without_replayable_journal
    条件: [issue:task_under_us, implementation_done_journal:missing_or_検証記録なし]
    action: close禁止 (US audit が replay できない)
  - id: us_close_with_unaudited_children
    条件: [issue:user_story, 配下issue:open_or_unrecorded, review_gate:recorded]
    action: close禁止 (audit 対象が確定していない)
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
mozyo-bridge docs validate --check-file-coverage {{COVERAGE_ROOT_BASH_EXAMPLES}}
mozyo-bridge docs generate-file-conventions --check
mozyo-bridge docs audit-impact --all-changed --check-generated
```

- **使用契約**: 作業対象 path が分かった時点で `mozyo-bridge docs resolve <path...>` を実行し、解決された docs 本文を **実装前 / review 前 / guardrail 変更前** に読んでから着手する。`docs resolve` は catalog の代替ではなく、その path に紐づく catalog エントリ (guardrail / spec / convention) を読むための入口である。
- resolver が失敗した (catalog 不在 / path 未解決 / command 実行不能) 場合は、読んだふりをせず停止するか、Redmine journal に未確認事項として記録してから進める。
- catalog 自体や file_convention pattern を変更した場合は validator と coverage check を通す。coverage roots の選択順序は **(1) CLI `--coverage-root`** が指定されていればそれ、**(2) catalog の `coverage_roots` field** が定義されていればそれ、**(3) validator 組み込み default**{{VALIDATOR_DEFAULT_QUALIFIER}}。CLI が catalog より優先される。project が該当 layer を持たない場合は missing root は `notice:` として印字されるだけで exit code には影響しない。project ごとの恒久指定は catalog 側に書く運用が望ましい。
- file_conventions 生成物 (project が採用している場合) を変える場合は generator を実行し、drift check を通す。
- staged commit 直前は `mozyo-bridge docs audit-impact --staged --check-generated` を通す。作業中の棚卸しでは `--all-changed`。
- いずれの command も `--repo <path>` で target repo を、`--catalog <path>` で catalog 位置を override できる。default は cwd / `<repo>/.mozyo-bridge/docs/catalog.yaml`。

これらは catalog が埋まっていれば即座に機能する。catalog が空でも tool 自体は valid catalog skeleton を accept するため、operator は段階的に埋められる。`--check-file-coverage` も {{COVERAGE_LAYER_LABEL}} の有無に関わらず安全に実行できる。

### 回答前 Doc 解決 (Answer-Time Resolution)

<!-- mozyo-bridge:activation:always id=answer-time-doc-resolution digest="設計・仕様・現状挙動を回答・断定する前に、質問ドメインの cataloged docs を catalog (`.mozyo-bridge/docs/catalog.yaml` / `docs resolve`) で解決して読む。memory / 直近 journal は pointer であり verdict ではない。正本: central preset `### 回答前 Doc 解決 (Answer-Time Resolution)`。" -->

上の使用契約は変更対象 path 起点 (実装前 / review 前 / guardrail 変更前) の義務である。それに加え、**owner / user への回答・断定・裁定**も doc 解決の対象とする。直近の裁定や agent の記憶が committed spec と乖離している場合、回答時に正本を読み直さない限り乖離は回答として再生産されるためである。

```yaml
回答前doc解決:
  対象: 設計・仕様・現状挙動・規約についての回答 / 断定 / 裁定 (実装作業の有無と無関係)
  義務:
    - 質問ドメイン (表示 / routing / identity / workflow 等) に対応する cataloged docs を
      catalog (`.mozyo-bridge/docs/catalog.yaml`) / `mozyo-bridge docs resolve` 経由で解決し、
      本文を読んでから答える
    - agent memory・直近 journal・直近裁定は pointer / 手がかりとして扱い、verdict にしない。
      committed spec と矛盾する可能性を照合してから断定する
    - 解決不能 (catalog 不在 / ドメイン対応 doc 不明) の場合は、読んだふりをせず
      「正本未照合のまま回答している」ことを回答内に明示する
  禁止:
    - memory / 直近 journal のみを根拠に「設計どおり」「仕様どおり」と断定すること
```

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
- `mozyo-bridge tests resolve` (module-to-test impact resolver) で変更 path から focused tests を解決し、commit 前に実行する (例: `mozyo-bridge tests resolve --staged --format targets | xargs python -m unittest`)。
- {{REQUIRED_VERIFICATION_TEST_CMD}}
- {{REQUIRED_VERIFICATION_LINT}}

impact resolver の推奨が fail-closed に `full` の場合は、full suite を実行するか、この commit では実行しない明示 carve-out (理由 + full をいつ実行するか。例: push 前 / CI full lane) を Redmine journal に記録する。無言の skip は verification record にならない。resolver が repo の source layout を解決できない project では推奨は常に `full` になるため、上記 authoritative test command の実行がそのまま要件になる。

`looks fine` は verification record ではない。command が走らなかった理由と、代替確認の内容を Redmine に残す。

## Journal Templates

```markdown
## Gate: codex_direct_edit
- role: 実装者
- direct_edit: true
- allowed_paths:
- reason:
- follow_up_review:

## Gate: review_request (US-level audit request; Task-level は例外時のみ)
- 対象US: #<us_id>
- 配下issue状態: (#<id> 状態 / implementation_done journal / 残リスク を issue ごとに列挙)
- implementation_done_journal: (US 自身の summary journal)
- commit_or_diff: (対象 commit 群)
- commit_origin到達: (対象 commit が origin 到達可能か: push済み確認方法 / 未push理由)
- changed_paths:
- review_focus:
- 根拠出所: (依頼理由・review_focus に載せる根拠: owner_intent j#<id>/原文要点 | documented_rule <path + 節名> | agent_judgment | hearsay)
- 未確認事項:
- receiver: Codex
- receive_method: mozyo-bridge journal <id>

## Gate: review (US-level audit)
- target_commit_or_diff: (対象 commit 群)
- remote_verification: (対象 commit の origin 到達確認: 方法 / 結果。到達不能なら blocker)
- 配下issue確認結果: (#<id> ごとの journal / 検証 / close 妥当性)
- resolved_docs:
- 照合規約:
- 指摘事項 [事実]:
- 指摘事項 [仮説]:
- 指摘事項_根拠出所: (finding ごと: owner_intent j#<id>/原文要点 | documented_rule <path + 節名> | agent_judgment | hearsay)
- 未確認事項:
- 再review要否:
- 結論:

## Gate: review_finding_verdict
- 対象review_journal: j#
- finding_1: <指摘の要約>
  - verdict: accepted | disputed | blocked (hearsay のみ根拠 → 記録化待ち)
  - 検証方法: (確認した code / docs / 事実)
  - 根拠:
  - 根拠出所: (owner_intent anchor | documented_rule path | agent_judgment | hearsay)
  - disputed の場合: design_consultation (purpose: dispute) journal id
- (以降 finding ごとに繰り返す。一括 verdict は invalid)

## Gate: full_surface_review_escalation (Late-Finding Full-Surface Adversarial Sweep)
- subsystem: <対象 subsystem / authority 面>
- late_authority_rounds: (late authority finding を持った distinct round の list)
- late_authority_round_count: <数>
- threshold: <既定 2>
- escalate: true | false
- next_round_mode: per_finding_rereview | full_surface_adversarial
- 根拠: (各 late authority finding の journal anchor)
- authority不変: review/close authority を緩めない (scope を足すのみ)

## Gate: task_close (US 配下の Task / Test / Bug)
- implementation_done_journal:
- commit_hash: (commit を伴う場合。origin 到達可能であること)
- 親USへの引き継ぎ: (未完 scope / 残リスク / なし)
- task_level例外該当: none | <該当理由と対応journal>

## Gate: design_consultation (dispute)
- purpose: dispute
- dispute_target: journal #
- evidence:
- 根拠出所: (owner_intent anchor | documented_rule path | agent_judgment | hearsay)
- counterproposal:
- owner_escalation_required:

## Gate: owner_close_approval
- approval_source: standing_delegation | direct_owner
- delegation_scope: normal_development
- carve_out_check: none | <該当理由>
- review_journal:
- qa_journal:
- production_verification_journal:
- commit_hash: (origin 到達可能であること)

## Gate: close
- 受け入れ確認:
- 指摘対応:
- 残留リスク:
- review結果journal:
- owner_close_approval_journal:
- commit_hash: (origin 到達可能であること。local-only commit では close 不可)
- close判断:
```

## Completion

Implementation Done は完了ではない。US 配下の Task / Test / Bug は implementation_done journal (検証・残リスクを含む) と必要な commit hash record があれば close できるが、それは US の完了ではない。UserStory は Redmine に US-level audit の Review Gate、指摘対応、owner close approval journal (Review Gate とは別)、commit hash record、Close Gate が記録されるまで完了扱いしない。親USを持たない単独 issue は base どおり issue 自身を audit 単位として同じ条件を満たす。

lane process の hibernate (early hibernate を含む) も完了ではない。lane を early hibernate — same-lane Review Gate approved + coordinator staging integration + required CI green を根拠に、TestPyPI / installed dogfood の execution/evidence を専用 release issue へ durable に委譲して process を畳む — しても、それは issue close でも owner close approval でも dogfood 成功でもない。委譲するのは dogfood の execution/evidence であって close authority ではない: source issue の close authority と owner close approval は coordinator の通常経路に残る (release issue へ移らない)。hibernate した lane の issue は依然 open であり、上記 Completion 条件を満たすまで close しない。early hibernate は owner close approval が未成立 (owner_waiting) でも発動してよく、owner approval を hibernate の blocker にしない。hibernate を close / owner approval / dogfood 成功へ読み替えない (process drain と ticket close の分離)。運用手順の正本は skill `references/workflow.md` `## Sublane hibernate (プロセス解放) と early hibernate`、集約 dogfood の release issue 運用は同 `references/release.md` を読む。

## Repo-Local Rules Maintenance (governed mode)

- Dev Container / ephemeral home 対応として、target repo は `.mozyo-bridge/rules/presets/{{PRESET_NAME}}/agent-workflow.md` を repo-local preset として読むことができる。
- preset store を再生成する場合は `mozyo-bridge rules install --repo-local .` を使う。
- router + governance artifact を再生成する場合は `mozyo-bridge scaffold apply {{PRESET_NAME}} --repo-local --target . --backup` を優先する。`--force` は差分を確認してから使う。
- governed preset 配布物 (`.mozyo-bridge/rules/llm_rule_authoring.md`、`.mozyo-bridge/rules/docs_catalog_governance.yaml`、`.mozyo-bridge/docs/catalog.yaml.example`) とこの `agent-workflow.md` は scaffold preset 側を正本とする。target repo で個別に編集したい変更は preset 側へ upstream し、`mozyo-bridge rules install` と `mozyo-bridge scaffold apply --backup` で再配布する手順を取る。`.mozyo-bridge/docs/catalog.yaml` (example 不付き) は target repo 側で自由に埋めてよく、scaffold は上書きしない。docs catalog tooling は mozyo-bridge package 側に同梱されており、target repo は Python source を保持しない。`mozyo-bridge` を upgrade すれば tool も同時に更新される。

## Governed Mode Prohibitions

- この `agent-workflow.md` の Codex direct edit gate を bypass して通常実装 file を監査者が直接編集すること。
- generated 物 (file_conventions.yaml の生成物など) を catalog を介さずに手編集すること。
- docs catalog や resolver / generator tooling を deactivate して shared preset の `{{BASE_PRESET_NAME}}` だけで完了報告すること (本 preset を採用したなら governance verification も完了条件に入る)。
- catalog に project 固有の業務ドメイン名 (顧客名 / 製品コード / 個人名) を canonical id として焼くこと。catalog id は機能種別 / 層 / spec id 程度に抽象化する。
- review、journal、commit message に credential / token / 個人情報 / 本番データ抜粋を記録すること。
