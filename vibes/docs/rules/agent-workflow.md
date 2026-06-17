# Agent Workflow Rules

## 目的

この文書は `mozyo_bridge` repository で作業する AI agent の実行規約である。root の `AGENTS.md` / `CLAUDE.md` は router に留め、詳細規約はこの文書に置く。

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

- Redmine は durable な作業ログであり、実行単位は原則 UserStory / Task / Test /
  Bug issue である。
- Issue は目的、作業対象、成果物、完了条件、必要な gate journal を持つ。
- 作業が完了、block、scope 変更、handoff、review、owner close approval、close
  に進む場合は、該当 issue の journal を更新する。
- chat message を durable な作業ログとして扱わない。
- issue scope が膨らんだ場合は、黙って削らず follow-up issue に分割する。
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
  - **v0.4 以降の default rail** (`--mode queue-enter`、Claude / Codex agent pane 向け `mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variants、`--force` 不可): typing 前に deterministic preflight が走り、すべて pass したときだけ Enter を発行する。marker 観測ありは `sent` / `ok`、marker 未観測は `sent` / `queue_enter` を emit する。promise は **strong preflight 付き practical queued submission** であり、confirmed landing ではない。receiver は引き続き Redmine journal 等の durable record を正本として読む。preflight は (a) explicit `--target` は receiver の tmux window 配下、(b) target pane は sender と同じ tmux session、(c) target pane は所属 window の active split、(d) foreground process が receiver の allowlist (`claude` literal は claude-strong、`codex` literal は codex-strong、`node` literal と `versioned-native-binary` basename は両 receiver で weak admit) を要求する。1 つでも false なら typing 前に `blocked` で die する (`Reason` は `invalid_args` / `target_not_agent` のいずれか)。default 化に伴って preflight 強度を弱化しない。
  - **strict explicit fallback** (`--mode standard`、`mozyo-bridge message --submit` 標準動作、non-agent pane 向け送信): `wait_for_text(marker)` を Enter の必要条件とし、未観測時は `C-u` で入力を消して Enter を送らず `blocked` / `marker_timeout` で fail-closed する。strict landing observation が監査要件・regression check・observability test として必要な送信、または default scope 外 (`mozyo-bridge message` / non-agent pane) のときに明示的に選択する。v0.4 で default ではなくなったが contract からは削除しない。挙動は v0.1 以降一切変更しない。
- どちらの rail を使った場合でも、durable record (Redmine journal 等) が正本である。pane notification は pointer。
- 詳細・state machine 全体・例外条件の正本は `vibes/docs/logics/tmux-send-safety-contract.md` の `## Default Delivery Promise (v0.4)` / `## Queue-Enter Default Rail` 節を参照する (重複させない)。
- `.agent_handoff/tasks.json` は retired queue の棚卸し用であり、standard notification fallback として扱わない。

## User Interaction And Escalation

- Claude は active Redmine issue の scope 内では自律的に作業する。通常はユーザーへ直接質問しない。
- Claude は以下に該当する場合だけ Codex へ escalation する。
  - Redmine issue の目的、成果物、完了条件が曖昧である。
  - 規約、Redmine、repository docs の間に矛盾がある。
  - shared skill、scaffold preset、repo-local policy の境界判断が必要である。
  - destructive、irreversible、release、publish、tag、version bump など外部影響のある操作判断が必要である。
  - secret、credential、個人情報、権限、認証に触れる可能性がある。
  - ユーザー意図の解釈が複数あり、間違えると作業が無駄になる。
  - audit finding への対応方針が source of truth から決めきれない。
- Codex は escalation を受けたら、既存の source of truth から判断できるかを先に確認する。判断できる場合はユーザーへ質問せず、判断と根拠を Redmine に記録する。
- Codex は source of truth だけでは推測になる場合に限り、ユーザーへ問い合わせる。ユーザーとの対話窓口は原則 Codex に統一する。
- ユーザーが Claude に直接指示した場合、Claude は必要に応じて Redmine journal または Codex への通知で source of truth を更新してから続行する。

## Claude / Codex Role Boundary

- 通常開発 task の実装者は Claude とする。Codex は通常開発 task を直接実装しない。
- Codex は escalation、audit、ユーザー対話窓口、source of truth からの判断整理を担当する。
- Codex が通常開発 task ID を受けた場合の standard 動作は、自ら実装することではなく、Claude handoff に変換することである。task の規模、緊急度、実装難易度、ユーザーからの催促、ユーザーが Codex pane に直接書いたことを理由に standard を曲げない。
- cockpit / sublane 運用中の通常開発 task は、coordinator / main lane / main Claude で実装しない。coordinator は owner-facing、audit、routing、drain 判断を担当する。実装可能な ready work は、`review_waiting` / `owner_waiting` / `close_waiting` / `blocked` / `callback_due` を先に drain したうえで、専用 sublane / worktree を作り target-lane Codex gateway 経由で dispatch する。main Claude へ直接渡してよいのは read-only 調査、要約、draft、Design Consultation までであり、専用 sublane へ明示的に移されるまで実装 diff を出させない。
- coordinator は implementation-shaped work の Implementation Request を作る前に、Redmine に dispatch decision を記録する。dispatch decision では、work shape、current lane states、blocking queue、sublane dispatch 可否、default-lane / main-lane 例外理由を明示する。理由のない main Claude / default-lane Claude への implementation_request は process gap として correction 対象である。
- `mozyo_bridge` dogfooding では 3 本の active implementation sublane を標準目安とする。4 本目は burst decision、5 本目以降は explicit owner/operator decision を durable record に残す。review / owner / close / callback queue が詰まっている場合は新規 dispatch を止めて drain を優先する。
- ユーザーからの「実行せよ」「対応して」「やって」「お願いします」「実装して」「進めて」など命令形・依頼形・激励形の指示は、それ単独では Codex の direct edit 権限の根拠にならない。これらは「実行してほしい」という意思表示であり、「Claude を経由しなくてよい」という意思表示ではない。
- Codex 受領時に上記 standard handoff を上書きできるのは、Policy / Skill Authoring Boundary に定義された Codex direct edit 例外に明示的に該当する場合だけである。
- Codex が自律フロー反映確認 task を受けた場合、検証対象となる通常開発 task を選定し、Claude へ handoff する。
- Codex は handoff 前に、選定理由、対象 issue、既存 worktree 差分の扱い、Codex の後続 audit 役割を Redmine に記録する。
- Codex が誤って通常開発 task を直接実装した場合、その実行は task の正規完了に数えない。確認 task 中であれば自律フロー反映確認の成功条件にも数えない。
- 上記の誤実装が発生した場合、対象 issue を未完了に戻し、誤実装の事実、影響範囲、後続対応(採用・破棄・再実装)の判断を Redmine に correction として記録したうえで、Claude 実装から Codex audit までの flow をやり直す。この correction flow は、検証対象の確認 issue に限らず、すべての通常開発 issue に適用する。

## Codex Pre-Edit Classification Gate

Codex は `apply_patch`、新規 file 作成、既存 file 更新、git commit の前に、対象変更がどの実装主体に属するかを分類する。分類を作業後に思い出して correction する運用を標準にしない。

- repo 内の正本成果物を作成・更新・削除する作業は、拡張子や内容種別に関係なく **実装成果物** と扱う。Markdown、HTML、調査メモ、ドラフト、表、taxonomy、report、runbook、設定例も、repo に置かれて後続 agent / user / release が参照するなら実装成果物である。
- 「コードではない」「一時メモに見える」「文章だけ」「commit hash を journal に書く必要がある」という理由は、Codex direct edit の根拠にならない。commit 要件は実装主体の分類を通過した後にだけ発動する。
- Codex が直接編集できるのは、対象 path が Repo-Local Guardrail Autonomous Lane に入っている場合、または active ticket に `codex_direct_edit` gate があり `allowed_paths` に対象 path が列挙されている場合だけである。
- ユーザーが `mozyo-bridge`、Claude 協業、handoff、agent 分担を話題にした場合は、Codex direct edit を default にしない。default は Claude handoff とし、autonomous lane または `codex_direct_edit` gate が確認できた場合だけ direct edit に切り替える。
- Codex が direct edit 例外を使う場合は、edit が land する前、または autonomous lane では edit と同時 / commit 直後に durable record を残す。record には例外種別、対象 file、理由、検証方法、follow-up review 要否を含める。
- Codex が誤って先に成果物を作った場合、その成果物を完了扱いにしない。correction として事実、影響範囲、採用・修正・破棄の判断を durable record に残し、Claude 実装 / 採否判断から Codex audit へ戻す。

## Policy / Skill Authoring Boundary

- 自律フロー、規約、skill、handoff、audit、release / distribution gate の変更では、Codex は方針整理、文案作成、ユーザー対話、audit を担当する。
- 上記の repo ファイル変更実装者は原則 Claude とする。Codex は通常時、規約や skill reference の repo ファイルを直接編集して commit しない。保護 scope は実装ファイル (`src/**`, `tests/**`, `docs/**`, `vibes/docs/**`, `README.md`, release workflow, CLI behavior) と guardrail / docs / catalog surfaces (`AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, `.mozyo-bridge/docs/catalog.yaml`, `.codex/skills/**`, `.claude/skills/**`, `skills/mozyo-bridge-agent/**`, `plugins/mozyo-bridge-agent/**`, `src/mozyo_bridge/scaffold/presets/**`) の両方を含む。chat 上の「ユーザーがガードレール変更を明示」だけでは bypass にならない。
- Codex direct edit が許される例外は、以下のいずれかに当てはまる場合に限る。条件は narrow に運用し、ユーザー指示が曖昧な場合や、複数の解釈ができる場合は default に戻して Claude handoff にする。
  1. ユーザーが `Codex direct edit` または「Codex が直接編集してよい」「Codex に直接実装させてよい」と同等の文言で、対象 task または対象 file を限定して明示的に許可した場合。「実行せよ」「対応して」「やって」「お願いします」「進めて」など一般的な命令形・依頼形・激励形は該当しない。
  2. 既存の誤実装、誤 commit、または誤手順を Redmine / repo に correction として記録するための最小の変更である場合。
  3. Claude に handoff する暇がない真に緊急の小修正である場合(例: 数分以内に進行する release / publish / CI を止めるための1〜数行の修正)。この例外を使う前に Codex は実装を停止し、Redmine に「緊急 direct edit 申請」として状況、対象ファイル、想定変更、影響範囲を記録し、可能ならユーザー確認を得る。状況が曖昧な場合や、確認を得られない場合は適用しない。
- Codex が例外として直接実装した場合の durable record は ticket system に依存する。edit が land する **前** に作成する。
  - Asana projects: Asana task comment に `Codex direct edit` を残し、(a) 該当した例外条件、(b) ユーザー指示の原文または引用、(c) 変更ファイル、(d) 実施した verification、(e) 後続反映確認の要否、を記録する。
  - Redmine projects (`mozyo_bridge` repo を含む。`redmine-governed` preset を採用): active issue に Redmine `codex_direct_edit` gate journal を起票する。必須 field は `role: 実装者`, `direct_edit: true`, `allowed_paths` (Codex が触る全 path を列挙), `reason`, `follow_up_review`。journal が存在しないまま edit を commit した場合は invalid とみなす。
- 上記の record が欠けた direct edit は事後 correction の対象とする。過去 incident pattern: `codex_direct_edit` gate journal なし、または Review Gate 承認済み audit-owned commit path なしで Codex が repo diff を作成した場合は、correction journal に記録し、governed implementation/review flow に戻す。
- `.mozyo-bridge/docs/file_conventions.generated.yaml` をはじめとする catalog generator output は Claude / Codex / owner いずれも手編集しない。`.mozyo-bridge/docs/catalog.yaml` を変更して `mozyo-bridge docs generate-file-conventions` で再生成し、`--check` で drift 確認する。
- 自律フローや role boundary の変更を Codex が直接実装した場合でも、変更後の反映確認 requirement は免除されない。

## Redmine Hierarchy Semantics

`mozyo_bridge` の Redmine 階層では、Epic / Feature を短期作業の完了単位として扱わない。これらは project の長期機能ポートフォリオであり、1 年以上残る投資領域や機能カテゴリを表す。

- Epic は product / governance の大きな投資領域を表す。例: `スキャフォールド統治`, `Agent UI / VS Code 連携`。
- Feature は Epic 配下の継続的な機能カテゴリを表す。例: `Redmine 統治プリセット`, `Workspace 横断セッション管理`, `VS Code Agent Pane PoC`。
- UserStory は実際に受け入れ条件を持ち、review / owner close approval / close の対象になる完了単位である。
- Task / Test / Bug は UserStory の実装・検証・不具合対応の内訳であり、replayable journal と commit / validation record が揃えば close する。
- Version は release / milestone の完了管理に使う。Epic / Feature の close で release 完了を表現しない。
- Version は、関連 issue が複数 Feature / UserStory に分かれる場合の roadmap grouping surface でもある。同じ release、stabilization、UX 改善、dogfooding batch に属する work package は、親子関係を無理に寄せず同じ Version に割り当てて束ねる。Version は親子関係の代替ではなく、進捗・残 scope・release readiness を横断して見るための planning axis である。

進捗管理と構造管理を混同しない。進捗・完了判定は UserStory、child issues、Version で行う。Epic / Feature は「この領域が project 上まだ有効か」を表す構造 node であり、配下 UserStory がすべて close されても自動 close しない。

Epic / Feature を close するのは、その領域を今後使わない、統合・分割で別 node に移す、または product owner が portfolio から外すと判断した場合に限る。単に直近の US が完了した、または当面作業予定が無いという理由では close しない。

Redmine の表示上、Epic / Feature が `未着手` のまま配下 UserStory が `着手中` / `クローズ` になることがある。これは「親の機能領域が未着手」という意味ではなく、Epic / Feature を作業進捗 status として運用していないことを示す。status の見た目が誤解を生む場合は、親 issue の description / journal に「portfolio node / normally left open」と記録し、進捗判断は配下 UserStory と Version で行う。

## Audit Handoff (Claude → Codex)

- 監査の標準単位は UserStory である (central preset `### US-Level Audit Model` を正本とする)。Claude は US 配下の Task / Test / Bug をまとめて実装し、各 issue に実装・検証・残リスクの journal を残したうえで、US 完了時に US-level audit request (gate 名は `review_request`) を記録して Codex に audit を依頼する。doc-only / rule-only scope の US でも省略しない。
- Task / Test / Bug ごとの per-issue audit 依頼は不要。ただし central preset の `us_level_audit.task_level例外` (guardrail / workflow / preset / router / skill 変更、release / packaging / CI、credential、destructive operation / migration、architecture / 互換性変更、実装者の判断迷い、owner / 監査者の明示要求) に該当する場合は Task-level review または design consultation を経由する。親 US を持たない単独 issue は issue 自身を audit 単位とする。
- 依頼経路は高レベル handoff primitive を標準とする: `mozyo-bridge handoff send --to codex --source redmine --issue <issue_id> --journal <journal_id> --kind <review_request|design_consultation|implementation_done|reply|custom>` (および `mozyo-bridge handoff reply ...` / 上位 alias `mozyo-bridge reply ...`)。primitive が receiver pane resolve / deterministic Layer B preflight / marker-prefixed notification の typing / Enter 発行をまとめて行うため、caller は `mozyo-bridge read codex` + `mozyo-bridge message codex` の shell-level 組み立てを行わない。`mozyo-bridge read` / `message` / `type` / `keys` は operator/debug 用の低レベル primitive (read inspection、ad-hoc operator message、raw typing、raw keys) であり、standard handoff/reply の代替にしない。`status` / `doctor` / pane scrollback は durable Redmine anchor が利用可能なときに receiver state / issue state の推測 source として使わず、anchor を直接読む。
- audit 依頼 (US-level audit request) には次を含める。
  - 対象 US と配下 issue の一覧・各状態・implementation_done journal
  - 対象 commit 群と変更ファイルの一覧
  - 実施した verification
  - 重点的に audit してほしい観点・残リスク・未確認事項
- Codex の audit feedback が ticket system のコメント / journal または明示的な通知として返るまで、US を completed として扱わない。pane に echo されただけの応答を audit pass と判定しない。
- audit で issue が指摘された場合は修正 commit を打ち、同じ Codex pane に再 audit を依頼する。
- US close 前の mandatory audit は `mozyo_bridge` repository の project-local policy として維持する (US-level audit model 自体は `redmine-governed` / `redmine-rails-governed` preset 経由で配布される)。
- `mozyo-bridge scaffold apply <preset>` ではユーザーが ticket system preset を明示選択する。選択された preset の workflow だけを適用し、他 preset やこの repo 固有の audit policy を混ぜない。

## Workflow Change Verification

- 自律フロー、skills、rules、handoff、escalation、release / distribution gate を変更した場合は、変更後に新規セッションで反映確認を行う。
- 反映確認は `mozyo_bridge` 本体の通常開発 task で行う。検証対象の規約や skill そのものを変更する task を検証対象にしない。
- 反映確認の通常開発 task は Claude が実装し、Codex は handoff と audit を担当する。Codex は検証対象 task を直接実装しない。
- task の大小や production 影響の有無では検証対象を判定しない。判定軸は、検証対象の自律フロー規約、skill、workflow、release / distribution gate を直接変更する作業かどうかである。
- 反映確認では、agent が起動時規約、Redmine issue、source of truth、handoff / escalation、audit、verification 記録を想定どおり扱ったかを確認する。
- 反映確認の結果は Redmine に記録する。問題があれば follow-up issue を起票する。

## 禁止事項

- root の `AGENTS.md` / `CLAUDE.md` に詳細規約を大量貼り付けしない。
- `vibes/tools/mozyo_bridge` を runtime path として再導入しない。
- Redmine / Rails / vibes 前提の別 project 規約を、この repository に無断で持ち込まない。
- generated build outputs を commit しない: `build/`, `dist/`, `*.egg-info/`, `__pycache__/`。
