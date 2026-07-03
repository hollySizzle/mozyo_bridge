# Agent Workflow Rules

## 目的

この文書は `mozyo_bridge` repository で作業する AI agent の実行規約である。root の `AGENTS.md` / `CLAUDE.md` は router に留め、詳細規約はこの文書に置く。

役割の分界 (#13025): 本 doc は **mozyo_bridge repo 固有** の運用規約の正本である。配布される portable 運用手順の正本は `skills/mozyo-bridge-agent/references/workflow.md`、gate / 役割 / 編集権限 / close 条件の governance contract は central preset `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` にあり、本 doc はそれらを再掲せず、採用宣言・repo 固有拡張・pointer に留める。配置判断の正本は配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `## Workflow docs の正本境界`、mozyo_bridge への適用は `vibes/docs/rules/workflow-docs-boundary.md` を読む。

## 作業開始

- 現在の `cwd` が対象 repository root、またはその配下であることを確認する。
- root の `AGENTS.md` / `CLAUDE.md` は router として扱い、central preset
  `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` と必要な
  project-local docs を読む。
- active な Redmine issue / journal を source of truth として確認する。該当
  issue がない場合は、実装前に作成または owner / coordinator へ起票を依頼する。
- pane message / chat message は通知にすぎない。判断前に Redmine issue
  description / journals / status と catalog で解決された docs を読む。

## Redmine 運用

- Redmine は durable な作業ログである。標準の作業・dispatch 単位は UserStory
  (`1US=1作業単位`; #13002) であり、Task / Test / Bug は US scope 内で実装者が
  まとめて遂行する内訳である。granularity の設定 enum
  (`epic|feature|user_story|leaf_issue`) と例外条件の正本は
  `vibes/docs/specs/work-unit-granularity-config.md` を読む。
- Issue は目的、作業対象、成果物、完了条件、必要な gate journal を持つ。
- narrative の issue 参照 labeling — `#<id> <短い概要>` の形、同一段落内の省略ルール、
  machine-readable surface (commit trailer / CLI flag / JSON field / branch 名 / file path)
  の対象外扱い — の正本は、#13029 により配布側
  `skills/mozyo-bridge-agent/references/workflow.md` の
  「Narrative の issue 参照は `#<id> <短い概要>` で書く」節にある。本 doc は再掲しない
  (#13029 で pointer 化)。repo 内の適用例: `#12703 ticketless no-anchor callback transport`。
- 作業が完了、block、scope 変更、handoff、review、owner close approval、close
  に進む場合は、該当 issue の journal を更新する。
- chat message を durable な作業ログとして扱わない。
- issue scope が膨らんだ場合は、黙って削らず follow-up issue に分割する。
- owner intent、future scope、non-goal、later-stage、decision pending の棚卸しと、
  「後で」「別 Version」「follow-up」「later-stage」提案の immediate durable
  classification (new issue / existing issue / explicit no-op / owner decision pending)
  の正本は、#13029 により配布側 `skills/mozyo-bridge-agent/references/workflow.md` の
  `## Backlog reconciliation gate (deferred intent の即時 durable 分類)` にある。
  repo の US close / Version close 運用への組み込みは
  `vibes/docs/logics/coordinator-sublane-development-flow.md` `## US close と Version close`
  を読む。会話だけに残さない。
- Claude の通常開発完了 journal には、次の最小証跡を短く残す。
  - `mozyo-bridge-agent` skill を loaded したこと。
  - active Redmine issue / journal と relevant project docs を確認したこと。
  - 変更 commit / 変更 file / 実施した verification / 残リスク。
  - 追加で参照した relevant rule / reference がある場合は、その path または source。
- 上記は監査可能性のための証跡であり、全 reference を毎回読むことを要求しない。

## Secret Handling

- PyPI / TestPyPI token、API key、personal credential、個人情報を repository、Redmine、external docs に記録しない。
- `.env`、`.env.*`、`.pypirc` は local-only の secret surface とし、ignored のままにする。
- production publish は local token upload を標準 route にしない。

## mozyo-bridge の扱い

- `mozyo-bridge` は notification transport であり、review、completion、task state の source of truth ではない。
- pane message を受けた agent は、作業前に Redmine issue / journal または明示された source of truth を確認する。
- 送信側は 2 本の rail から正しいものを選ぶ。default を黙って弱化しない。
  - **v0.4 以降の default rail** (`--mode queue-enter`、Claude / Codex agent pane 向け `mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variants、`--force` 不可): typing 前に deterministic preflight が走り、すべて pass したときだけ Enter を発行する。marker 観測ありは `sent` / `ok`、marker 未観測は `sent` / `queue_enter` を emit する。promise は **strong preflight 付き practical queued submission** であり、confirmed landing ではない。receiver は引き続き Redmine journal 等の durable record を正本として読む。preflight は explicit target / same-session / receiver identity / foreground process allowlist に加え、v0.5 以降は inactive registered agent pane が `standard_target_admission` (live pane / strong role match / `workspace_id` present / unambiguous) を満たす場合に `tmux select-pane` で active 化して delivery する。admission 不通過または activation disabled では typing 前に `blocked` で die する。default 化に伴って preflight 強度を弱化しない。
  - **strict explicit fallback** (`--mode standard`、`mozyo-bridge message --submit` 標準動作、non-agent pane 向け送信): `wait_for_text(marker)` を Enter の必要条件とし、未観測時は `C-u` で入力を消して Enter を送らず `blocked` / `marker_timeout` で fail-closed する。strict landing observation が監査要件・regression check・observability test として必要な送信、または default scope 外 (`mozyo-bridge message` / non-agent pane) のときに明示的に選択する。v0.4 で default ではなくなったが contract からは削除しない。挙動は v0.1 以降一切変更しない。
- どちらの rail を使った場合でも、durable record (Redmine journal 等) が正本である。pane notification は pointer。
- delivery rail dogfood、pre-smoke gate、release-adjacent runtime verification では runtime fingerprint を durable record に残す。portable 規律 (version 文字列単独を evidence にしない / fingerprint 記録 / 不一致は `blocked`・`environmental` で PASS に混ぜない / 実行 surface の無承認自己修復禁止) の配布正本は、#13060 により配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `## Runtime fingerprint 検証規律` にある。本 repo 固有の適用: 依存 feature probe の例は `standard_target_admission` present、PASS evidence の代表例は #12546 real-machine smoke。local bare / pipx runtime alignment は source-runtime smoke green 後の別 Task とし、owner / operator approval なしに dogfood / smoke 中の pipx reinstall、local install、tag、publish、version bump で PASS を作らない。具体 field 一覧と alignment 手順は `vibes/docs/logics/tmux-send-safety-contract.md` の `### Runtime Fingerprint Gate` を参照する。
- 詳細・state machine 全体・例外条件の正本は `vibes/docs/logics/tmux-send-safety-contract.md` の `## Default Delivery Promise (v0.4)` / `## Queue-Enter Default Rail` 節を参照する (重複させない)。
- `.agent_handoff/tasks.json` は retired queue の棚卸し用であり、standard notification fallback として扱わない。

## User Interaction And Escalation

escalation trigger 一覧 (実装者 Claude がユーザー窓口 Codex へ escalate する 7 条件) と、escalation を受けた Codex の handling (source of truth 先行確認、ユーザー問い合わせの限定、対話窓口の Codex 統一、Claude への直接指示時の source of truth 更新) の正本は、#13029 により配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `### 実装者 escalation trigger (Claude → Codex)` にある。本 doc は再掲しない (#13029 で pointer 化)。本 repo 固有の追加はない (durable record は Redmine issue / journal)。

## Claude / Codex Role Boundary

- 通常開発 task の実装者は Claude とする。Codex は通常開発 task を直接実装しない。
- Codex は escalation、audit、ユーザー対話窓口、source of truth からの判断整理を担当する。
- Codex が通常開発 task ID を受けた場合の standard 動作は、自ら実装することではなく、Claude handoff に変換することである。task の規模、緊急度、実装難易度、ユーザーからの催促、ユーザーが Codex pane に直接書いたことを理由に standard を曲げない。
- cockpit / sublane 前提の開発フローでは、管制塔とサブレーンの責務分担、仕様決定 routing、帯域 / admission / pipeline fill、US close、sublane retirement、後続 Version / US 提案の順序は `vibes/docs/logics/coordinator-sublane-development-flow.md` を spine とする。この文書では詳細を複製せず、判断時は同 doc を先に読む。
- cockpit / sublane 運用中の通常開発 task は、coordinator / main lane / main Claude で実装しない。coordinator は owner-facing、audit、routing、drain 判断を担当する。`review_waiting` / `owner_waiting` / `close_waiting` / `integration_waiting` / `blocked` / `callback_due` / `callback_delivery_failed` は coordinator-blocking state として先に drain する。一方で、既存 lane が `implementing` のみなら coordinator は idle とみなし、独立した ready work を専用 sublane / worktree へ pipeline dispatch する。直列化する場合は file / invariant overlap、merge order、release gate、owner decision など具体理由を dispatch decision に残す。main Claude へ直接渡してよいのは read-only 調査、要約、draft、Design Consultation までであり、専用 sublane へ明示的に移されるまで実装 diff を出させない。
- `sublane` は cockpit / tmux / `mozyo-bridge agents targets` で発見できる checkout lane と agent pane identity を持つ実運用 lane を指す。Codex の内部 `multi_agent` / hidden worker / forked subagent は、operator の文字コックピットに現れず、Redmine callback と pane identity を同じ方法で監査できないため、この repo の sublane として扱わない。coordinator が sublane 実装を要求する作業で可視 sublane が存在しない場合は、不可視 worker や default-lane Claude に流さず、worktree / cockpit lane の作成または operator 判断を Redmine に記録して停止する。
- coordinator は implementation-shaped work の Implementation Request を作る前に、Redmine に dispatch decision を記録する。dispatch decision では、work shape、current lane states、blocking queue、sublane dispatch 可否、default-lane / main-lane 例外理由を明示する。理由のない main Claude / default-lane Claude への implementation_request は process gap として correction 対象である。
- coordinator は 1 本の sublane dispatch 成功を turn 完了条件にしない。dispatch 後、callback / review / owner / integration / close / retirement drain 後、next-action 判断前に `vibes/docs/logics/coordinator-sublane-development-flow.md` の Post-Dispatch Fill Loop を実行し、追加 dispatch または concrete stop reason を Redmine に残す。
- `mozyo_bridge` dogfooding の具体的な lane 数 soft profile は `vibes/docs/logics/coordinator-sublane-development-flow.md` を正本とする。現行 profile は main coordinator に加えて active implementation sublane 4 本を target、5 本目を低衝突・drain 可能な burst、6 本目以降を explicit owner/operator decision 必須として扱う。review / owner / close / callback queue が詰まっている場合は新規 dispatch を止めて drain を優先する。
- ユーザーからの「実行せよ」「対応して」「やって」「お願いします」「実装して」「進めて」など命令形・依頼形・激励形の指示は、それ単独では Codex の direct edit 権限の根拠にならない。これらは「実行してほしい」という意思表示であり、「Claude を経由しなくてよい」という意思表示ではない。
- Codex 受領時に上記 standard handoff を上書きできるのは、Policy / Skill Authoring Boundary に定義された Codex direct edit 例外に明示的に該当する場合だけである。
- Codex が自律フロー反映確認 task を受けた場合、検証対象となる通常開発 task を選定し、Claude へ handoff する。
- Codex は handoff 前に、選定理由、対象 issue、既存 worktree 差分の扱い、Codex の後続 audit 役割を Redmine に記録する。
- Codex が誤って通常開発 task を直接実装した場合、その実行は task の正規完了に数えない。確認 task 中であれば自律フロー反映確認の成功条件にも数えない。
- 上記の誤実装が発生した場合、対象 issue を未完了に戻し、誤実装の事実、影響範囲、後続対応(採用・破棄・再実装)の判断を Redmine に correction として記録したうえで、Claude 実装から Codex audit までの flow をやり直す。この correction flow は、検証対象の確認 issue に限らず、すべての通常開発 issue に適用する。

## Architecture Boundary For Modularization

- `src/**` / `tests/**` の分割・整理・新規設計を伴う通常開発では、単なる free function のファイル移動だけを architecture 改善として扱わない。対象 path の catalog resolve で `vibes/docs/logics/object-oriented-architecture-policy.md` を読み、command handler / use case / domain policy / value object / port-adapter のどこを改善する作業かを Redmine に記録する。
- OOP-first は「すべてを class にする」ことではない。pure deterministic helper、serialization helper、局所 validation は function のままでよい。一方で、外部副作用、複数 step の状態遷移、workflow authority / routing / approval / send safety、test double を必要とする境界は named object / typed result / Protocol port へ寄せる。
- `argparse.Namespace`、dict payload、raw subprocess / tmux / Redmine calls を use case deep layer へ流し続ける変更は、分割後も procedural coupling が残っているものとして residual を記録する。今回の scope で解消しない場合は、対応する OOP-first follow-up issue (例: #12638 / child task) へ明示的に引き継ぐ。
- Codex review では、line count や module count だけで承認しない。authority-bearing orchestration が handler / use case / port boundary に近づいたか、または未対応 residual が durable issue に接続されているかを確認する。

## Codex Pre-Edit Classification Gate

正本は central preset `### Codex Pre-Edit Classification Gate` (#13028 で pointer 化)。edit / commit 前に変更の実装主体を分類し、Markdown・runbook・設定例も repo 正本成果物なら実装成果物として扱う、という判断規約は preset 本文を読む。本 repo 固有の追加はなく、repo 固有の gated-surface path 拡張は `## Policy / Skill Authoring Boundary` を読む。

## Policy / Skill Authoring Boundary

- 役割分担 (Codex = 方針整理 / 文案 / ユーザー対話 / audit、Claude = repo file 実装)、Codex direct edit の例外 3 条件、edit 前の記録要件の正本は、skill `skills/mozyo-bridge-agent/references/workflow.md` `## Policy / skill authoring 境界` (cross-system 手順) と central preset `### Codex Direct Edit Gate` / Gate Schema `codex_direct_edit`。autonomous lane 外の Codex 直接編集には active issue 上の Redmine `codex_direct_edit` gate journal (必須 field は `role: 実装者`, `direct_edit: true`, `allowed_paths`, `reason`, `follow_up_review` — 意味論の正本は preset Gate Schema) が edit 前に必要。本 doc は semantics を再掲しない (#13028 で pointer 化)。
- **本 repo 固有の保護 scope 拡張**: 実装ファイル (`src/**`, `tests/**`, `docs/**`, `README.md`, release workflow, CLI behavior) に加え、autonomous lane 外の guardrail / docs / catalog surfaces として `AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, `.codex/skills/**`, `.claude/skills/**`, `skills/mozyo-bridge-agent/**`, `plugins/mozyo-bridge-agent/**`, `src/mozyo_bridge/scaffold/presets/**` を含む。chat 上の「ユーザーがガードレール変更を明示」だけでは bypass にならない。
- `Repo-Local Guardrail Autonomous Lane` に入る `vibes/docs/rules/**`, `vibes/docs/logics/**`, `vibes/docs/specs/**`, `.mozyo-bridge/docs/catalog.yaml` は preset と `vibes/docs/rules/codex-autonomous-guardrail-lane.md` (採用記録) に従って Codex が自律編集できる。
- `.mozyo-bridge/docs/file_conventions.generated.yaml` 等の catalog generator output は誰も手編集しない (`.mozyo-bridge/docs/catalog.yaml` 変更 → `mozyo-bridge docs generate-file-conventions` 再生成 → `--check`)。
- 記録が欠けた direct edit は事後 correction の対象 (過去 incident pattern: `codex_direct_edit` gate journal なし、または Review Gate 承認済み audit-owned commit path なしの Codex repo diff → correction journal に記録して governed flow へ戻す)。direct edit 後も反映確認 requirement は免除されない。correction flow の詳細は上記正本に従う。

## Redmine Hierarchy Semantics

一般的な記載粒度 / Version 運用の判断ロジック (Epic / Feature / US / leaf の granularity 判断表、原文要点と normalized intent の分離、US close conditions の書き方、Version の sizing / follow-up 収容 / dispatch 候補選定) の正本は、#13024 により配布側 `skills/mozyo-bridge-agent/references/redmine-issue-authoring.md` にある。本節はその一般則を再掲せず、`mozyo_bridge` workspace 固有の採用事実と例外を記録する。workspace 固有の採用: Epic と Feature の番号 prefix はそれぞれ独立した系列で、`110`, `120`, `130`... の 10 刻みが workflow 上の読み順・並び順を表す (優先度や進捗ではない)。

`mozyo_bridge` の Redmine 階層では、Epic / Feature を短期作業の完了単位として扱わない。これらは project の長期機能ポートフォリオであり、1 年以上残る投資領域や機能カテゴリを表す。

- Epic は product / governance の大きな投資領域を表す。例: `スキャフォールド統治`, `Agent UI / VS Code 連携`。
- Feature は Epic 配下の継続的な機能カテゴリを表す。例: `Redmine 統治プリセット`, `Workspace 横断セッション管理`, `VS Code Agent Pane PoC`。
- UserStory は実際に受け入れ条件を持ち、review / owner close approval / close の対象になる完了単位である。
- Task / Test / Bug は UserStory の実装・検証・不具合対応の内訳であり、replayable journal と commit / validation record が揃えば close する。
- Redmine Version は release / milestone の完了管理に使う。Epic / Feature の close で release 完了を表現しない。
- Version は、関連 issue が複数 Feature / UserStory に分かれる場合の roadmap の候補範囲 (grouping surface) でもある。同じ stabilization、UX 改善、dogfooding batch、acceptance batch に属する work package は、親子関係を無理に寄せず同じ Version に割り当てて束ねてもよい。Version は親子関係の代替でも、active lane-set の正本でもなく、進捗・残 scope・release readiness を横断して見るための planning axis である。
- Redmine Version / issue `fixed_version` は実行レーン配置の source of truth ではない。標準の実行・dispatch 単位は UserStory (`1US=1作業単位`; granularity は `epic|feature|user_story|leaf_issue` の設定 enum、正本は `vibes/docs/specs/work-unit-granularity-config.md`)、受け入れ単位も UserStory、active lane-set は coordinator が Redmine journal、branch ancestry、changed paths、merge state、owner / release gate、live callback state から都度決める。`leaf_issue` 単位の dispatch は central preset の `us_level_audit.task_level例外` に該当する場合の例外、`epic` / `feature` は explicit owner/operator decision なしに implementation dispatch しない。
- coordinator は `fixed_version` で候補を絞ってよいが、Version が同じことを理由に直列化したり、Version が違うことを理由に無条件で並列化したりしない。dispatch / hold の理由は concrete conflict cost、dependency、gate、integration backlog、callback / review / close drain のいずれかとして durable record に残す。
- active lane-set は ready work unit 数最大化問題として扱う (標準単位は UserStory)。local soft profile の範囲で、期待される merge conflict / module_health baseline conflict / shared invariant conflict / rework cost を増やさない ready work unit を優先的に載せる。管理が面倒、pane が多い、1 lane が既に動いている、という coordinator 都合は stop reason ではない。
- Smoke / acceptance / real-machine rerun は実装 blocker と混ぜず、最後に owner 承認付きの run window として扱う。実装 blocker が残る間は blocker issue を実装候補として扱い、smoke issue は実行承認まで hold する。
- 親 UserStory が umbrella として複数 roadmap / acceptance group にまたがる場合、親へ fixed_version を一括 propagation しない。子 issue の実行可否は各 leaf の durable record と live integration state から読み、親 issue には umbrella / cross-group であることと close 条件を journal / description に記録する。
- Redmine Version 名に将来の package release 番号 (`v0.10.x` など) を先入れしない。Redmine Version は作業テーマ / roadmap grouping / acceptance bundle の名前であり、package version の正本ではない。
- Package release 番号は release gate で、実際に release candidate に含める commit、互換性、release notes、tag / publish scope を確認してから決める。正本は Git tag、package metadata、release notes、release journal であり、Redmine Version 名ではない。
- 既存の番号付き Redmine Version 名は歴史記録として残してよいが、新規 roadmap group 作成時は semver 風の番号を避ける。番号付き Redmine Version を改名する場合は、参照 issue / journal / roadmap への影響を Redmine に記録し、release / tag / publish / version bump とは別作業として扱う。
- 将来 `lane_group` / `lane_set` 相当の Redmine custom field や workflow DB が整備された場合も、それは candidate grouping / decision support であり、active lane-set の正本にはしない。active lane-set の authority は coordinator の drain / dispatch decision journal と、その根拠になる Redmine issue / journal / Git state / gate state である。

進捗管理と構造管理を混同しない。進捗・完了判定は UserStory、child issues、Version で行う。Epic / Feature は「この領域が project 上まだ有効か」を表す構造 node であり、配下 UserStory がすべて close されても自動 close しない。

Epic / Feature を close するのは、その領域を今後使わない、統合・分割で別 node に移す、または product owner が portfolio から外すと判断した場合に限る。単に直近の US が完了した、または当面作業予定が無いという理由では close しない。

Redmine の表示上、Epic / Feature が `未着手` のまま配下 UserStory が `着手中` / `クローズ` になることがある。これは「親の機能領域が未着手」という意味ではなく、Epic / Feature を作業進捗 status として運用していないことを示す。status の見た目が誤解を生む場合は、親 issue の description / journal に「portfolio node / normally left open」と記録し、進捗判断は配下 UserStory と Version で行う。

## Audit Handoff (Claude → Codex)

- 監査の標準単位 (UserStory)、task_level例外、US-level audit request の必須内容、gate 語彙の正本は central preset `### US-Level Audit Model` / `### Gate Schema` (review_request)。handoff primitive の使い方 (高レベル `mozyo-bridge handoff send` 標準、低レベル read/message/type/keys は operator/debug 用、durable anchor を直接読む) の正本は skill `references/workflow.md` の `## Handoff ライフサイクル` / `## 同一レーン Claude dispatch`。本 doc は再掲しない (#13028 で pointer 化)。
- 本 repo 固有の宣言: US close 前の mandatory audit は `mozyo_bridge` repository の project-local policy として維持する (US-level audit model 自体は `redmine-governed` / `redmine-rails-governed` preset 経由で配布される)。doc-only / rule-only scope の US でも省略しない。
- `mozyo-bridge scaffold apply <preset>` ではユーザーが ticket system preset を明示選択する。選択された preset の workflow だけを適用し、他 preset やこの repo 固有の audit policy を混ぜない。

## Workflow Change Verification

正本は skill `references/workflow.md` `## Workflow 変更の反映確認 (Workflow Change Verification)` (guardrail / skill / gate 変更後の新セッション反映確認、検証対象を直接変更しない通常開発 task の選定、Claude 実装 / Codex 選定・audit、結果記録と follow-up 起票)。本 doc は再掲しない (#13028 で pointer 化)。本 repo での適用: 反映確認は `mozyo_bridge` 本体の通常開発 task で行う。

## 禁止事項

- root の `AGENTS.md` / `CLAUDE.md` に詳細規約を大量貼り付けしない。
- `vibes/tools/mozyo_bridge` を runtime path として再導入しない。
- Redmine / Rails / vibes 前提の別 project 規約を、この repository に無断で持ち込まない。
- generated build outputs を commit しない: `build/`, `dist/`, `*.egg-info/`, `__pycache__/`。
