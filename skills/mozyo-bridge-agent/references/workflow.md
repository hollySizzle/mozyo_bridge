# Workflow リファレンス

## 作業開始

- `AGENTS.md` に記載された central preset rules を取得する (`mozyo_bridge` は `redmine-governed` を使う。他の repo は `redmine`、`asana`、`none` を使う場合がある)。
- repository root と現在の `cwd` を確認する。
- repo の ticket システムで active な ticket を確認する: Redmine-preset の repo (`mozyo_bridge` を含む) では Redmine issue / journal、Asana-preset の repo では Asana task。ticket が存在しない場合は、実装前に作成する。
- `mozyo_bridge` の project notes / 親 issue / 親 task を確認する。

## Ticket-ID 入口

入力が ticket ID・ticket URL・ticket 名を挙げる pane / chat テキストのみの場合、行動する前に durable な ticket record を取得して照合する。pane や chat から与えられた framing は、完全に framing されているように見えても source of truth の代わりにならない。

- ID の形状、URL の host、scaffold preset から ticket システムを特定する。特定できない場合は停止して確認する。
- そのシステムの正規 API 経由で ticket を取得し、durable record から目的、対象 path、成果物、参照 rule、完了条件、禁止事項を抽出する。行動する前に、pane からの framing を取得した record と照合する。
- システムごとの gate / comment semantics は、その ticket システムの central preset に従う。Asana と Redmine の語彙を混用しない。
- 必須の framing field が欠落している、曖昧である、または親 ticket と矛盾する場合は、実装を開始しない。まず ticket の durable log に gap を記録する。
- ユーザーからの命令・依頼表現 ("実行せよ"、"対応して"、"やって"、"implement it" など) は、下で定義する Codex / Claude 役割境界を上書きしない。entrypoint は依然として durable record を経由する。

## Ticket システム運用規約

active な ticket システムは、repo の central preset が選択するものである。`mozyo_bridge` 自身では Redmine であり、他の採用 repo は Asana を使う場合がある。両者の語彙を混用しない — システムごとの gate 名、comment / journal semantics、必須 field は central preset にあり、ここには置かない。

両者に共通:

- ticket は実行キューであり、chat ではない。
- ticket は、目的、対象 path、出力 path、参照、done 条件、禁止事項を持つ実行可能単位として扱う。
- 作業が完了したとき、block したとき、scope が実質的に変わったときは ticket を更新する。
- scope が拡大したときは、follow-up 作業を新しい ticket に分割する。
- 通常の開発完了 entry には、短い監査証跡を記録する:
  - central preset rules を取得したこと;
  - `mozyo-bridge-agent` skill を読み込んだこと;
  - active な ticket と project notes を確認したこと;
  - 追加で参照した関連 rule / reference path。
- この監査証跡は review 可能性のためのものである。task ごとにすべての reference file を読むことを要求するものではない。

システム固有の entry point:

- **Redmine** (`mozyo_bridge` の default。preset は `redmine-governed`): durable な作業 log は Redmine issue とその journal である。Start / Implementation Done / Review Request / Review / Owner Close Approval / Close などの gate は、issue 上の個別 journal entry として記録される。標準の Review Request / Review / owner close approval 単位は UserStory である (central preset の `### US-Level Audit Model`): 子の Task / Test / Bug issue は実装 / 検証 journal を持ち、task ごとの review ではなく 1 回の US-level Codex audit に集約される。ただし preset に列挙された task-level 例外を除く。central preset (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md`) が gate ごとの必須 field を定義する。この skill はそれらの表を複製してはならない。
  - **Issue 起票の粒度と Version 運用**: ticket 起票時に Epic / Feature / UserStory / leaf の粒度をどう選ぶか、owner 発話の digest と正規化した intent をどう分離するか、Redmine Version を planning / lane-inventory の bucket としてどう sizing / 選択するかは、`references/redmine-issue-authoring.md` にある。
  - **Default project 解決**: 明示の `project_id` が与えられない場合 (ユーザー指示、ticket 本文、MCP request、session context のいずれにもない場合) は、検証済みの workspace-local default として `<repo>/.mozyo-bridge/redmine-defaults.md` を読む。その file は `mozyo-bridge workspace-defaults` が `<repo>/.mozyo-bridge/project-defaults.yaml` から生成する (legacy 名 `workspace-defaults.yaml` も fallback として読まれる。Redmine #11920 / #11921)。どちらかの file が欠落している場合、または default が `UNVERIFIED` 警告を出す場合は、issue 作成前に operator に `project_id` を確認する。明示の `project_id` は常に default に優先する。default が verified とマークされていても、明示値が利用可能なときに default へ fallback しない。
- **Asana** (Asana-preset の repo 向け): durable な作業 log は Asana task とその comment / story である。完了 note、監査 comment、follow-up の scope 変更は task 自体に記録する。Asana central preset (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md`) がその語彙の正本である。

### Issue の subject / description 分離

ticket を作成するとき — `create_*` MCP tool (`create_task_tool`、`create_user_story_tool` など) による Redmine issue / task、または Asana task — subject と description は役割の異なる別 field である。subject は短く明示的な、人が書く一行要約であり、description が完全な context を運ぶ。本文の構造は subject ではない (Redmine #11856。#11850 multi-lane PoC より: 長い Markdown 本文が渡され、subject が本文の最初の heading である `## 背景` として登録され、後で手動修正が必要になった)。

規則は **作成時の明示 subject (explicit-subject-on-create)** であり、事後の cleanup ではない:

- 常に明示の `subject` を渡す。本文テキストとは独立に、作業の簡潔な要約 (良い commit subject line のような命令形) として自分で書く。
- description 本文から subject を生成させない。Markdown heading (`## 背景`)、長い本文の 1 行目、切り詰められた本文断片は有効な subject ではない。container / US-level の creator — `create_epic_tool`、`create_feature_tool`、`create_user_story_tool`、`create_inquiry_tool` — は明示の `subject` field を取るので、本文に決めさせず自分で設定する。
- leaf creator には `subject` field がないことに注意する。`create_task_tool`、`create_bug_tool`、`create_test_tool` は現状 `description` のみを公開し、その先頭内容から subject を導出する (これが #11884 の `## 背景` subject を生んだ)。これらの tool には設定する field がないため: description の 1 行目を `#`/`##` の heading marker を持たない plain-text の一行要約にし、作成された subject を検証して、heading や本文断片が登録されていた場合は `update_issue_subject_tool` で直ちに修正する。これら 3 つの leaf creator への明示 `subject` の追加は、外部 Redmine MCP server (`redmine_epic_grid`) 側の upstream 変更であり、Redmine #11885 で追跡している — `mozyo_bridge` repo の変更ではない。
- 完全な context — 目的、対象 path、受け入れ条件、参照 — は subject ではなく description に置く。両者を互いに滲ませない: description は長い subject ではなく、subject は description の一行 dump ではない。

**即時修正規則。** 不正な subject が実際に登録されてしまった場合 (`## 背景` のような heading 断片、切り詰められた本文行、その他要約になっていない subject) は、同一 session 内で修正する — 後の手動 cleanup に残さない:

- ticket システムの update tool (Redmine では `update_issue_subject_tool`、Asana では相当する task-name 更新) で、subject を直ちに簡潔で明示的な要約へ修正する。
- 修正を durable な作業 log (Redmine journal / Asana comment) に記録し、不正な subject とその修正が監査可能になるようにする。これは、不正な subject をその場で修正した元の #11850 j#57294 の観測を踏襲する。

この規約は作成時の discipline を追加するものであり、gate 語彙、階層 semantics、必須 field は一切変えない。それらは central preset の定義のままである。

portable な規則は、*creator が明示の簡潔な subject を渡し、不正な subject を durable record 上で直ちに修正する* ことである。operator 個人の subject の言い回し style、命名 template、ticket-title 規約は operator 自身の runbook であり (採用 repo の public / private boundary rule を読む。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)、配布される skill / preset 本文ではない。

## Local docs

- `AGENTS.md` と `CLAUDE.md` は router である。
- 採用 repo 自身の docs namespace が、その repo の working rules、構造 / 仕様 note、決定と release の logic、再利用可能な template を保持する。固定 layout を仮定せず、repo の router と docs catalog を通じて解決する。`mozyo_bridge` repo 自身では、その namespace は `vibes/docs/` である (`rules/` working rules、`specs/` 構造と仕様 note、`logics/` 決定と release の logic、`temps/` 再利用可能な template)。

### Catalog resolver 使用契約

- task の対象 path が判明したら、`mozyo-bridge docs resolve <path...>` を実行して、それらの path に catalog で紐づく docs (guardrail / spec / convention) を表示する。
- 解決された docs を、実装前、review 前、および guardrail 変更前に読む。`docs resolve` は catalog を読むための入口であり、読むことの代替ではない。
- resolver が失敗した場合 (catalog がない、path が未解決、command が使えない) は、docs を読んだふりをしない: 停止するか、続行前に Redmine journal へ open item として gap を記録する。

## Workflow docs の正本境界

mozyo-bridge workflow は階層化された surface に書かれており、すべての rule は一つの問いで配置される: **他の採用 project がそれを必要とするか?** yes と答える内容は配布本文に属し、no と答える内容は採用 repo 自身の docs に留まる。ある repo の dogfooding で実証された core workflow は配布本文へ upstream する — その repo の local docs に閉じ込めたままにしてはならない。そこでは他のすべての採用 project が黙ってそれを失う。

- **Central preset** (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md`、`mozyo-bridge rules install` / scaffold で配布): governance 契約 — gate 語彙と必須 field、役割分担、path 編集権限の境界、close 条件。preset は *gate が何であるか* の正本である。本 reference はその表を再掲しない。
- **この skill reference 本文** (`skills/mozyo-bridge-agent/references/**`、plugin marketplace と Codex `$skill-installer` で配布): portable な運用手順 — ticket システムの entrypoint と issue 起票、Epic / Feature / UserStory / leaf issue と Version の一般的な使い方、handoff ライフサイクルと送信安全、coordinator / sublane / callback / review / owner-close の標準モデル (標準の cockpit window topology を含む)、stall 検出、退役 (retirement)、および hidden-worker 禁止を伴う visible-lane / durable-anchor 前提。この本文は、あらゆる採用 project で *日々の flow がどう回るか* の正本である。
- **Repo-local docs** (採用 repo 自身の docs namespace): repo の architecture / source-layout 方針、repo 固有の技術的負債と baseline、具体的な Version 名 / issue 履歴 / local 例外、採用宣言、および配布本文への thin pointer や preset が明示的に許可した override。

配置の discipline:

- **1 つの rule に置き場は 1 つ (one rule, one home)。** repo-local doc は配布された手順を再掲しない。採用を宣言し、repo 固有の拡張を加え、配布された section を指す (path + section 名であり、paraphrase ではない)。同じ問いに異なる答えを返す 2 つの doc は、この境界が防ごうとしている failure そのものである。
- **読み順が二重読みを解消する。** gate / 権限の問いは central preset を読み、手順の問いは本 reference 本文を読み、repo 固有の適用は repo の local rule doc を読む。agent は同じ rule を 2 か所で二度読む必要はない。
- **repo-local に見つかった portable rule は配布の gap であり、precedent ではない。** portable な部分を upstream し、local doc を pointer + repo 固有の残余に縮めることで修正する。
- **2 つの境界は合成される。** この section は *distributed vs repo-local* を決め、public / private boundary (`mozyo_bridge` repo では `vibes/docs/rules/public-private-boundary.md`、他は採用 project の相当物を読む) は *portable vs operator-private* を決める。operator-private な policy — 具体的な path、cockpit 構成、lane 数の profile、private runbook — は、どの層に収まり得るとしても配布本文には決して入れない。

`mozyo_bridge` repo 自身については、具体的な surface inventory と upstream 手順は `vibes/docs/rules/workflow-docs-boundary.md` にある (Redmine #13025)。

## Handoff ライフサイクル

handoff は、active な project workflow またはユーザーが別の agent の参加を明示的に求めた場合にのみ使う。

1. sender は、まず durable な source of truth を記録するか特定する。
2. sender は、高レベルの `mozyo-bridge` handoff primitive (`mozyo-bridge handoff send` / `mozyo-bridge handoff reply` / top-level alias `mozyo-bridge reply`) を通じて receiver に通知する。primitive は自身の決定的な preflight を実行する。caller は、通常の handoff / reply のために `mozyo-bridge read` + `mozyo-bridge message` の shell choreography を組み立てない。`notify-*` wrapper (`notify-codex`、`notify-claude`、`notify-codex-review`、`notify-claude-review-result`) は、標準的な Redmine 形の通知のために同じ primitive を経由する互換 entrypoint である。`notify-*-legacy-task` は retired-queue cleanup 用の wrapper のままである。
3. receiver は durable な source of truth から開始する。pane テキストのみからでも、`mozyo-bridge status` / `doctor` / pane scrollback の推測からでもない。それらの surface は operator/debug 用の補助であり、durable な Asana / Redmine anchor が利用可能なときは、名指しされた task / comment / issue / journal を読む。
4. receiver は、所見、blocker、完了 note、検証を durable な source of truth に記録する。
5. receiver は、同じ handoff primitive を通じて sender に短い結果通知を返し、sender が durable record を読むべきだと分かるようにする。
6. sender は durable record から再開し、次の action を決める。

pane message は、このライフサイクルにおける notification edge である。review の pass、task completion、release approval、作業 log ではない。

## Workspace 横断 handoff

sender (Claude または Codex) が、別の tmux session — 例えば別 repo の workspace — にいる agent へ通知する必要がある場合、routing は workflow レベルだけでなく CLI でも制約される (Redmine #10332)。

- target を指名する前に、`mozyo-bridge agents list` (必要に応じて `--json`、`--session NAME`、`--agent claude|codex|unknown`) で target workspace の session、window、pane、process、cwd、推定 repo root、agent 種別を列挙する。discovery は read-only であり、`mozyo-bridge list` / `status` とは別物である。
- session 横断の `mozyo-bridge handoff send --to claude` は、CLI で `blocked` / `cross_session_claude` として拒否される。origin agent は、`--to codex --target <target_session>:codex --target-repo <target_workspace_root>` で target session の Codex window を経由し、その target Codex に local の Claude handoff の実施を依頼しなければならない。理由: 他 workspace の Claude pane へ直接入力することは、その workspace の audit boundary を bypass するからである。
- session 横断の `--to codex` が明示的な gateway path である。Redmine #11301 以降、default の `queue-enter` rail は、制約付き identity gate の下で session 横断 target を受け入れる: 明示の `--target` pane id **に加えて** pass する `--target-repo` (target pane の cwd がその workspace / repo root に解決されること)。この gate を満たすとき、gateway send は `--mode` 不要で default rail 上で動く。`--target-repo` のない session 横断 target、repo が未解決または不一致の target、implicit な target は依然として拒否され (`invalid_args` / `target_repo_mismatch`)、no-rollback 契約を検証済み workspace に束縛したままにする。`--mode standard` / `--mode pending` は fallback として引き続き利用できる — 例えば `--target-repo` を主張できない場合や、厳密な landing 観測が必要な場合である。
- `--target-repo PATH` は repo / workspace の identity gate である。指定された場合、target pane の cwd がその root まで walk up できなければ、handoff は `blocked` / `target_repo_mismatch` で拒否される。root は、`.git` / `.tmux.conf` / `pyproject.toml` を持つ任意の directory、**または** scaffold 済み mozyo workspace marker `.mozyo-bridge/scaffold.json` (Redmine #11301) であり、これにより非 git の Google-Drive-hosted workspace も第一級の identity root になる。異なる repo に対して開かれた同名 session への hardening に加えて、`--target-repo` の指定こそが、session 横断の `--to codex` gateway send を default の queue-enter rail で受け入れさせるものである。
- `--target-repo auto` (Redmine #11778) は、明示の `%pane` target 自身の cwd からその root を推定するため、session 横断の gateway send の前に `tmux display-message -p -t %pane '#{pane_current_path}'` を手で実行しなくてよい。これは明示の `%pane` target がある場合に **のみ** 受け入れられ (receiver label、`session:window` location、implicit discovery では決して受け入れられない)、pane の cwd が identity marker に到達しない場合は fail-closed (`target_repo_mismatch`) のままである。これはいかなる境界も変えない — session 横断の `--to claude` は依然として拒否される — operator が本来手で入力する identity gate を埋めるだけである。
- workspace 横断 request の durable な source of truth は Redmine / Asana に留まる。pane 通知は依然として pointer でしかない。target Codex は durable anchor を読み、target workspace でその request をどう取り込むかを決める。

### `handoff cross-workspace-consult` (高レベル primitive)

`mozyo-bridge handoff cross-workspace-consult` は、上記の標準的な workspace 横断 design-consultation route を 1 つの command にまとめる (Redmine #11779)。これは `handoff send` の thin で boundary-preserving な wrapper である — どの gate も再実装せず、緩めない。安全な route が唯一の route になるよう surface を *狭める* だけである:

- **receiver は `codex` に固定される。** consult は常に target workspace の Codex gateway pane に着地する。`--to` flag が存在しないため、他 workspace の Claude pane に入力させることはできない (session 横断の `--to claude` はいずれにせよ上記の gate で拒否される)。target Codex は durable anchor を読み、**実装が必要な場合は、自身の workspace 内で local の same-session Claude handoff を実施する** — この local hop が、request が target lane の Claude に到達する唯一の sanctioned な経路である。
- **identity gate は必須である。** `--target` と `--target-repo` の両方が parser で必須のため、`handoff send` では `--target-repo` 指定時にのみ走る repo-identity gate が、ここでは常に走る。明示の `%pane` target と `--target-repo auto` (その pane の cwd から root を推定する)、または明示の repo root を使う。この surface に `--force` flag はない。
- **receiver binding はすべての mode で維持される。** 明示の `--target` pane は、何かが入力される前に Codex role (`@mozyo_agent_role` / window 名) に解決されなければならず、これは `queue-enter`、`--mode standard`、`--mode pending` のいずれでも同様である (Redmine #11779)。`handoff send` はこの binding を `queue-enter` の下でのみ強制する (`standard` send の marker-timeout C-u rollback が誤宛先をカバーする)。consult primitive はそれに依存できないため、すべての mode で fail closed する — そうでなければ `--mode standard` / `--mode pending` によって、明示の foreign-Claude `%pane` へ `to=codex` marker の下で入力できてしまう。
- **`--kind` の default は `design_consultation`** であり、override してよい (例: workspace 横断の `review_request`)。
- **durable な source of truth は不変である。** まず Redmine issue / Asana task に consult request を記録する。command は pointer を送るだけである。すべての gating (session 横断 Claude の block、repo-identity gate、receiver-process binding、landing rail、`--target-repo auto` の明示 `%pane` 要件) は、同じ `handoff send` orchestration に委譲され、隠されも弱められもしない。

運用 route: (1) `agents list` / `agents targets` で target workspace の Codex pane を発見する; (2) durable anchor に request を記録する; (3) `mozyo-bridge handoff cross-workspace-consult --source <redmine|asana> <anchor flags> --target <%pane> --target-repo auto [--summary ...]` を実行する; (4) target Codex が anchor を読み、実装が続く場合は local の Claude handoff を行う。手組みの `handoff send --to codex ...` への fallback も引き続き有効である。consult primitive は、同じ route の反復可能な shorthand である。

低レベルの `mozyo-bridge read`、`mozyo-bridge message`、`mozyo-bridge type`、`mozyo-bridge keys` command は operator/debug 用 primitive である (pane 検査、ad-hoc な operator message、raw typing、raw keys)。これらは標準の handoff / reply path ではなく、primitive の日常的な代替として手で組み立ててはならない。sanctioned な用途は、Retry Path Checklist (preset ごとの central rules) の step 3 における operator 主導の `--no-submit` retry path と、明示的な operator debugging のみである。

特定方向のすべての handoff で sender が receiver に通知することを要求する project-local rule は、その scope のすべての task に適用され、audit-only、revalidation、doc-only の task も含む。この「すべて」は、task の framing のされ方、receiver の事前の pickup-intent 表明 (例えば "task record から pull する")、receiver がどうせ durable record を読むだろうという sender の判断によって緩和されない。その根拠で通知を skip することは sender 側の rationalization であり、条件の充足ではない。

## 自然名 target への handoff

operator は handoff target を tmux pane id ではなく自然言語で指名することが日常的にある — 例えば「人形使いへ返して」、「mozyo-bridge の issue_11812 lane の Claude に渡して」、「送って、あっちの Codex に」。これらの request は intent を運ぶが、audited な target ではない。agent (Codex または Claude) は、送信前に compact な target discovery を通じて、自然名を単一の明示 pane target に解決する責任を負う。以下の手順がその解決の標準である。新しい command flag を追加せず、`references/safety.md` の送信安全境界を一切緩めない。

`mozyo-bridge agents targets` (Redmine #11811) は **candidate listing であり、target selection ではない**。自然名の確認に必要な identity column とともに、発見可能な agent pane を列挙する。代わりに 1 つを選ぶことも、送信することも決してしない。選択と送信は caller の audited な判断のままである。

### 解決手順

1. **candidate を列挙する。** compact な表には `mozyo-bridge agents targets` を、programmatic に filter / match する必要があるときは `mozyo-bridge agents targets --json` を実行する。自然名が既に含意している場合は、`--session <name>` や `--agent claude|codex` で絞り込む。discovery は read-only であり、`mozyo-bridge list` / `status` とは別物である。
2. **自然名を pane title 単独ではなく identity column と照合する。** 口頭の名前を、`cockpit group` (`session`)、`workspace` (`workspace_label`)、`lane` (`lane_label` / `lane_id`)、`role` (`claude` / `codex`)、`repo` (`repo_short` / `repo_root`)、および canonical な `pane_id` を使って candidate と照合する。cockpit group 名 (`SESSION` column の named tmux session)、workspace の nickname、`lane-...(issue_11812)` のような lane label、`mozyo_bridge-11812` のような repo short name、role の単語は、正確にこれらの field へ写像する。`role_source` と `confidence` は advisory な signal であり — `window_name` 由来の role は `pane_option` marker より弱い証拠である — それら単独では曖昧さを解決しない。cockpit group / `session` は *grouping* の disambiguator (pane がどの named cockpit session に属するか) であり、identity の source of truth ではない — 下の `名前付き cockpit group と複数 local cockpit session` を読む。
3. **一意な match を要求する。** 確認済み column 全体で自然名を満たす candidate がちょうど 1 つのときにのみ、送信へ進む。workspace / lane / role / repo のすべてが request と一致する単一の非曖昧な candidate (`ambiguous = 0`) だけが green path である。
4. **match が一意でない場合は fail closed する。** match する candidate が 0 の場合、複数が match する場合、または match した candidate の `ambiguous` flag が立っている場合は、推測 **しない**。また、「最も近い」行や先頭行を黙って選ば **ない**。停止して candidate の行 (pane_id + workspace + lane + role + repo) を operator に提示し、明示的な選択を求める — または owner clarification へ escalate する。曖昧な自然名に対する silent な target selection は fail-open regression であり、convenience ではない。
5. **routing actor を lane boundary で選び、検証済みの repo identity gate とともに、その明示 pane id へ送信する。** まず、送信が *誰に* 向かうかを決める (下の `lane 横断 handoff は target lane の Codex を経由する` を読む): same-lane の送信は解決された pane を直接宛先にしてよいが、lane boundary を越える送信は、その lane で解決された Claude pane ではなく **target lane の Codex** pane を宛先にする。次に、選択した `pane_id` (`%NNN`) へ handoff する — `session:window` location、裸の role label、implicit discovery へではない — そして workspace identity gate を、`--target-repo auto` (明示の `%pane` の cwd から root を推定する。明示の `%pane` target のみで受け入れられ、`target_repo_mismatch` で fail-closed) または明示の repo root path で与える。identity の主張を pane title / window 名の parse に頼らない。送信を検証済み workspace に束縛するのは、title テキストではなく `--target-repo` gate である。

### 本手順が緩めない境界

- **session 横断の Claude 直接送信は禁止のままである。** 自然名が別の tmux session の Claude pane に解決されても、session 横断の `mozyo-bridge handoff send --to claude` は許可されない。それは依然として CLI で拒否される (`blocked` / `cross_session_claude`)。session 横断 target には、`--to codex --target <target_session>:codex --target-repo <target_workspace_root>` (または明示の Codex `%pane` からの `--target-repo auto`) で target session の Codex window を経由し、その target Codex に local の Claude handoff の実施を依頼する。discovery によって他 session の Claude pane を指名しやすくなっても、その audit boundary は動かない。
- **lane 横断 handoff は、単一の物理 session 内でも target lane の Codex を経由する。** lane boundary は、tmux topology に関わらず governance boundary である。handoff が coordinator lane から target lane へ — または任意の 2 つの lane 間で — 越えるときは、両 pane が 1 つの物理 tmux session を共有していても (例えば複数 lane を並べて host する cockpit session)、workspace boundary と同様に扱う。compact discovery がその pane を一意に解決し、CLI が技術的には same-session の送信を受け入れるとしても、別 lane の解決済み Claude pane へ handoff を直接送信 **しない**。代わりに **target lane の Codex** pane を宛先にする (`--to codex --target <target_lane_codex_%pane> --target-repo auto`)。その Codex が durable な Redmine / Asana anchor を読み、lane context を検証し、自身の local Claude へ route するかどうかを決める。直接の Claude 配送は、**same-lane** の宛先指定 (coordinator が自 lane の Claude に通知する、または agent が自 lane 内で返信する) のために予約される。これは、session 横断 `--to claude` 禁止と同じ原則を single-session multi-lane の場合に適用するものであり、共有された物理 session が lane boundary を黙って弱めないようにする (Redmine #11812 routing 修正)。
- **discovery は durable record ではない。** `agents targets` の出力は、`status` / `doctor` / scrollback と同様に operator/debug 用の補助である。handoff request の durable な source of truth は Redmine / Asana に留まり、解決された pane への送信は pointer でしかない。
- **operator 固有の identity を配布 default に入れない。** この flow を文書化・script 化するとき、private な path、host、session nickname、operator 固有の lane policy を OSS skill / preset の default に焼き込まない。portable なのは手順であり、具体的な workspace 識別子は配布本文ではなく operator 自身の runtime に属する。

## 同一レーン Claude dispatch

target-lane の Codex が実装 / review request を **自 lane の** Claude implementer へ route するとき — `## Workspace 横断 handoff` と `lane 横断 handoff は target lane の Codex を経由する` が、作業が lane の Claude に到達する唯一の sanctioned な経路として予約する same-lane の `--to claude` hop — その dispatch は **標準 handoff であり、submit を完了しなければならない**。入力済みだが未 submit の prompt で止まってはならない。pending input のまま残された same-lane dispatch は、operator / coordinator に手で Enter を押させることになり、それこそが本 section が防ごうとする stall である (Redmine #12207 j#60741: same-lane の Codex→Claude dispatch が `blocked` を報告し、coordinator が Enter を押すまで notification が Claude pane に staged のまま残った)。

1. **まず durable anchor を記録し、その後 send を submit-complete する。** `mozyo-bridge handoff send --to claude --source redmine --issue <issue_id> --journal <journal_id> --kind <implementation_request|review_request|...>` を実行する。default の `queue-enter` rail は、Claude pane がその window の active split であるとき、または inactive な登録済み Claude pane が standard_target_admission (step 2) を pass するとき、`--mode` 不要で Enter を押す。
2. **inactive-split の Claude pane: standard_target_admission が登録済み pane を activate する。`--mode standard` が必要なのは未 admit の pane のみである。** 一般的な cockpit-grid の場合、Codex pane が active split で、Claude pane は inactive な sibling である。Redmine #12597 以降、default の `queue-enter` rail は standard_target_admission を実行する: *登録済み* の inactive Claude pane (live / strong role / `workspace_id` あり / 非曖昧) は `tmux select-pane` で activate され、`--mode` 不要で default rail 上で submit-complete される — pane selection のみで raw key injection は決して行わず、activation は durable record に記録される。pane が admit され **ない** 場合 (例: `workspace_id` なし)、または `--no-target-activation` で activation を無効化した場合にのみ、rail は fail-closed (`blocked` / `invalid_args`) して何も入力しない。その場合は、block が emit する recovery command の指示どおり、strict-but-submitting rail 上で `--mode standard --target <claude_%pane> --target-repo auto` により再 dispatch する。`--mode standard` は landing marker を観測してから Enter を押すため、inactive な same-identity pane に対して submit-complete する — callback 手順の step 4 が (同じく通常 inactive な) coordinator pane に対して行うのと同じ `--mode standard` の選択である。
3. **`--no-submit` / `--mode pending` は標準の dispatch path ではない。** これは notification を入力し、意図的に input を pending のまま operator の submit に委ねる — 明示的な operator / debug fallback (および `references/safety.md` の preset ごとの `marker_timeout` retry path) であり、same-lane Claude へ作業を route する default route では決してない。標準の dispatch でこれを選ぶことは #12207 が修正した regression である: dispatch は配送済みに見えるのに、implementer は turn を決して受け取らない。
4. **dispatch outcome を記録する** (sent / 理由付き blocked に、replay 可能な `--mode standard` retry command を添える)。記録先は durable record であり、`### Callback 手順` が callback outcome を記録するのと同じやり方に従う。recovery command を携えた `blocked` outcome は dispatch を replay 可能に保つ。黙って pending のまま残された dispatch は、配送された handoff ではない。

submit rail 自体は `references/safety.md` が固定する (`queue-enter` が default、`standard` が strict な明示 fallback、`pending` が operator-submit path。`mozyo_bridge` repo における深い send-safety 契約 doc は `vibes/docs/logics/tmux-send-safety-contract.md`)。本 section は、same-lane dispatch が pending で stall せず submit に到達するよう、*どの* rail を使うかだけを固定する。receiver-binding / receiver ごとの process gate を緩めず、blind な Enter を導入しない (Redmine #12207 non-goals)。Redmine #12597 の standard_target_admission による inactive な登録済み pane の activation は、pane selection (`tmux select-pane`) のみを使い — raw key injection は決して行わず — 最小の admission 契約と他の preflight gate を無傷のまま残す。

## Sublane の coordinator callback

一部の運用モデルは、**coordinator lane** (project 管理・audit・release・軽量検証を所有する main Codex lane) と、1 つ以上の **sublane** (それぞれ自前の Codex gateway と Claude 実装者を持つ実装 lane) を並走させる。作業は coordinator から target lane の Codex gateway 経由で sublane に dispatch され (`## 自然名 target への handoff` を参照)、その後 durable な Redmine / Asana state は sublane の*内側で*進む。sublane が報告を返さない限り coordinator はその進捗を見ることができない。durable record が既に — 例えば review-approved や owner-close-approval-waiting まで — 前進していても、coordinator cockpit からは作業が stall しているように見える (Redmine #11850 j#57274、#11812 の実行中に顕在化)。

sublane は handoff-worthy state に到達するたびに、coordinator lane へ簡潔な callback を送らなければならない。これにより coordinator は前進した durable record を読むべきことを知る。callback は pointer であって work log ではない。Redmine journal (または Asana comment) が正本であり続ける。これは `## Handoff ライフサイクル` step 5 (受信側は結果を記録した後に依頼元へ通知する) と `references/safety.md` `## 結果通知の境界` の multi-lane 特化であり、どちらも置き換えない。

### coordinator callback を要する state

sublane が次のいずれかの state に到達したら callback を送る。各 state はまずそれ自身の durable gate / journal として先に記録される。callback はそれを指すだけである:

- **blocked / needs clarification** — sublane が決定または unblock の入力なしには先へ進めない。
- **implementation_done** — 実装が完了し記録された (記録は completion ではない)。
- **review_request** — US-level audit 依頼 (または preset 例外下の task-level review 依頼) が投稿された。
- **review result** — review が approved になった、または依頼元の対応を要する finding が記録された。
- **commit recorded** — audit-owned commit が着地し、その hash が durable record に記録された。
- **owner close approval requested** — 作業が owner の close 承認待ちである。

これらは別の lane が行動するか view を更新しなければならない遷移である。lane 内の日常的な進捗 (進行中の実装 step) に callback は不要である。trigger は coordinator が待っている state への到達である。

### Callback 手順

1. **まず durable state を記録する。** gate journal (Start / Implementation Done / Review Request / Review / Owner Close Approval / Close、または blocked state の場合は Progress Log) は callback 送信前に Redmine issue 上に存在しなければならない。callback は journal の代替には決してならない。
2. **lane 横断 callback は sublane の Codex が所有する。** sublane の内側では、Claude 実装者が自分の state を durable record に表出させ、自 lane の Codex に通知する (same-lane addressing)。その上で、sublane の Codex — その lane の coordinator 窓口 actor — が callback を lane 境界の向こうへ運ぶ。single-actor の sublane は両方の役割を兼ねるが、それでも callback の宛先は coordinator の Codex であり、別 lane の Claude には決して宛てない。
3. **coordinator lane の Codex pane を解決する。** coordinator のユーザー窓口 actor は Codex であるため、callback はその Codex pane に宛てる。これは lane 境界を越えるため dispatch と同じ規則に従う — 両 lane が 1 つの物理 cockpit session を共有していても、target (ここでは coordinator) lane の Codex を経由する (Redmine #11812 routing 訂正)。通常経路は `coordinator` target である (Redmine #12015)。`--target coordinator` は、送信者自身の `workspace_id` を共有し default (primary-checkout) lane に座る Codex pane に解決されるため、sublane が coordinator の `%pane` を手選びすることはない。これは **workspace-scoped** であり (別 workspace の coordinator には決して解決しない)、かつ **fail-closed** である — 送信 pane が不明、workspace identity を持たない、または default-lane の Codex が不在 / 一意でない場合は、推測せず候補と `--target %pane` の retry hint を出して停止する。`coordinator` が fail-closed で停止したときに pane を確認または手選びする手段は、引き続き compact な target discovery (`## 自然名 target への handoff`、`mozyo-bridge agents targets`) である。
4. **短く anchor 付きの通知を送る。** `mozyo-bridge handoff send --to codex --target coordinator --mode standard` を使い (coordinator pane は通常 active split ではないため `standard` が通常 mode。`coordinator` が解決できない場合は明示的な `--target <coordinator_codex_%pane> --target-repo auto` に fall back する)、本文に durable anchor — issue id、gate journal id、到達した state、そして関連する場合は commit hash — を入れる。最小の state + pointer にとどめる。retry 計画・試行 command・詳細 finding は durable record 側に置き、callback chat には置かない。callback outcome (sent / blocked-with-reason / not-attempted) を durable record に記録する — 無言の re-poke は禁止であり、callback 欠落は `progress_without_callback` として検出可能なままでなければならない (`## Stall / no-progress 検出標準`)。
5. **送信後に coordinator pane を poll しない。** delivery は pointer にすぎない。coordinator が durable anchor を読み、次の action (audit、owner 承認収集、close、またはさらなる routing) を決める。sublane は自身の durable record から再開する。

### 完了チェックリスト (handoff-worthy state を完了扱いする前に実行する)

上の手順は必要条件だが、それ単体では skip されやすい。sublane は gate journal を記録して*自分の same-lane* Codex に通知しただけで、main coordinator への lane 境界を一度も越えないまま state を完了扱いにできてしまう。これがまさに `progress_without_callback` の failure である。durable な Redmine state は前進する (implementation_done → review_request → review approval → owner-close-waiting) のに、main coordinator は手動の Redmine sweep によってしかそれを知り得ない (Redmine #12038 j#59102。review_request / review が sublane 自身の Codex `%1075` にだけ届き、`--target coordinator` 経由で main coordinator に一度も届かなかった)。callback を事後検出ではなく前提条件にするため、**sublane は下記のどの state も、すべての box が durable record に記録されるまで完了とみなさない。** このチェックリストは step 1–5 の enforce 可能な形であり、新しい gate や transport kind を追加しない。

`### coordinator callback を要する state` の各 state — 具体的には **implementation_done、review_request、review result (approval または findings)、owner-close-waiting、blocked** — について、以下のすべてを確認する:

1. **durable gate journal を記録済み。** その state 自身の gate / Progress Log journal がまず Redmine issue 上に存在する (step 1)。callback はその代替には決してならない。
2. **same-lane への表出は callback ではない。** 自 lane の Codex への通知 (Claude → same-lane Codex の hop) は same-lane addressing を満たすが、それは coordinator callback では*ない*。main coordinator は別 lane の Codex である。review_request / review を sublane 自身の Codex にだけ届けると、coordinator は盲目のままになる。これが #12038 が skip した具体的な step である。
3. **`--target coordinator` 経由で main coordinator へ lane 横断 callback を送信済み。** durable anchor (issue id、gate journal id、到達した state、関連する場合は commit hash) を添えて `mozyo-bridge handoff send --to codex --target coordinator --mode standard` を実行する。`--target coordinator` が通常経路である — workspace-scoped かつ fail-closed であるため、happy path で sublane が coordinator の `%pane` を手選びすることは決してない。明示的な `--target <coordinator_codex_%pane> --target-repo auto` (`mozyo-bridge agents targets`、`## 自然名 target への handoff` で解決) への fall back は、`coordinator` が解決できないときに限る。
4. **callback outcome journal を記録済み** (下の template を参照)。結果は正確に次の 3 つのいずれかである:
   - `sent` — target、command、観測した landing marker。
   - `blocked` — blocked の理由、候補 pane (`agents targets` の行)、具体的な `--target %pane` retry command。これにより次回の試行が durable record から replay 可能になり、gap は `progress_without_callback` として検出可能なままになる。
   - `not-attempted` — 明示的な理由がある場合のみ (例: この lane 自身が coordinator lane であり、lane 横断 hop が適用されない)。沈黙は決して有効な outcome ではない。

box 1–4 が未完の state は、作業がどう位置づけられていようと handoff されていない (`## Handoff ライフサイクル` の "every" rule)。`--target coordinator` が失敗した場合、state を回復可能に保つのは box 4 の `blocked` outcome であって沈黙ではない。pane を無言で re-poke しない。

### Callback outcome journal テンプレート

これを同じ Redmine issue 上の journal として記録する (または field を gate journal に畳み込む)。これにより callback は監査可能になり、欠落は検出可能なままになる:

```markdown
## coordinator callback
- state: implementation_done | review_request | review_result | owner_close_approval_waiting | blocked
- durable_anchor: #<issue_id> j#<gate_journal_id>
- target: coordinator (`--target coordinator`) | <coordinator_codex_%pane>
- result: sent | blocked | not-attempted
- on sent: command + observed landing marker
- on blocked: reason / candidates (`agents targets` rows) / retry command (`--target %pane --target-repo auto`)
- on not-attempted: explicit reason (e.g. this lane is the coordinator lane)
- commit_hash: (when the state carries one)
```

### 本手順が緩めない境界

- **Redmine / Asana が正本であり続ける。** callback の pane message は pointer にすぎない。callback を受けた coordinator は、行動する前に名指しされた journal / comment を読む — pane scrollback や `status` や `doctor` ではなく。
- **Codex 経由の lane 横断 routing は維持される。** callback は `## 自然名 target への handoff` と整合的に coordinator lane の Codex に宛てる。物理 cockpit session の共有は sublane-Claude から coordinator-Claude への直接送信を許可しない。lane 横断は依然として Codex-to-Codex を意味する。
- **operator 固有の identity を配布 default に入れない。** 本手順は portable である。具体的な coordinator pane id、session の nickname、host path、lane policy は operator 自身の runtime に属し、OSS skill / preset 本文には属さない。

## 名前付き cockpit group と複数 local cockpit session

単一の operator は、すべての checkout を 1 つの共有 grid に畳み込む代わりに、複数の local cockpit を同時に — 例えば project family ごとに 1 つの cockpit grid を — 運用できる。そのための portable モデル (Redmine #11853、#11850 PoC の findings に基づく) は、**grouping** と **routing identity** を別の関心事として保ち、multi-cockpit layout が routing 境界を無言で弱めることが決してないようにする。

### cockpit group は名前付き tmux session である

- **cockpit group は名前付き tmux session である**。`mozyo cockpit --session <name>` / `mozyo layout apply cockpit --session <name>` は明示的な session 名の下で cockpit を作成または adopt する。default session はそうした group の 1 つにすぎない。無関係または半関連の別々の project family は、1 つの共有 grid 内の追加 column ではなく、**別々の名前付き cockpit session** に属する。
- cockpit group は discovery では `mozyo-bridge agents targets` の `SESSION` column (`--json` では `session` field) として現れる。これは「この pane はどの cockpit に住んでいるか」の grouping key であり、自然名が group を含意するときの正しい disambiguator である — 自然名解決の際は `--session <name>` で絞り込む。

### grouping は表示であり、identity は resolver / lane / workspace に残る

- cockpit session とその window / iTerm 上の近接は**表示上の grouping** であり、routing や identity の正本ではない。window 名は layout / view の属性である (`agents targets` の role モデルを参照: 明示的な `@mozyo_agent_role` pane option が window 名より authoritative である)。session 名、window 名、画面上の隣接を role / routing の判断に昇格させない。
- routing identity は resolver が置いた場所に残る: **agent role** は pane-option role resolver (`role` / `role_source` / `confidence`) から、**checkout lane** は lane identity (`lane_id` / `lane_label`) から、**workspace** は解決済み workspace identity (`workspace_label` / `repo_root`) から。送信を検証済み workspace に束縛するのは `--target-repo` gate であって、cockpit session 名や title 文字列ではない。
- 関連 project group を一望監視のために物理的に隣接する window に置くことは、*表示としてのみ*許される。それを暗黙の「同じ group → 直接送信」shortcut にしてはならない。delivery は常に明示的かつ gated である。

### group / project 横断 consultation は target group の Codex gateway を経由する

- ある cockpit group から別の group へ渡る handoff は governance 境界の横断であり、上記の lane 横断・session 横断の規則とまったく同じに扱う: **target group の Codex** pane を経由させ (`--to codex --target <target_group_codex_%pane> --target-repo auto`)、その Codex に durable な Redmine / Asana anchor を読ませて自 group の context を検証させ、その後にのみ任意でその group の local Claude へ route する。discovery が一意に解決した場合でも、別 cockpit group の解決済み Claude pane に直接宛てない。
- これは高レベルの workspace 横断 consultation primitive が乗る標準 rail である (Redmine #11779 `handoff cross-workspace-consult`)。wrapper が着地しても、この gateway と identity の gate を隠したり弱めたりしてはならない。group 横断依頼の durable な正本は Redmine / Asana 上に残り、pane 送信は pointer にすぎない。

### 本モデルが緩めない境界

- **複数 cockpit session は session 横断 Claude shortcut を作らない。** 作業を複数の名前付き cockpit group に分けても、session を跨ぐ `--to claude` は許可されない。それは引き続き CLI で reject される (`blocked` / `cross_session_claude`)。group に名前を付けることは gateway target を見つけやすくするだけで、境界を動かさない。
- **private な cockpit 構成を OSS default に入れない。** どの具体的な project family が cockpit を共有するか、session の nickname、host/window layout、operator 固有の grouping policy は private な運用 policy である (採用 repo の public / private 境界 rule を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は*名前付き cockpit session が group 単位であり grouping は identity ではない*ことである。具体的な cockpit 構成は operator 自身の runtime / private runbook に属し、配布される skill や preset default には属さない。

## Coordinator stop と next-action 標準

coordinator lane (audit、owner 向け判断、release、sublane orchestration を所有する main Codex lane) は*正しく*保守的である。close を owner 承認で gate し、guardrail の bypass を拒む。しかし構造化された stop を伴わない保守性は throughput の sink になる — coordinator が owner の判断を待って停止し、次に何が来るかを何も言わなければ、それに依存するすべての sublane も遊休し、進捗が可能な場所でも cockpit は blocked に見える (Redmine #11860、#11850 multi-lane PoC より)。修正は coordinator の慎重さを下げることではなく、すべての stop に next-action 提案を持たせ、owner が一手で行動でき、無関係な作業が動き続けるようにすることである。

本標準は `## Sublane の coordinator callback` (sublane が coordinator へ*向けて*報告する方法を統治する) の coordinator 側の補完である。coordinator が stop を owner と他 lane へ*外向きに*提示する方法を統治する。

### coordinator stop とは何か

coordinator stop とは、coordinator lane が無人でできることを終え、何かを待っている任意の時点である: owner の判断、close 承認、owner が批准すべき review の結論、または次の作業の選定。これは正常で想定内の state であって failure ではない。本標準はその state を*どう記録し提示するか*についてであり、それを避けることについてではない。

### durable record が先、pane pointer は後

- stop は関連 issue 上の Redmine journal として記録する (Progress Log、または stop が待っている gate journal — 例えば owner close approval を待つ Review Request)。journal が正本であり、owner への pane 通知はそれへの pointer にすぎない。`## Handoff ライフサイクル` および `## Sublane の coordinator callback` とまったく同様である。
- stop の理由や next-action menu を pane scrollback に*だけ*書き込まない。状況を再構成する owner や別 lane は durable journal を読む。`status` / `doctor` / scrollback ではない。

### 自律範囲と owner 承認範囲を分ける

停止する前に、coordinator は保留中の next action を 2 つの bucket に分類し、後者のみで停止する:

- **自律範囲** — coordinator が owner 承認なしに正本から実行してよい action: durable record の読解と要約、review finding の投稿、承認済み実装の sublane への routing、audit 結論の記録、queue 済み backlog task の空き sublane への dispatch、または preset が既に許可する Repo-Local Guardrail Autonomous Lane の編集。自律 action が利用可能なら、coordinator は停止せずそれを実行する。
- **owner 承認範囲** — 中央 preset の `Close Approval Separation` と `### Owner Close Approval Delegation` の carve-out 一覧に従い owner 判断で gate される action (close 承認、release / publish、credential / auth 変更、破壊的操作、scope / stakeholder 判断、その他の carve-out)。ここで coordinator は停止して尋ねる。standing delegation の下でも carve-out action を self-authorize しない。

stop が正当化されるのは、残る next action が owner 承認範囲*のみ*になったときだけである。自律 action が残っているなら、その stop は早すぎる。

### すべての stop に next-action 提案を添える

coordinator が実際に停止するとき、durable journal (とそれを指す pane pointer) は、owner が一手で判断できるよう短い 3 部構成の提案を載せる:

1. **なぜ停止するのか** — 待っている具体的な gate または判断を、journal id に anchor して示す (例: 「US #NNNN review approved; Close Approval Separation に従い owner close approval 待ち」)。
2. **承認されたら何が起きるか** — owner が承認した瞬間に coordinator が取る具体的な action (例: 「on approval: owner_close_approval journal を記録し US #NNNN を closed へ移す」)。曖昧な「進める」ではなく、次の command レベルの step として述べる。
3. **承認なしで進められるものは何か** — lane 群を遊ばせないために、coordinator または sublane が*今*拾える gate されていない代替作業があるかどうか (例: 「meanwhile: sublane #MMMM の実装は継続可能; backlog task #PPPP は dispatch 可能」)。owner なしでは本当に何も進められないなら、含みに残さず明示的にそう述べる。

提案は短く anchor 付きに保つ。retry 計画、command の transcript、詳細分析は durable record 側に置き、pane pointer には置かない。

### stop で lane 群を遊ばせない

coordinator stop がすべての sublane を無言で凍結してはならない:

- **gate された作業は、保持した pane ではなく queue に返す。** ある作業単位について owner 判断で block されたとき、coordinator は next-action queue / backlog に戻り、cockpit 全体を gated item の上で止める代わりに、ready で gate されていない task を空き sublane に dispatch する。gated item は owner が行動するまで自身の durable journal 上に park されたままであり、無関係な lane を block しない。
- **coordinator だけを待つ sublane には明示的な pointer を与える。** sublane の次の step が coordinator の gate された判断に依存する場合、その依存を sublane の issue に記録する (「#NNNN j#JJJJ の coordinator 判断待ち」と記す Progress Log)。これにより sublane は説明なく stall して見えるのではなく、durable anchor 上に park される。coordinator の判断 journal が載ったら、sublane はその anchor から再開する。
- **下された owner 判断はそれ自体が durable journal である** (owner_close_approval journal、または Design Consultation への回答)。coordinator はそこから再開する。判断を促した pane 上のやり取りは record ではない。

### 本標準が緩めない境界

- **next-action 提案は self-authorization ではない。** 「承認されたら US #NNNN を close する」と提示しても、別建ての owner close approval journal なしに coordinator が close できるようにはならない。提案は owner の一手判断を容易にするものであり、Close Approval Separation の境界やいかなる carve-out も動かさない。
- **coordinator は sublane の Claude pane で owner 承認を収集しない。** owner 向け判断は coordinator lane の Codex に留まる (中央 preset の `### Owner Close Approval Delegation` を参照)。本標準は stop の*提示方法*を変えるのであって、owner 向けやり取りの所有者を変えない。
- **operator 固有の policy を OSS default に入れない。** 具体的な next-action menu、throughput 目標、その operator がどの backlog を先に drain するか、private な優先順位 rule は operator の runtime policy である (採用 repo の public / private 境界 rule を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は*すべての coordinator stop が durable な理由と 3 部構成の next-action 提案を記録し、ready な作業を queue に返す*ことである。operator の具体的な queue と優先順位は operator 自身の runbook に属し、配布される skill や preset 本文には属さない。

## Owner 承認の集約

`## Sublane の coordinator callback` は「owner close approval requested」を複数ある callback state の 1 つとして挙げ、`## Coordinator stop と next-action 標準` は coordinator が stop をどう提示するかを統治する。本節は、その 2 つが暗黙に残している特化を固定する: **owner-approval-waiting は、常に単一の owner 窓口 — main coordinator Codex — に収束しなければならない唯一の state class であり、それを生んだ sublane の内側で決して解決しない** (Redmine #11867、#11855 / #11860 multi-lane PoC より。そこでは review-approved / owner-close-waiting が Redmine に記録されたが、coordinator へ集約されず、cockpit 上では停止して見えた)。cockpit の sublane が増えても、「いま owner を待っているのは何か」が pane ごとの探索になってはならない。

### owner 窓口の集約点は main coordinator Codex の一点である

owner 承認を収集する actor はちょうど 1 つ、main coordinator lane の Codex である。すべての owner-approval-waiting state は、どこで発生してもその Codex に callback され、owner は sublane pane 群に散らばった判断ではなく、統合された 1 つの queue を見る。

- **sublane は owner 承認を自 lane 内で決して解決しない。** sublane の Codex と sublane の Claude は待機 state を durable record に記録して callback する。自分の pane で owner 判断を求めたり、収集したり、批准したりしない。これは `references/safety.md` `## 結果通知の境界` および中央 preset の `### Owner Close Approval Delegation` と同じ境界 — coordinator が owner 向けやり取りを所有する — を集約方向に適用したものである。
- **owner が一度で行動する場所が coordinator である。** すべての owner-approval-waiting state がそこに収束するため、owner は一箇所から批准 (または保留) し、結果として生じる `owner_close_approval` journal (または Design Consultation の回答、その他の owner 判断 journal) が、発生元 lane が再開の起点とする durable record として載る。

### owner-approval-waiting として callback すべき state

汎用の callback 一覧に加えて、次の 2 つが owner-approval class であり、常に coordinator Codex へ集約する:

- **owner close approval waiting** — UserStory または standalone issue が Review Gate / US-level audit を通過し、`Close Approval Separation` の上に park され、Close Gate の前に owner の close 承認を待っている。
- **owner-action-needed** — sublane が自律範囲を使い切り、残る唯一の next action が sublane 自身では下せない owner 判断を要する、その他すべての時点: scope / stakeholder 判断、`### Owner Close Approval Delegation` 下の carve-out action、owner のみが行える unblock、または owner (Codex ではなく) が回答すべき Design Consultation。これは close 承認より広く、無言で「blocked」に畳み込まれないよう明示的に命名されている。

各 state はまずそれ自身の durable gate / Progress Log journal として載る。callback はそれを指す pointer にすぎず、`## Sublane の coordinator callback` とまったく同様である。

### 承認待ち集合の列挙は pane 数に依存しない

未処理の owner queue に対する coordinator の把握は、pane が増えても劣化してはならない。portable な不変条件: **owner-approval-waiting 集合は durable record の性質であり、durable record から列挙できる。pane の走査によってではない。**

- 「いま owner を待っている issue」の authoritative な一覧は、各 lane が記録した owner-approval-waiting の gate journal / status を Redmine に query して再構成する — 各 pane の scrollback や `status` や `doctor` を歩き回るのではない。現在表示されていない pane や、その後 retire された sublane があっても、待機中の issue は queue から落ちない。待機は pane ではなく issue の上に住んでいるからである。
- これが、前節の callback が queue ではなく *pointer* である理由である。coordinator はたまたま受け取った callback から queue を再構築するのではなく、durable record から queue を導出し、callback は view をいつ refresh すべきかを知るためだけに使う。両者は同じ gate journal に anchor するため整合が保たれる。
- pane 数、cockpit group の layout、どの sublane が開いているかは表示上の事実であり、承認待ち queue は governance 上の事実である。両者を分けて保つことは、cockpit 運用モデルと同じ `Identity / Routing / Display / Governance` 分離である。

### 本標準が緩めない境界

- **集約は self-authorization ではない。** owner-approval-waiting state を coordinator に収束させても、coordinator が owner の代わりにそれらを承認できるようにはならない。引き続き `Close Approval Separation` に従って別建ての owner 判断 journal を記録し、standing delegation の下でも carve-out を self-authorize しない。
- **durable record が正本であり続ける。** 列挙された queue とすべての callback は Redmine journal に anchor される。coordinator は待機 item に行動する前にその journal を読む。pane scrollback ではない。
- **operator 固有の policy を OSS default に入れない。** operator が待機集合の列挙に使う具体的な Redmine filter / saved query / status mapping や、その queue の private な優先順位付けは operator の runtime policy である (採用 repo の public / private 境界 rule を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は *owner-approval-waiting が常に単一の coordinator Codex に集約され、pane 数に依存せず durable record から列挙できる*ことである。具体的な query は operator 自身の runbook に属し、配布される skill や preset 本文には属さない。

## Stall / no-progress 検出標準

`## Sublane の coordinator callback` は、sublane が handoff-worthy state に到達したとき coordinator へ*向けて*どう報告するかを定義する。しかし callback は best-effort の pointer であり、成長する cockpit では単に届かないことがある: sublane の Codex が routing callback を一度も記録しなかった、durable record は前進したが何もそれを指さなかった、あるいは送信自体が target 解決に失敗した。そうなると coordinator には沈黙しか見えず、lane が本当に blocked なのか、まだ作業中なのか、すでに完了しているのかを判断するために、現状では Redmine・worktree・pane を手で poll しなければならない (Redmine #11880、#11854 sublane PoC より)。本標準はその検出を機械的かつ durable-record-anchored にし、callback の欠落が cockpit 全体を stall させる代わりに graceful に degrade するようにする。

本標準は `## Sublane の coordinator callback` の failure-mode 側の補完である。あちらは happy path (sublane が前進した state を指す) を扱い、こちらは pointer が欠落または遅延したとき coordinator が何をするかを扱う。`## Coordinator stop と next-action 標準` や `## Owner 承認の集約` を緩めない — 検出された stall も、同じ durable journal と同じ単一の owner 窓口集約点を通じて解決する。

### stall candidate は pane ではなく durable record から定義する

**stall candidate とは、handoff は delivery されたが、期待される次の durable journal が operator の許容 window 内に現れていない作業単位**である。「delivered」とは、durable な dispatch journal — Start / implementation_request、または coordinator の routing journal — が issue 上に存在することを意味する (依頼は送信前に記録される。`## Handoff ライフサイクル` に従う)。「期待される次の journal が現れていない」とは、dispatch が待っていた gate / Progress Log journal の不在を意味する。この両半分とも Redmine issue から読む。pane scrollback からではない。

pane の沈黙、空の `status` / `doctor`、clean な worktree は、せいぜい*傍証*の signal であり、どれも trigger ではない。lane は何もまだ打ち返していなくても生きて作業中でありうるし、callback だけが欠落した完了済みでもありうる — したがって沈黙も clean な tree もこれらの場合を区別しない。trigger は「delivered な dispatch journal + 欠落した期待 durable journal」であり、これは issue 単体から再構成でき、retire 済みや非表示の pane にも耐える。

### coordinator がどの next state を待っているかを分類する

ある単位が stall candidate になったとき、coordinator はそれを単一の「stalled」label に畳み込まない。durable record から、issue が実際にどの state にあるかを分類する — PoC が顕在化させた 4 つの state である (#11880 j#57539):

- **`no_progress_after_handoff`** — delivery は成功したが、より新しい durable journal がまったく存在しない。lane は本当に blocked かもしれないし、実装の途中かもしれないし、一度も開始していないかもしれない。coordinator が次に読むのは sublane 自身の issue / Progress Log であり、その pane ではない。
- **`progress_without_callback`** — より新しい durable journal は*存在する* (例えば implementation_done や review_request) が、coordinator callback / ack がそれを指していない。作業は止まって*いない*。pointer だけが欠けている。coordinator は前進した state を直接拾う。
- **`callback_delivery_failed`** — sublane は callback を*試みた*が送信に失敗した (target 解決、window-binding preflight の reject、または stale-CLI の reject — 下記参照)。試行の durable record が存在するはずであり、coordinator はそれを読んで target を再解決する。
- **`callback_not_attempted`** — durable な進捗は存在するが、callback も receive-method journal も記録されなかった。これは sublane 側の process gap であり、coordinator の盲点ではない。coordinator はそれを記録して nudge する。

coordinator は issue の最後の journal を読み、それがどの gate を待っていたのかを問うことでこれらを区別する — **blocked、no-progress、still-working、implementation_done** のいずれか。判別子は durable な gate の連なりであり、pane では決してない。

### 検出と再通知を durable journal として記録する

stall check とあらゆる再通知は、それ自体が durable な event であり、一過性の pane 操作ではない:

- coordinator がある単位を stall candidate と結論して再通知または escalate するとき、**その事実を issue 上に記録する** — stall 分類、何が欠けていたか、再通知 target を記す Progress Log — #11854 j#57526 が stall check と nudge を記録したのとまったく同様に。journal を残さない無言の re-poke は次の coordinator から見えず、許されない。
- 解決が `progress_without_callback` である場合、coordinator は state を直接拾ったことを記録して再開する。durable record が既に done と示す作業を re-dispatch しない。
- 再通知の pane 送信は pointer のままである。stall 分類と retry 計画は journal 側に置き、callback chat には置かない。

### stale CLI は handoff / callback 中の独立した stall mode である

routing callback は、lane が遊休しているからではなく、lane の *tooling* が壊れているために欠落することがある: target-lane の Codex は生きて推論していたのに、その dispatch / callback が stale なインストール済み CLI (例えば `agents targets` より古く、それを invalid choice として reject するインストール済み `mozyo-bridge`) に block され、routing callback journal が一度も記録されなかった (#11880 j#57555)。coordinator はこれを `callback_delivery_failed` の sub-case として扱い、「no progress」と誤読しない:

- stall check の際、durable record が「lane は routing / preflight step に到達し、その後沈黙した」と示すなら、lane が遊休と結論する前に tooling の failure (stale CLI、window-binding preflight) を疑う。
- portable な規則: インストール済み CLI が dispatch や callback の必要とする機能に遅れている可能性があるときは、それらを載せていると分かっている runtime を優先し、runtime fingerprint を記録する。これにより送信が、必要な subcommand より古い version に無言で block されない。これは主に CLI 自体を開発する repo に適用される — `mozyo_bridge` の dogfooding では、release / install が追いつくまで、素のインストール済み `mozyo-bridge` より repo-local の source CLI (`PYTHONPATH=src python3 -m mozyo_bridge ...`) を優先する。
- stale-CLI stall を unblock する coordinator の介入 (例えば明示的な target を付けて repo-local CLI から再送する) は、それ自体を durable な Progress Log として issue に記録し、一時的な dogfooding 介入と理解する。target-lane Codex gateway モデルの置き換えではない。

### 本標準が緩めない境界

- **durable record が正本であり続ける。** stall candidate、その分類、その解決はすべて Redmine journal から導出され、Redmine journal 上に記録される。pane scrollback / `status` / `doctor` / worktree state は傍証の補助であり、trigger にも record にも決してならない。callback の欠落が durable record を降格させることは決してない。
- **検出は完了済み作業の re-dispatch ではない。** 再通知の前に coordinator は issue を読み、`progress_without_callback` (done の state を拾う) と `no_progress_after_handoff` (本当に待っている) を切り分ける。durable record が既に前進済みと示す作業を盲目的に再送しない。
- **stop / 集約の標準を bypass しない。** 検出された stall も、同じ `## Coordinator stop と next-action 標準` の next-action 提案を通じて解決し、owner 待ちの state については `## Owner 承認の集約` の同じ単一集約点を通る。stall 検出は state を見つけるのであって、close や carve-out や owner 判断を self-authorize しない。
- **operator 固有の policy を OSS default に入れない。** 具体的な許容 window (どれだけ長ければ「長すぎる」のか)、stall candidate の列挙に使う Redmine の saved query / filter、private な再通知 cadence や escalation 順序は operator の runtime policy である (採用 repo の public / private 境界 rule を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は *stall candidate が「delivered な dispatch journal + 欠落した期待 durable journal」として定義され、4 つの durable state に分類され、すべての stall check と再通知がそれ自体 issue に記録される*ことである。operator の具体的な timeout と query は operator 自身の runbook に属し、配布される skill や preset 本文には属さない。

## Sublane 完了 guardrail

`## Sublane の coordinator callback`、`## Coordinator stop と next-action 標準`、`## Stall / no-progress 検出標準` は、それぞれ multi-lane 運用の 1 本の rail を記述する。単一 version 上で複数の sublane を同時に走らせると、これらが暗黙に残す再発性の gap が顕在化した: durable な Redmine state は前進するのに、*完了条件*が「coordinator が実際にそれを見て再開できる」ではなく「gate journal が載った」と読まれるため、coordinator がまだ盲目のまま sublane は done に見える (Redmine #12213、v0.9.1 sublane PoC #12189 / #12190 / #12191 / #12207 の一般化)。本標準は handoff-worthy state の完了条件を再定義し、この gap を事後検出ではなく定義によって閉じる。新しい CLI checker や drain command は追加しない。*state がいつ complete と数えられるか*と *resume を誰が所有するか*を、将来の checker が読める固定 field の shape で固定する。

下の 4 つの guardrail は 1 つの**固定 field shape** を共有し、すべての sublane state journal に同じ field が現れて machine-readable であり続ける: `state`、`durable_anchor`、`callback_result`、`blocked_by`、`resume_condition`、`resume_owner`、`origin_reachable`。各 guardrail は自分の state に該当する subset を使う。完全な template は下の `### 固定 field の journal shape` にある。

### handoff-worthy state は callback outcome journal が載るまで完了しない

sublane state の完了条件は「gate journal が存在する」ではない — 「gate journal が存在し、**かつ** callback outcome journal が coordinator をそれに向ける」である。具体的には、**`implementation_done`、`review_request`、`review_result`、`owner_close_approval_waiting`、`blocked` は、その callback outcome journal が記録されるまで complete ではない。** それまでその state は in-flight であり、sublane の内側で作業がどれほど完了したと感じられても変わらない。

- これは `### 完了チェックリスト (handoff-worthy state を完了扱いする前に実行する)` をチェックリストから定義へと強化する: `callback_result` field (`sent` / `blocked` / `not-attempted`、沈黙は不可) は state を complete にする要素の一部であり、後回しにできる step ではない。`implementation_done` を記録して自分の same-lane Codex にだけ通知した sublane は、`implementation_done` を完了して*いない*。lane 横断の callback outcome journal がまだ負っているままである。これは `progress_without_callback` の failure (#12189 j-series: 実装 / 承認は載ったが callback / downstream resume が coordinator へ drain されなかった) を、stall の*検出*から完了の*前提条件*へ昇格させたものである。
- callback outcome journal は少なくとも `state`、`durable_anchor` (`#<issue_id> j#<gate_journal_id>`)、`callback_result` を載せる。`blocked` の `callback_result` でも、replay 可能な retry command を載せている限り記録義務は満たされる (`### Callback 手順` step 4)。禁止されるのは、callback journal がまったく無いまま state を done と扱うことである。

### dependency hold は durable record に park する (go-ahead を待たない)

別の issue に依存して先へ進めないとき、sublane は **durable な parked state を記録してそこで停止する — operator への go-ahead の質問で停止しない。** 「開始してよいか?」と尋ねて停止しても durable な痕跡は残らず、coordinator は依存を見ることも resume を所有することもできない (Redmine #12191: dependency hold が Redmine に記録される前に operator の go-ahead を待った; #12190: `blocked_by #12189` は正しく待ったが resume の責務が anchor されなかった)。

- parked-state journal は固定 field を記録する: `state: blocked`、`blocked_by` (その完了によって本 issue が unblock される issue id)、`resume_condition` (作業再開を許す durable な event — 例えば「blocked_by がその callback outcome journal に到達する」)、`resume_owner` (re-dispatch する者 — 次の guardrail に従い coordinator)、`callback_result` (parked state 自体が handoff-worthy な `blocked` state であり、他と同様に callback する)。
- park は質問でも stall でもない。sublane は人間が「go」と言うのを待って遊休しない。parked state を書き、callback し、yield する。再開は `resume_condition` が durable record 上で true になることで trigger される。operator が pane を nudge することによってではない。

### callback drain と downstream resume は coordinator が所有する

sublane が state を報告することは必要条件だが十分条件ではない — 誰かが callback を*消費*し、待っていた作業を*再起動*しなければならない。その責務は明示的に coordinator lane にある: **coordinator は callback drain (蓄積した callback outcome journal を読み、それに基づいて行動する) と downstream resume (`resume_condition` が満たされた parked な依存先を re-dispatch する) を所有する。** これを暗黙のままにしたことが #12189 → #12190 を stall させた: #12189 は approval に到達したが、その callback と #12190 の downstream resume が drain されず、#12190 は再起動に責任を持つ actor がいないまま park され続けた。

- **callback drain。** coordinator は定期的に durable record から未処理集合を導出し (`## Owner 承認の集約` と `## Stall / no-progress 検出標準` に従い、pane ではなく journal から列挙する)、各 callback をその次の action (audit、owner 承認収集、close、または re-dispatch) を取ることで消化する。coordinator を名指ししながら drain されない callback outcome journal は、sublane ではなく coordinator の backlog item である。
- **downstream resume。** `blocked_by` の issue が*自身の* callback outcome journal に到達したとき (すなわち第 1 の guardrail により complete になったとき)、coordinator が `resume_owner` として parked な依存先を lane へ re-dispatch し、その resume を依存先 issue 上の routing / Progress Log journal として記録する。parked な sublane は polling によって自己再開しない。coordinator によって durable record から再起動される。
- これは最初の 2 つの guardrail の coordinator 側の補完であり、`## Coordinator stop と next-action 標準` が `## Sublane の coordinator callback` の coordinator 側の補完であるのとまったく同じである。coordinator が owner 判断を self-authorize できるようにはならない。次の action が owner-gated である callback の drain は、依然として `## Owner 承認の集約` を経由する。

### gate へ commit hash を記録する前の origin 到達性 preflight

`implementation_done` や `review_request` の gate に書かれた commit hash は、reviewer がその commit を fetch できて初めて有用である。**gate journal に commit hash を記録する前に、その commit が `origin` から到達可能であることを検証し、結果を `origin_reachable` として記録する。** local にしかない hash (未 push、または後の rebase で orphan 化) は、auditor が到達できない anchor の上で Review Gate を block させる (Redmine #12207: commit が origin に到達する前に Review Request が投稿され、Review Gate が到達性で block した。coordinator の rebase が記録済み hash を origin 到達不能にする rebase-anchor failure mode と比較せよ)。

- preflight は具体的である: lane branch を push し、その後 hash が remote 上にあることを確認する — 例えば hash は `git rev-parse HEAD` で得て、それが `origin/...` ref 上にあることを `git branch -r --contains <hash>` (または `git ls-remote origin` / fetch) で示す。gate journal に hash と並べて `origin_reachable: true` を記録する。まだ到達可能でないなら gate は ready ではなく、hash は記録しない。
- これは `implementation_done` / `review_request` に対する前提条件であり、`Audit-Owned Commit Authority` step 6 の hash 記録の上流にある。あの step を置き換えるのではない。review へ、そして audit-owned commit record へ流れ込む hash が、書かれた時点で到達可能だったことを保証する。後の coordinator rebase が記録済み hash を無効化した場合、修正は re-anchoring の訂正 journal (rebase 後の byte-identical な commit へ指し直す) であって無言の編集ではない — だが preflight は*最初の*記録が到達不能になることを止める。

### 固定 field の journal shape

該当する subset をその state の gate / callback journal に記録する (または field を gate journal に畳み込む)。これによりすべての sublane state は監査可能になり、将来の checker が散文を parse せずに読める:

```markdown
## sublane state
- state: implementation_done | review_request | review_result | owner_close_approval_waiting | blocked
- durable_anchor: #<issue_id> j#<gate_journal_id>
- callback_result: sent | blocked | not-attempted
- blocked_by: #<issue_id> (dependency hold only; omit when not blocked)
- resume_condition: <durable event that unblocks> (dependency hold only)
- resume_owner: coordinator (dependency hold only)
- origin_reachable: true | false (implementation_done / review_request carrying a commit hash)
- commit_hash: <hash> (when the state carries one; record only with origin_reachable: true)
```

### 本標準が緩めない境界

- **本標準は completion を再定義するのであって、checker を追加しない。** 新しい drain CLI も、Redmine 自動化も、schema 変更もない (Redmine #12213 non-goals)。固定 field shape は既存の durable journal を将来の checker のために machine-readable にするだけである。今日の enforcement は「state は callback outcome journal なしには incomplete である」という定義そのものである。
- **durable record が正本であり続ける。** すべての guardrail は Redmine journal に anchor する。callback、parked state、resume、到達性の結果はすべて issue から読み、issue に記録する。pane scrollback / `status` / `doctor` からでは決してない。
- **stop / 集約 / 役割の境界を緩めない。** coordinator の drain と resume は、owner-gated な next action を引き続き `## Owner 承認の集約` を通し、stop を引き続き `## Coordinator stop と next-action 標準` に従って提示し、lane 横断 routing を引き続き Codex-to-Codex に保つ。coordinator が resume を所有しても、close や carve-out を self-authorize できるようにはならない。
- **operator 固有の policy を OSS default に入れない。** 具体的な drain の cadence、coordinator が callback 集合を sweep する頻度、private な resume 優先順位付けは operator の runtime policy である (採用 repo の public / private 境界 rule を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は *handoff-worthy state は callback outcome journal が載るまで incomplete であること、dependency hold は go-ahead を待つ代わりに durable record に park すること、callback drain と downstream resume は coordinator が所有すること、commit hash は gate に記録する前に origin 到達性を check すること* — いずれも上の固定 field shape で — である。

## Sublane 退役 drain

`## Sublane 完了 guardrail` は sublane のライフサイクルの前半を閉じる: handoff-worthy state は callback outcome journal が載るまで完了せず、callback drain と downstream resume は coordinator が所有する。ライフサイクルの*後半*は暗黙のままにしている。Version #222 で複数 sublane を同時に走らせたことでこの欠落が表面化した: lane の issue が close された後も lane / worktree / cockpit pane は無期限に生き続け、単一 version に Redmine 上は closed だが依然常駐する lane が多数蓄積する (Redmine #12214、v0.9.1 sublane PoC の一般化; #12213 の後継)。これは単なる operator の cleanup 怠慢ではない — workflow が*退役 (retirement)* を明示的な completion stage として扱ってこなかったため、誰もそれを所有せず、何もそれを安全にしない。本標準は sublane 退役を、将来の checker が読める同じ固定 field shape による、**callback drain の後に走る coordinator 所有の drain** として定義する。自動化された retire CLI や checker は追加せず、既存の lane / worktree をそれ自体が kill することもない (Redmine #12214 の non-goal)。本標準が定めるのは、*どの lane が retire candidate か*、*何が退役を禁止するか*、*破壊的な pane-kill / worktree-remove がどの safety preflight を要するか*、*どの journal が retire を挟むか* である。

退役は破壊的操作 (pane kill、worktree remove) に触れるため、completion state 群より強く gate される。以下の field は checker が読みやすい shape を共有する: `retirement_state`, `lane`, `worktree`, `pane`, `redmine_issue_state`, `retain_reason`, `downstream_consumed`, `retire_blockers`, `safety_preflight`, `durable_anchor`。各 state は該当する subset を使う。完全な template は下の `### retire_ready / retired journal shape` にある。

### closed lane は既定の retire candidate である

lane の Redmine issue (その lane が dispatch された対象の UserStory または standalone issue) が **closed** になったとき、その lane は既定で `retire_candidate` となる: その worktree と cockpit pane は除去予定に入る。ここでの close とは durable な close を指し、`implementation_done` でも Review Gate approval でもない — `implementation_done` は completion ではなく (base preset)、Review Gate approval は close ではない。issue が単に `implementation_done` や `owner_close_approval_waiting` にある lane は retire candidate では**ない**。それは `## Sublane 完了 guardrail` の観点では依然 in-flight である。

- `retirement_state: retire_candidate` は lane が*原則として*適格であることを記録するものであり、破壊的操作を許可するものではない。退役は、下記の禁止条件がすべて解消し safety preflight が green になって (`retirement_state: retire_ready`) 初めて進行し、kill / remove 自体は `retirement_state: retired` として記録される。
- candidate 集合は durable record — Redmine 上の lane の issue state — から導出し、pane scrollback / `status` / `doctor` からは導出しない。idle に*見える* pane は、その issue が closed でない限り retire candidate ではない。closed な issue は、pane を二度と見に行かなくても、その lane を candidate にする。

### dependency ancestor lane は downstream 消費まで retain する

closed な lane が常に即座に退役して安全とは限らない: branch が **downstream lane がこれから merge / rebase する対象の ancestor** である lane は、その downstream 消費が完了するまで生存しなければならない。さもなければ downstream の rebase が base を失う。そのような lane は `retire_candidate` の代わりに `retirement_state: retain_until_downstream_consumed` を記録し、`retain_reason` (どの downstream issue / lane がまだこの branch に依存しているか) と `downstream_consumed: false` を添える。

- これは `## Sublane 完了 guardrail` の dependency hold の退役側の鏡像である: あちらでは *dependent* が ancestor の完了まで `blocked_by` に park するのに対し、こちらでは dependent が消費し終えるまで *ancestor* が退役から保留される。この hold は durable record に anchor され、operator が依存関係を覚えていることには依存しない。
- retain が解除される — `downstream_consumed: true` となり lane が `retire_candidate` になる — のは、downstream lane が ancestor commit へ merge または rebase を終え、その消費自体が記録された (downstream lane の merge / rebase journal) ときである。この遷移は退役 drain の一部として coordinator が所有する。ancestor lane が polling で自己解除することはない。

### hold 条件が open の間は退役を禁止する

issue が closed の lane は candidate だが、以下のいずれかが open の間、退役は**禁止**される。それぞれが `retire_blockers` の entry であり、`retire_blockers` list が空でない限り `retirement_state: retire_blocked` であって、決して `retire_ready` にはならない:

- **active lane** — lane がまだ作業中である (未消化 gate のない closed issue 以外のあらゆる state)。
- **review pending** — Review Request が未消化で、Review Gate 結果が無い。
- **owner approval pending** — `owner_close_approval_waiting` で owner_close_approval journal がまだ無い (close は実在せず、したがって lane は closed ではない)。
- **unresolved callback** — callback outcome journal がまだ載っていない handoff-worthy state (`## Sublane 完了 guardrail` の第一 guardrail)。callback drain の後に退役するということは、callback は既に drain 済みであるということである。
- **dirty worktree** — lane の worktree に uncommitted / untracked な変更がある。除去するとそれらが破棄される。
- **pending prompt** — lane の pane に queue 済み / 未 submit の prompt がある。kill すると in-flight の入力が失われる (`## 同一レーン Claude dispatch` の submit 完了に関する懸念)。
- **unpushed commit** — lane branch 上に `origin` から到達できない commit がある。worktree を除去するとそれが orphan になり得る (`## Sublane 完了 guardrail` の `origin_reachable` preflight を、記録済みの gate hash だけでなく lane の*すべての* commit に適用したもの)。
- **unknown target identity** — lane / worktree / pane の identity が durable record / resolver から解決されていない。未検証の target に対する破壊的操作は禁止される (確実に特定できない pane を kill しない)。

### 破壊的操作の safety preflight

破壊的な pane-kill / worktree-remove の前に、coordinator は、すべての field が true でなければならない `safety_preflight` を実行し記録する。green な preflight こそが lane を `retire_candidate` から `retirement_state: retire_ready` へ進めるものである:

- `redmine_closed: true` — lane の issue が durable に close されている (`implementation_done` ではなく、Review approval のみでもない)。
- `worktree_clean: true` — worktree での `git status` に uncommitted / untracked な変更が無い。
- `origin_reachable: true` — lane branch 上のすべての commit が `origin` から到達可能である (branch を push してから確認する — 例えば `git branch -r --contains <hash>` や branch tip に対する `git ls-remote origin`)。これにより worktree 除去で作業が失われない。
- `pending_prompt_absent: true` — pane に queue 済み / 未 submit の prompt が無い。
- `callback_drained: true` — この lane に対する coordinator callback drain が完了している。未消化の callback outcome journal の負債が無い。
- `target_identity_known: true` — kill / remove の前に、pane id / worktree path / lane branch が durable record / resolver から確実に解決されている。

いずれかの field が false であれば lane は `retire_blocked` にとどまる。破壊的操作は実行せず、open な field が先に解消すべき `retire_blockers` entry となる。

### retire_ready / retired journal shape

破壊的操作は、lane の issue 上の 2 つの durable journal で挟む: `retire_ready` (preflight green、これから退役する) と `retired` (pane kill 済み / worktree 除去済み)。将来の checker が散文を parse せずに読めるよう、該当する subset を記録する:

```markdown
## retire_ready
- retirement_state: retire_ready
- lane: <lane id / branch name>
- worktree: <worktree path>
- pane: <pane id>
- redmine_issue_state: closed
- retain_reason: none | <downstream issue still consuming this ancestor>
- downstream_consumed: true | n/a
- retire_blockers: []  (must be empty to be retire_ready)
- safety_preflight: redmine_closed=true worktree_clean=true origin_reachable=true pending_prompt_absent=true callback_drained=true target_identity_known=true
- durable_anchor: #<issue_id> j#<close_journal_id>

## retired
- retirement_state: retired
- lane: <lane id / branch name>
- worktree: <worktree path> (removed)
- pane: <pane id> (killed)
- durable_anchor: #<issue_id> j#<retire_ready_journal_id>
```

### 退役 drain は callback drain の後に coordinator が所有する

退役は coordinator の責務であり、順序は明示的である: **coordinator は callback drain の後に退役 drain を実行する。** callback drain (`## Sublane 完了 guardrail`) は未消化の callback と downstream resume を解消する。lane の callback が drain され issue が close されて初めて、coordinator はその lane を退役対象として評価する。この順序には意味がある — callback の負債が残る lane を退役させると、handoff-worthy state を取りこぼすことになる。

- **candidate 集合を durable record から導出する。** coordinator は issue が closed の lane を Redmine から列挙する (`## Owner 承認の集約` / `## Stall / no-progress 検出標準` の pane 数非依存の列挙に従う) のであって、pane 一覧からではない。`retain_until_downstream_consumed` の lane は、その `downstream_consumed` が反転するまで retire 集合の外にとどまる。
- **preflight を実行してから退役する。** 各 candidate について coordinator は `retire_blockers` を解消し、green な `safety_preflight` とともに `retire_ready` を記録し、破壊的操作を実行し、`retired` を記録する。`retire_ready` に到達できない lane は `retire_blocked` にとどまり、drain されていない callback と全く同様に coordinator の backlog item となる。
- **退役は close を自己承認しない。** この drain は issue が*既に* closed の lane を退役させるのであって、lane を退役可能にするために issue を close することは決してない。lane が完了に見えるのに issue が close されていない場合は、通常の close 経路 (US-level audit → `## Owner 承認の集約` → owner_close_approval) を通すのであって、退役経由にはしない。

### 本標準が緩めない境界

- **stage を定義するのであって、checker を追加するのではない。** 自動化された retire CLI も、Redmine automation も、schema 変更も無い (Redmine #12214 の non-goal)。固定 field shape は将来の checker のために retire journal を machine-readable にするだけであり、今日の enforcement は、退役が coordinator 所有であり、candidate で gate され、禁止条件で gate され、preflight で gate されるという定義そのものである。
- **既存のものは何も退役させない。** 本標準は常駐している Version #222 の lane / worktree / pane を kill しない (Redmine #12214 の non-goal — 実際の kill / remove は scope 外)。今後どのように退役を行うかを定義するものである。
- **durable record が正本であり続ける。** candidate 集合、retain hold、blocker、preflight、retire の bracket はすべて issue から読み issue に記録するのであって、pane scrollback / `status` / `doctor` からでは決してない。idle に見える pane は retire signal ではなく、closed な issue が retire signal である。
- **破壊的操作は gate と target 特定の下にとどまる。** pane-kill / worktree-remove は、green な `safety_preflight` と確実に特定された target がある場合にのみ実行する。未検証の target identity はそれ自体が `retire_blockers` entry である。本標準は、close されていない issue、dirty な worktree、未 push の commit、不明な target に対する破壊的操作を決して許可しない。
- **operator 固有の方針を OSS 既定に持ち込まない。** 具体的な退役 cadence、closed-lane candidate の列挙に使う Redmine saved query、closed な lane を退役させるまでの private な猶予 window は operator の runtime policy である (採用 repo の public / private 境界規約を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は、*closed な lane が retire candidate であること、dependency ancestor は downstream 消費まで retain されること、open な hold 条件が退役を禁止すること、破壊的操作が green な safety preflight を要すること、そして coordinator が callback drain の後に退役 drain を所有すること* — いずれも上記の固定 field shape で、`retire_ready` と `retired` に挟まれる形で — である。

## Dispatch 後の fill loop

上の各節はそれぞれの drain を個別に定義している: `## Sublane の coordinator callback` (callback の受け入れ)、`## Coordinator stop と next-action 標準` (stop の提示)、`## Owner 承認の集約` (owner 待ちの収束)、`## Stall / no-progress 検出標準` (欠落した callback)、`## Sublane 完了 guardrail` (callback drain + downstream resume)、`## Sublane 退役 drain` (callback drain 後の退役)。これらを 1 つの coordinator turn に束ねるものがここには欠けていた: **独立した ready な作業と lane capacity の両方が残っているのに、sublane を 1 つ dispatch して停止する coordinator は、並列に走れるはずの pipeline を黙って直列化する。** 本節は pipeline-first の fill 規律を移植し、各 drain と次の dispatch が別々の one-shot gate ではなく 1 つの loop として読めるようにする。これは repo-local spine `vibes/docs/logics/coordinator-sublane-development-flow.md` の portable な抽出である (Redmine #12353 j#62946 で特定され #12355 で出荷された配布ギャップ)。spine が深く読むべき first-read であり続け、operator 固有の lane 数はこの配布本文には置かない。

### pipeline-first が default、直列化は記録付き例外

sublane の帯域は coordinator の attention であり、CPU capacity ではない。durable record 上 ready な実装作業が存在し、下記の受け入れ条件が成り立つとき、それを dispatch するのが*優先される*行動である。すべての unit を 1 つの lane で直列化するのは multi-lane model の浪費であり、throughput の smell であって安全な default ではない。既に `implementing` の lane は肯定的な pipeline 占有である — coordinator が遊ぶ理由には**ならない**。逆に、並列化が総 latency や risk を上げる場合には coordinator は意図的に直列化する: 未決定の設計判断、file / invariant / merge 順序の重複、coordinator にしかできない drain、release / credential / 破壊的操作の gate、あるいは別の lane を覆い隠すような callback backlog である。

### 最小の coordinator-blocking state 語彙

fill するか stop するかを判断する前に、すべての lane を durable record から分類する (pane layout からではない)。portable な区別は、どの state が coordinator を block するかである:

- **coordinator-blocking** — `callback_due`、`review_waiting`、`owner_waiting`、`integration_waiting`、`close_waiting`、および `blocked` (`callback_delivery_failed` を含む)。任意の新規作業を開く前にこれらを drain する。close-ready なのに open のままの issue や、unmerged な local commit しか持たない closed issue は durable state の不整合であり、無害な帳簿処理ではない。
- **non-blocking** — `implementing` は lane capacity には計上されるが、それ単体で coordinator を直列化することは**ない**。`retire_ready` と `idle` の lane は drain または再利用の対象であり、active な作業として扱わない。

これは drain の各節が既に含意している最小の語彙である (完全な 9 分類の taxonomy と lane 帯域 profile は spine 側にある)。列挙は durable record から行い、`## Owner 承認の集約` が要求する通り pane 数に依存しない。

### Drain 順序

複数の lane が対応を要し、より強い durable な依存関係が順序を組み替えない場合、この順序で drain し、その後 dispatch し、loop を再実行する:

1. production / release / credential / 破壊的操作の blocker。
2. coordinator にしか集約できない `owner_waiting` (`## Owner 承認の集約`)。
3. `review_waiting`。
4. `integration_waiting` — commit は存在するが merge / push / patch 等価性 / 明示的 deferral が未記録。
5. `close_waiting` — durable な close gate は満たしているが issue がまだ open。
6. `blocked` または `callback_due`。callback 配送失敗を含む (`## Stall / no-progress 検出標準`)。
7. cockpit / worktree の attention を消費している `retire_ready` な lane (`## Sublane 退役 drain`)。
8. 新規 dispatch。

この順序は coordinator の帯域のためだけのものである。Redmine gate、review 品質、Review Gate と owner close approval の間の Close Approval Separation を一切変えない。

### dispatch / drain のたびに loop を再実行する

fill check は dispatch 前の一回きりの受け入れ判定ではない。coordinator は次の各時点で active な lane 集合を再分類し、capacity まで fill するか、なぜ停止したかを記録する: sublane dispatch が成功した直後、callback / review / owner / integration / close / 退役を drain した直後、owner 向け next action を提示する前、そして「次のタスク」を判断する前である。dispatch が 1 件成功しただけでは coordinator の stop 条件には**ならず**、「1 つの lane が既に implementing である」ことも stop 理由には**ならない**。

coordinator-blocking な state が解消され、active なのが `implementing` の lane だけで、独立した ready な作業が残り、lane capacity にも余りがあるとき、coordinator は次の sublane を target lane の Codex gateway 経由で dispatch する (`## Sublane の coordinator callback` の routing)。ready な作業があるのに dispatch **しない**場合は、durable な fill decision をちょうど 1 件記録する — 黙った待機、pane の雰囲気、「1 つはもう走っている」で済ませることは決してない:

- `dispatch_next` — capacity と独立した ready な作業が残っている。次の sublane を dispatch する。
- `stop_no_ready_work` — durable record 上 ready な実装作業が存在しない。
- `stop_overlap` — file / invariant / merge 順序の衝突により直列化の方が安全。具体的な依存関係を記録する。
- `stop_coordinator_blocking` — coordinator-blocking な state を先に drain しなければならない。
- `stop_soft_profile_full` — operator の local な lane capacity に達している。
- `stop_owner_or_release_gate` — owner 判断、release、credential、破壊的操作のいずれかの gate が active である。

### 本標準が緩めない境界

- **pipeline を fill することは gate の bypass ではない。** dispatch 後の fill は、active な owner / release / credential / 破壊的操作の gate を越えて作業を開くことは決してなく、直列化を要する重複を無効化することも、新規 dispatch より coordinator-blocking state を優先する Drain 順序を緩めることも決してない。
- **durable record が正本であり続ける。** lane 分類、fill decision、stop 理由はすべて Redmine journal から導出され Redmine journal に記録されるのであって、pane scrollback / `status` / `doctor` からではない。帯域や stop の記録が挙げるのは issue id と state class のみである。
- **operator 固有の方針を OSS 既定に持ち込まない。** 具体的な lane 数の soft profile (target / burst / stop の数値)、private な cockpit 構成、operator の path / branch 命名は、spine の local profile と operator 自身の runbook に置く operator runtime policy であり (採用 repo の public / private 境界規約を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)、この配布本文には決して置かない。portable な部分は、*coordinator がすべての dispatch とすべての drain の後に fill loop を再実行し、最小の coordinator-blocking 語彙で lane を分類し、Drain 順序で drain し、ready な作業を dispatch しないときは必ず durable な fill decision を 1 件記録すること* であり、具体的な数値は operator のものである。

## Integration disposition と push authority

push は二層の権限であり、層ごとに所有者が異なる (Redmine #13026。#13024 / #13025 の main-unit session で、実装者が preset の「記録前に push する」を `origin/main` を直接 push してよい許可と読み、また shared checkout での branch 切り替えによって実装者 commit が誤った branch に載った事例に由来する):

- **実装者は issue / lane branch を push し、それのみを push する。** `implementation_done` / `review_request` の origin 到達可能性前提条件 (`### gate へ commit hash を記録する前の origin 到達性 preflight` を参照) は branch の到達可能性で満たされる — 実装者が `origin/main`、release branch、その他いかなる integration branch を進めることも、決して要求せず、許可もしない。誤って local の integration branch に載った実装者 commit は issue branch へ移し、correction として記録する。push はしない。
- **integration は coordinator が所有する。** review approval の後、coordinator が integration branch を進め、その判断を **integration disposition** として記録する — `merge` (fast-forward-only が default。非 ff の merge commit や rebase による統合はその理由を記録する)、`patch_equivalent` (変更が等価な形で target branch に到達した。等価性の確認方法を記録する)、`explicit_deferral` (統合を意図的に延期。何が解除条件かを記録する) のいずれかである。disposition journal には統合した commit、merge type、merge 後の検証を明記する。これが `## Dispatch 後の fill loop` の coordinator-blocking state `integration_waiting` の背後にある durable な定義である: commit を持つ unit は、その disposition journal が載るまで `integration_waiting` である。
- **main-unit 例外の実装でも branch は切る。** dispatch decision が main unit / primary checkout で実装する例外を記録した場合でも、実装者は issue branch を作成しその branch を push する。primary checkout の integration branch に作業を直接 commit することは決してない。coordinator と実装者が 1 つの checkout を共有すると branch 切り替えが衝突し得る — commit 前に現在の branch を確認し、専用 worktree を優先し、衝突が起きたら黙って修復せず correction として記録する。
- **権限 field は central preset に置いたままにする。** この規約の gate-level の記述 (実装者の push ref 制限、integration disposition の記録、禁止遷移 `implementer_advances_integration_branch`) は central preset の `### Commit Hash Origin 到達可能性` にある。本節は運用手順であり、preset の field 表を再掲しない。

## 既存 project の sublane 導入

上の sublane 各節は、project が既に governed scaffold を持っている場合に coordinator がこのフローをどう運用するかを記述している。本節は repo-local runbook `vibes/docs/logics/existing-project-sublane-adoption.md` (Redmine #12423) の portable な抽出である: すなわち、code・router・ticket lifecycle・固有 docs を既に持つ**既存** project を、**既存の routing を壊さずに** governed scaffold と sublane フローへ載せる方法である。これは `--with-sublane-flow` opt-in profile (`vibes/docs/profiles/sublane-flow-runtime-profile.md`) から到達できる採用者向け手順である。dogfood 固有の lane 数、cockpit 構成、絶対 path はこの配布本文には置かない。

導入は setup 経路であり、いかなる gate の緩和でもない。owner close approval、Review / Close の分離、release / credential / 破壊的操作の gate はすべて、本リファレンスの他の部分が定義する通りに維持される。導入固有の許容はただ 1 つ、**bootstrap 例外**である: child gateway や target-lane docs がまだ存在しない場合、coordinator はより薄い dispatch 前の decision record で進行し、欠けた判断を follow-up correction として記録してよい。bootstrap 例外は薄い decision record を修復するだけであり、承認 invariant を bypass することは決してなく、通常開発の近道にも決してならない。

### 既存 project 導入が適用される場面

本手順を使うのは、project が durable な ticket record (Redmine journal) を正本として扱う予定であり、agent が pane chat だけでは再現できない継続的な implement / review / close lifecycle を回し、project root に router が無い、または root router と subdir の関係が曖昧である場合である。一回限りの sandbox、短命な demo、初日以降 `catalog.yaml` を維持する catalog owner がいない project、private な運用方針を OSS 既定として hard-code しなければ成り立たない project には使わない。

### 導入編集前の read-only preflight

導入 target を変更する前に、coordinator は durable record に **read-only preflight** を記録する。そこに記録するのは: target の durable な work system と ticket project、`AGENTS.md` / `CLAUDE.md` が既に存在するか・手編集か scaffold 管理か、`.mozyo-bridge/scaffold.json`・repo-local または central の rules store・既存の `.mozyo-bridge/docs/catalog.yaml` の有無、root 導入が subdir の catalog や subproject router を黙って上書きしないための root と subdir の関係、そして現在の branch を添えた `git status --short` であり、無関係な dirty file は scope 外として名指しする。preflight には、導入する理由、触る path、保全する path、採用予定の preset、検証計画、既知の risk を明記する。**既存 routing を保全する**: root 導入は、既存の subdir と project-local docs を上書きするのではなく、root router から到達可能なまま維持しなければならない。

### 導入を実装 child と検証 child に分解する

導入を、親 UserStory (導入目標と close 条件)、実装 child (scaffold / rules / catalog / router の変更)、検証 child (dry-run、status、docs validation、handoff smoke) に分割する。commit を伴う実装と dry-run 検証を別 issue に保つことで、それぞれの review anchor が混ざらなくなる。親 US audit はその上で、child issue、commit、検証、callback outcome、integration disposition をまとめて読む。

### Dispatch decision と scaffold / rules / catalog 導入

実装の形をした導入作業は既定で sublane dispatch とする (`## Dispatch 後の fill loop` の routing)。coordinator が自分の lane で編集する場合は例外理由を記録する。dispatch decision には、target issue、target lane / branch の identity、`work_shape: implementation`、変更が見込まれる path、main-lane 作業を使う / 使わない理由、callback の期待を記録する。実装 lane は `mozyo-bridge rules install`、`scaffold status --target .`、`scaffold apply <preset> --target . --backup`、その後再度 `scaffold status --target .` を実行する (repo-local store の場合は `--repo-local` variant を使う。central と repo-local を決して混在させない)。すべての `scaffold apply` diff を読み、root router が preset を指す thin router であること、出荷 artifact が `.mozyo-bridge/scaffold.json` で追跡されていること、既存の subdir router、業務 docs、private overlay、generator 出力が未編集のまま残っていることを確認する。`.mozyo-bridge/docs/catalog.yaml` は scaffold が決して上書きしない target 所有の data である。`rules install` の hash のみの drift は修正して commit と journal に記録し、router / artifact 本文の drift とは区別して扱う。初回導入時には、出荷された `catalog.yaml.example` を target の `catalog.yaml` へ適合させ、実在する coverage root と、root router・governed artifact・project docs・実装 path 向けの file conventions を持たせる — 既存の subdir catalog を複製して第二の正本を作ることは決してなく、private な path、credential、operator 固有の cockpit 構成を記録することも決してない。

### 検証・origin 到達 commit・callback recovery・close 順序

最小検証は `scaffold status --target .`、`doctor --target .`、`docs validate --repo .`、`docs validate --check-file-coverage --repo .`、`docs generate-file-conventions --check --repo .`、`git diff --check` であり、必要に応じて `docs resolve`、`docs audit-impact --all-changed --check-generated --repo .`、target 固有の test、同一レーン handoff / coordinator callback の smoke で拡張する。検証 child は、commit を伴う実装とは独立に、target の clean status、docs validation、generated の同期、dry-run smoke を記録する。review や close の anchor に使う commit は origin 到達可能でなければならない。local のみの commit は実装上の observation としては読めるが、close や audit の anchor には決してならない (`### gate へ commit hash を記録する前の origin 到達性 preflight`)。実装 branch が無関係な local history を含む場合は、target branch から clean な integration branch を作り、導入 scope の commit のみを cherry-pick し、元 commit と integration commit の対応を記録する。sublane はすべての handoff-worthy state で coordinator callback を送る (`## Sublane の coordinator callback`)。callback が欠落または誤宛先の場合、coordinator は Redmine journal の sweep から recovery してよく、どの state が durable record 上で進んでいたか、callback がどこへ届いた / 届かなかったか、どの journal から recovery したか、route の修正内容を記録する — journal が正本、callback は pointer であり、nagger は二次 signal であって決して primary な制御ではない。close は次の順序で行う: child の実装 / 検証 issue を audit し、親 US の Review Gate を記録し、owner close approval を別 journal として収集し、child を close し、その後親 US の Close Gate を記録する。

### 既存 project 導入が緩めない境界

- **導入は承認 gate を決して緩めない。** clean な `scaffold status` は workflow 導入ではない — project が実際に Start / handoff / callback / review / close を durable record 上で回さなければならない。bootstrap 例外は薄い decision record を埋めるだけであり、owner close approval、Review / Close の分離、release / credential / 破壊的操作の gate は変わらない。
- **durable record が正本であり続ける。** 導入の判断、preflight、dispatch、callback recovery、integration disposition は ticket journal に記録するのであって、pane scrollback / `status` / `doctor` から推測しない。`catalog.yaml.example` は出荷 skeleton であり、`catalog.yaml` が target 所有の正本で、generated な file conventions は決して手編集しない。
- **operator 固有の方針を OSS 既定に持ち込まない。** private な絶対 path、operator の cockpit 構成、並列 lane 数、session / window の命名は、operator 自身の private な運用 profile に置いたままにし (採用 repo の public / private 境界規約を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)、この配布本文にも出荷 scaffold 既定にも決して置かない。portable な部分は*導入の一連の手順 — read-only preflight、child への分解、既存 routing を保全する dispatch decision、scaffold / rules / catalog の導入、検証、origin 到達可能な commit、callback recovery、close の順序* — であり、具体的な数値と path は operator のものである。

## Main-unit Claude の安全使用境界

`## Sublane の coordinator callback` と `## Coordinator stop と next-action 標準` は、main coordinator lane が Codex pane であることを前提としている。cockpit layout によっては、coordinator Codex の隣に auto mode で idle した **Claude pane を main coordinator unit 自体に**配置することもある。coordinator Codex の context を節約するために、その pane を coordinator 業務に使いたくなる。本節はその利用に境界を引き、main-unit Claude が coordinator の owner 窓口 / gate 判断の役割を曖昧にせずに context を節約できるようにする (Redmine #11858、#11850 multi-lane PoC に由来)。

この境界は**観測された workflow 上の risk から引かれたものであり、特定 model の能力についての固定的な判断ではない**。Claude も Codex も時間とともに変化する。tooling が変わったら本節を見直す。守っているのは構造である: main unit は owner 窓口 / audit / routing の正本に最も近い pane であるため、そこにいる actor が黙って gate 判断や owner 判断を下すと、multi-lane model 全体が依存する分離が崩壊する。

### main-unit Claude は assistant であり並列 coordinator ではない

main-unit Claude pane は coordinator にとっての非権威的な helper である。その出力は **draft / input であって決して evidence ではない**: coordinator Codex は、それを何らかの判断に変える前に、source file、Redmine record、command 出力と突き合わせて確認しなければならない。main-unit Claude の結果は pane scrollback と同じ扱いにする — 確認すべき pointer であり、durable な事実ではない。

### 許可される用途 (安全な Codex context 節約)

以下は、coordinator の context budget ではなく main-unit Claude の context budget を使わせるために coordinator が渡してよい具体的なタスクである。いずれも権威的な判断を生まないからである:

- 長い Redmine journal、diff、log、command transcript を、coordinator がその後検証する短い brief に要約する。
- candidate の抽出 — 例えば stall candidate、変更 path、影響 issue の first-pass な list — を行い、coordinator が durable record と突き合わせて確認する。
- durable な編集を残さない scratch 分析と read-only 調査。
- 文面の draft 作成 (journal 本文、next-action menu の提案、doc の段落)。載る前に coordinator が review して所有する。
- coordinator が比較検討するための、非権威的な選択肢比較。
- 作業が適切に Redmine で gate された lane (専用の sublane または worktree) へ移された**後**の通常実装。標準の implement → record → review フローに従う。これはもはや「main-unit assistant」としての利用ではなく、境界づけられた lane での通常の実装者利用である。

### 禁止される用途 (coordinator Codex に残すもの)

main-unit Claude は owner 窓口や gate 判断の行動を一切行ってはならない。Claude pane への依頼がどのような言い回しであっても、以下は coordinator Codex に残る:

- owner への質問、または owner close approval の要請 / 収集 / 承認確定 (`## Owner 承認の集約` を参照 — owner 承認は単一の coordinator Codex に収束するのであって、決して Claude pane にではない)。
- Review Gate / US-level audit の結論を出すこと、または review verdict を記録すること。
- durable な routing 判断を下すこと — 依頼をどの lane に送るかの選択、lane 境界をまたぐ handoff の dispatch、sublane が完了したという判断。
- Redmine gate を満たしたと解釈すること、gate を進めること、issue を close すること。
- 保護された workflow / skill / source / test 面 (`## Policy / skill authoring 境界` に列挙された面) への黙った編集、または明示的に gate された lane の外でのあらゆる編集。

main-unit Claude pane に打ち込まれた依頼はこれを変えない。`## Claude / Codex 役割境界` と同様、命令形の言い回し ("やって", "go ahead") は意図を表すのであって、境界を越える権限を与えるものではない。

### sublane Claude との違い

sublane Claude と main-unit Claude はどちらも owner 窓口ではないが、権限が異なる:

- **sublane Claude** は境界づけられた実装 worker である。自分の lane の中で実際の diff を生み、implementation_done / review_request journal を記録し、検証を実行する — durable な実装 output であり、ただし owner 窓口向けではないだけである。その lane は固有の Redmine gating と固有の Codex gateway を持つ。
- **main-unit Claude** は固有の実装 lane を持たない。coordinator の隣に座っているため、安全範囲はより狭い: 作業が明示的に gate された lane へ移されるまでは assistant 専用 (要約、抽出、draft、scratch) である。その場で実装させると、未 review の編集が audit / owner 窓口の正本のすぐ隣に置かれることになり、それこそが本節が防ごうとしている risk である。

要するに: sublane Claude は自分の lane の gate の下で実装し、main-unit Claude は coordinator を補助し、作業が main unit を離れて gate された lane に移って初めて実装する。

### 本節が緩めない境界

- **assistant の出力は input であり、evidence ではない。** main-unit Claude が生み出すものは、coordinator Codex が正本と突き合わせて確認し記録するまで durable にはならない。
- **owner 窓口と gate 判断は coordinator Codex に残る。** 本節は coordinator の context を節約するものであって、`Close Approval Separation`、`## Owner 承認の集約`、`## Policy / skill authoring 境界` の各境界を動かすものではない。
- **operator 固有の方針を OSS 既定に持ち込まない。** ある operator が main unit に Claude pane をそもそも置くかどうか、日常的にどの具体タスクをそこへ offload するか、private な優先順位付けは operator の runtime policy である (採用 repo の public / private 境界規約を参照。`mozyo_bridge` では `vibes/docs/rules/public-private-boundary.md`)。portable な部分は、*main-unit Claude は出力が input-not-evidence の assistant であり、列挙された安全なタスクは引き受けてよく、owner 窓口や gate 判断を引き受けたり gate された lane の外で実装したりしてはならないこと*である。operator の具体的な offload list は operator 自身の runbook に置くものであり、配布される skill や preset の本文には置かない。

## Durable record の根拠出所 (Evidence Provenance)

gate journal に書かれるすべての load-bearing な主張 — review finding、dispatch 指示の背後にある根拠、design consultation への入力 — は、その確からしさとは別に、**誰の権威に立脚するか**を宣言する (fact / hypothesis 分類は確度の軸であり、provenance は権威の軸で、両者は直交する)。provenance class は 4 つに固定される: **owner intent** (durable anchor — journal id または issue description の発話 digest — がある場合のみ有効)、**documented rule** (path と section)、**agent 自身の judgment** (争い得る主張であって、格下げされた主張ではない)、**hearsay** (未記録の owner 発話。そうであると label しなければならない)。

効力を持つ規約は label ではなく重みである: **未記録の owner 発話は、それ単体では block、要修正 finding、gate 遷移を決して正当化しない。** 使うには先に記録する — ユーザー窓口 (既定の役割分担では Codex) が owner に確認し、その発話を durable record に載せる。その後は owner intent として数えられる。anchor の無い owner-intent 主張は、どう label されていようと hearsay として扱う。これは、pane で観測した owner の生の回答は close approval にとって未確認の input であるという既存規約の一般化である: すべての gate とすべての agent に、双方向で適用される — reviewer は実装者が検証できない伝聞に finding の根拠を置けず、実装者は自分が記録しなかった pane 上の発言に判断の根拠を置けない。規範的な field 要件は central preset の `### 根拠出所分類 (Evidence Provenance)` にあり、本節はそれを再掲しない。

## Claude / Codex 役割境界

- 通常開発タスクの実装は Claude が所有する。
- Codex は `mozyo_bridge` の通常開発タスクを直接実装しない。
- escalation 対応、audit、ユーザー向け確認、正本から下せる判断は Codex が所有する。
- Codex が通常開発タスクの ID を受け取ったとき、標準の行動はそれを Claude handoff へ変換することであって、自ら実装することではない。タスクの規模、緊急性、実装難易度、ユーザーの苛立ち、ユーザーが Codex pane に直接書き込んだことは、この default を上書きしない。
- ユーザーからの命令・依頼の言い回し — 例えば "実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "go ahead", "please do it" — は、それだけでは Codex が direct edit を行う authorization にはならない。それらは「これをやってほしい」という意思の表明であって、「Claude を bypass してよい」ではない。
- 標準の handoff が上書きされるのは、Policy / skill authoring 境界 の節で定義された明示的な Codex direct edit 例外による場合のみである。
- Codex が workflow 変更の検証タスクを受け取ったときは、Codex は有効な通常開発タスクを選定し、その選定を active な ticket system (`mozyo_bridge` では Redmine journal、Asana preset の repo では Asana comment) に記録し、Claude へ handoff する。
- 検証タスクとして数えられるのは、Claude が通常開発作業を実施し、Codex が audit 経路を実施した場合のみである。
- Codex が誤って通常開発タスクを直接実装した場合、その実行はタスクの正常な completion として数えない。それが検証タスク中に起きた場合は、workflow 変更の検証も満たさない。
- そのようなミスの後は、影響を受けた ticket を reopen し、ミス、影響範囲、follow-up の判断 (採用、破棄、再実装) を active な ticket system に correction として記録し (Redmine の correction journal または Asana の correction comment)、その後 Claude 実装から Codex audit までのフローを再実行する。この correction フローは検証対象タスクに限らず、すべての通常開発タスクに適用される。

## Policy / skill authoring 境界

- autonomous workflow、rules、skills、handoff、audit、release/distribution gate の変更については、方針の枠組み設定、draft 文面、ユーザー向け確認、audit を Codex が所有する。
- それらの policy や skill reference への repository file 編集の default 実装者は Claude である。
- Codex は通常運用中に policy や skill reference の file を直接編集して commit してはならない。保護 scope は、実装 file (`src/**`, `tests/**`, `docs/**`, `vibes/docs/**`, `README.md`, release workflow, CLI 挙動) と、guardrail / docs / catalog 面 (`AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, `.mozyo-bridge/docs/catalog.yaml`, `.codex/skills/**`, `.claude/skills/**`, `skills/mozyo-bridge-agent/**`, `plugins/mozyo-bridge-agent/**`, `src/mozyo_bridge/scaffold/presets/**` 配下の scaffold packaged preset / router template) の両方を覆う。chat レベルの "ユーザーがガードレール変更を明示" は、それだけではこれらの面で Codex が Claude を bypass する authorization にはならない。
- Codex の direct edit が許可されるのは、以下の狭い例外のいずれかに該当する場合のみである。例外は保守的に運用する。疑わしい場合、またはユーザー指示が複数の読み方を許す場合は、default に戻り Claude handoff を作成する。
  1. ユーザーが、特定のタスクや file に scope を限定して、`Codex direct edit`、"Codex が直接編集してよい"、"Codex に直接実装させてよい" と同等の文言で Codex の direct edit を明示的に authorize した場合。一般的な命令・依頼形 ("実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "please do it") は該当しない。
  2. 変更が、既に起きた誤実装・誤 commit・誤手順を active な ticket system (Asana task / Redmine issue) または repo に記録するために必要な、最小限の record-keeping correction である場合。
  3. 変更が、handoff によって損なわれる真に緊急の小規模 fix である場合 (例えば、進行中の release・publish・CI 実行を止めるために数分以内に必要な 1〜数行の fix)。この例外を発動する前に、Codex は実装を停止し、状況、対象 file、意図する変更、影響範囲を添えた「urgent direct-edit request」を active な ticket system (`mozyo_bridge` では Redmine journal、Asana preset の repo では Asana comment) に記録し、可能なときはユーザー確認を得なければならない。状況が曖昧な場合や確認が得られない場合は、この例外を適用しない。
- Codex が例外の下で direct edit を行う場合、durable record は **system 固有**であり、編集が載る前に存在しなければならない:
  - Asana project: task 上の Asana comment に `Codex direct edit` を記録し、(a) どの例外が該当したか、(b) ユーザー指示の逐語または引用、(c) 変更した file、(d) 実施した検証、(e) follow-up 検証が必要かどうかを添える。
  - Redmine project (`redmine-governed` preset を使う `mozyo_bridge` repo を含む): Codex の編集より前に、active な issue に Redmine の `codex_direct_edit` gate journal を作成する。必須 field — `role: 実装者`, `direct_edit: true`, `allowed_paths`, `reason`, `follow_up_review` — は central preset の Gate Schema (`codex_direct_edit`) が定義しており、そちらが正本である。本リファレンスはその意味論を再掲しない。gate journal の無い direct edit は、それ自体が correction の対象となる違反である。
- `.mozyo-bridge/docs/file_conventions.generated.yaml` などの catalog generator 出力は generator 専用の artifact である。Claude も Codex も手編集しない。`.mozyo-bridge/docs/catalog.yaml` を変更して `mozyo-bridge docs generate-file-conventions` で再生成し、`--check` で検証する。
- これらの field のいずれかを欠く direct edit は、それ自体が follow-up correction の対象となる。過去の incident pattern: 事前の `codex_direct_edit` gate journal 無しに、あるいは Review Gate で承認された audit-owned commit 経路無しに Codex が作成した repo diff は、correction journal で記録し、governed な実装/review フローへ差し戻さなければならない。
- autonomous workflow や役割境界への Codex direct edit は、workflow 変更の検証要件を免除しない。

### Repo-Local Guardrail Autonomous Lane (mozyo-bridge product 全体の方針)

`redmine-governed` と `redmine-rails-governed` の preset は、`codex_direct_edit` gate から repo-local path の狭い集合を切り出す **Repo-Local Guardrail Autonomous Lane** を配布する: 既定では `vibes/docs/rules/**`、`vibes/docs/logics/**`、`vibes/docs/specs/**`、`.mozyo-bridge/docs/catalog.yaml` である。この lane の内側では、Codex は編集前の gate journal 無しに直接編集し、代わりに `codex_autonomous_edit` journal (`lane: autonomous`) を記録する。この lane は preset によって有効化されるのであって、chat の言い回しによってではない。project-local な追加はこれを拡張・制限してよいが、配布物 / runtime / 実装の面に広げることは決してしない。

journal field、commit 前検証 command、lane に入れない条件、journal 欠落時の correction フローについては central preset の `### Repo-Local Guardrail Autonomous Lane` が正本であり、本リファレンスはそれらを再掲しない。agent の反応の仕方を左右するため、skill レベルの reminder が 2 つだけここに残る: `codex_autonomous_edit` journal を欠いた lane commit は record-keeping correction である (それだけを理由に変更を revert しない) こと、そして lane 定義自体へのいかなる変更も、標準の `Workflow 変更の反映確認 (Workflow Change Verification)` フローを起動する workflow / guardrail 変更であることである。

## Audit-Owned Commit Authority (audit 所有 commit 権限)

`mozyo_bridge` の既定の役割分担では Claude が実装し、Codex が audit する。durable な audit record が確保された後 (Asana task 上の audit / review comment、または Redmine issue 上の Review Gate journal)、Codex は *audit で承認された diff のみ* を stage して commit することを authorize される。これは commit 権限であって、実装権限ではない。両者は別個の境界である:

- **Codex による直接実装編集** — `Policy / skill authoring 境界` の狭い例外に制限される。新しい diff を生み出すこと。
- **Codex による audit-owned commit** — audit record が存在した後に許可される。Claude が既に生み出し、audit record が承認した diff を commit すること。

audit-owned commit は実装者 / 監査者の境界を免除しない。Codex は、staging 中に audit 承認済み diff を「手直し」するために実装 file を編集してはならない。diff に変更が必要なら、それは Claude に差し戻す新たな実装 iteration である。

audit-owned commit の前に、Codex は次を行わなければならない:

1. durable な audit record — Asana task 上の audit / review comment、または Redmine issue 上の Review Gate journal — が存在することを確認する。この記録が存在する前に commit を載せることはできない。
2. `git status` を実行し、dirty な集合を実装 actor が記録した変更 path list と突き合わせる。scope 外の dirty file がある場合は、stash する、別 scope のタスクへ回す、または触らずに残す — audit-owned commit に同梱することは決してしない。
3. audit で承認された path のみを stage する。worktree に承認済み diff 以外のものがある場合は常に `git add -A` と `git add .` を避ける。
4. `git diff --cached --stat` を実行し、内容 review が必要な場合は `git diff --cached` も実行する。staged な集合を実装 comment と一行ずつ突き合わせる。
5. project の central preset が定義する system ごとの ticket 参照を含む message で commit する:
   - Asana project: `Refs: Asana task <task_id>` に加えて `Audit: Asana comment <comment_id>` (承認の durable な comment / story id)。
   - Redmine project: `Refs: Redmine #<issue_id>` に加えて `Journal: <journal_id>` (Review Gate journal の id)。
6. commit hash を durable な正本に記録する: 同じ task への follow-up Asana comment、または Redmine issue 上の Close Gate / Progress Log journal。hash は durable record に置かなければならず、pane chat のみに置いてはならない。
7. task を complete にする、または issue を closed へ進めるのは、audit record と commit hash 記録の両方が揃った後に限る。implementation done 単独や、hash 記録の無いまま載った commit は completion ではない。

central preset が review approval と owner close approval を区別する system (Redmine project: Redmine preset の `Close Approval Separation` を参照) では、close の前に、review approval と owner close approval の両方を別個の durable journal として記録しなければならない。review approval 単独は close approval ではない。実装者は close に進む前に owner close approval journal を待たなければならない。

この権限は、project が実装 actor と audit actor を分けている限り、通常開発タスクにも guardrail / rule / workflow タスクにも等しく適用される。autonomous workflow、skills、rules、release / distribution gate への変更に対する `Workflow 変更の反映確認 (Workflow Change Verification)` の要件を免除するものではない — rule 変更の検証は依然として、標準の handoff を伴う別個の通常開発タスクである。

## Workflow 変更の反映確認 (Workflow Change Verification)

- autonomous workflow、skills、rules、handoff、escalation、release/distribution gate を変更した後は、新しい session でその変更を検証する。
- その検証には通常の `mozyo_bridge` 開発タスクを使う。
- 検証対象の workflow / rule / skill 領域を直接変更するタスクは使わない。
- 通常開発タスクは Claude が実装する。タスク選定、handoff、audit は Codex が担い、Codex は検証対象を直接実装してはならない。
- 検証対象をタスクの規模や production への影響で選ばない。基準は、そのタスクが検証中の workflow、skill、gate を直接変更するかどうかである。
- 検証結果を active な ticket system (`mozyo_bridge` では Redmine journal、Asana preset の repo では Asana comment) に記録し、ギャップがあればそこに follow-up ticket を作成する。
