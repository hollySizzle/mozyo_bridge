# Redmine Agent Workflow

## 正本

- Redmine issue を実行単位とし、Redmine journal を作業状態の正本とする。
- agent 間 handoff、設計相談、実装完了、レビュー依頼、レビュー結果、close 判断は、同一 issue の journal として追跡できなければならない。
- pane message と chat message は通知であり、Redmine gate の代替ではない。pane 通知は journal id を運ぶ pointer にすぎず、対応する journal がなければ監査可能な作業記録ではない。
- status / tracker 名は Redmine project ごとに異なる。project 固有 status は下記 gate lifecycle に対応付ける。並行する独自 lifecycle を作らない。

## 事実姿勢

- 迎合より事実を優先する。ユーザーの前提、他 agent の報告、自分の過去発言と調査結果が矛盾した場合は、根拠とともに明示する。
- 意見の不一致、採用しなかった選択肢、判断理由は chat ではなく該当 Redmine gate に残す。
- `Implementation Done` は `完了` ではない。Review Request、Review、owner approval、Close Gate が Redmine に残るまで、chat でも issue status でも完了扱いしない。
- 未確認事項は断定しない。`未確認` と書き、何を確認すれば解消するかを gate に残す。

## 作業開始

1. current project root と active Redmine issue を確認する。
2. parent issue (Epic / Feature / UserStory) を確認し、目的、受入条件、前提、既知の制約を把握する。
3. 現在の handoff / review / design consultation を境界付ける journal id を確認する。存在しない場合は、pane 通知より先に適切な gate journal を作る。
4. project-local docs を必要最小限読む。project が docs catalog、active-doc resolver、file convention generator を提供している場合は、それで対象 path に紐づく active docs を解決し、resolver のタイトルだけで済ませず本文を読む。
5. issue、parent issue、journal が存在しない、曖昧、またはアクセス不能な場合は作業を始めない。正しい gate を確認する。

## Ticket-ID Entrypoint

入力が Redmine issue id、Redmine URL、または pane / chat 上の issue 名だけの場合でも、この entrypoint を適用する。pane text が十分に具体的に見えても、作業指示の正本は Redmine issue と journal である。

作業前に必ず行うこと:

1. Redmine API または project 標準 tool で issue と最新 journals を取得する。
2. parent issue (Epic / Feature / UserStory) を確認し、目的、受入条件、前提、既知の制約を把握する。
3. 現在の handoff を境界付ける journal id を特定する。典型例は Review Request、Design Consultation、Implementation Done の journal。指定 journal が存在しない場合は、prompt 本文に従う前に適切な gate を作るか、作成を依頼する。
4. project の status / tracker を標準 gate lifecycle (Start / Progress Log / Design Consultation / Design Consultation Answer / Implementation Done / Review Request / Review / QA Verification / Production Verification / Close) に対応付ける。
5. 必須 framing が欠落、曖昧、または parent issue と矛盾する場合は、実装を始めない。まず Progress Log Gate に gap を記録する。

pane / chat 由来の説明は、Redmine issue record に照合してから扱う。Redmine の journal gate semantics を Asana の単一 comment thread 的な形に潰さない。Redmine では journal id と gate 順序が監査 replay の鍵である。

## Redmine Gate Lifecycle

通常開発 task の標準 lifecycle。各 gate は durable な Redmine journal として残す。pane notification は gate の journal id を運ぶだけである。

1. **Start Gate** — 目的、parent issue、受入条件、参照 docs、既知の不明点、現在の担当 agent、次 action を記録する。project workflow に `着手中` 相当の status がある場合は更新する。ない場合は status を無理に合わせず、Start Gate journal を正本にする。
2. **Progress Log Gate** — 重要な進捗、判断、scope 変更、blocker、owner 判断待ち、次の具体 action を記録する。最後だけでなく、前提や scope が動いた時点で使う。
3. **Design Consultation Gate** — 後戻りしにくい判断の前に作る。例: 仕様解釈、責務境界、永続化形状、自動補正、UI flow、authorization、DB integrity、既存 URL / data compatibility。背景、正本参照、選択肢、pro/con、実装者の推奨、判断が必要な質問、残る不明点を分けて記録する。
4. **Design Consultation Answer Gate** — 相談を受けた audit / design role が記録する。選択した案、理由、却下した案と却下理由、残る不明点、owner 判断が必要な事項を分ける。
5. **Implementation Done Gate** — changed paths、実装意図、前提、未確認事項、verification、doc 更新、commit hash または diff ref を記録する。これは review input であり completion ではない。未完 scope はここに混ぜず Progress Log Gate または child issue に出す。
6. **Review Request Gate** — reviewer に渡す前に作る。必須 payload は、issue id、journal id、target commit / diff、changed files、verification、未確認事項、受信 agent、期待する read / ack path、review focus、対象外 scope。pane 通知はこの gate が存在してから送る。
7. **Review Gate** — reviewer が記録する。必須 payload は、target commit、照合した rules / docs、総合判定、severity 順の findings、file / line 根拠、`[事実]` と `[仮説]` の区別、是正条件、再 review 要否、未確認事項。`[事実]` はコード・設定・docs で確認済みの不整合に限る。`[仮説]` は確認すべき事項と確認方法を併記する。
8. **QA Verification Gate** — project が manual check、tester check、受入確認、または本番前確認を要求する場合に記録する。確認対象、環境、手順、期待値、実際値、evidence、tester / verifier、未確認事項を分ける。bug を見つけた場合は bug / spec misunderstanding / unnecessary work を triage し、source change が必要なら通常開発 issue または child issue に切り出し、元 issue と link し、理由を journal に残す。
9. **Production Verification Gate** — deploy 後の本番確認が必要な project で記録する。deploy request、deploy 実施者、version / revision、確認対象、本番での期待値と実際値、rollback / follow-up 要否を分ける。deploy request と production verification を implementation done や review に混ぜない。
10. **Close Gate** — acceptance result、Review Gate findings の disposition、QA / Production Verification Gate の disposition、remaining risks、owner approval、audit result、commit hash record、Epic / Feature close basis、retired queue stale check が必要な場合の結果、最終 close 判断を記録する。passing Review Gate と owner approval と commit hash record が揃うまで issue を close しない。

project 固有 status / tracker はこの gate に map する。別の名前を使ってもよいが、gate の意味と順序を曖昧にしない。

## Review Quality Hierarchy

review は style 指摘の収集ではない。重大度は下記の順に見る。

1. **仕様・設計整合** — parent issue、受入条件、Design Consultation Answer、既存仕様、data compatibility、authorization、operation flow と矛盾しないか。
2. **docs / rule 整合** — project-local docs、Redmine gate、migration / release / verification rules、生成物管理規約に反していないか。
3. **behavior / data risk** — runtime error、data loss、権限漏れ、既存 URL / API / data 互換性、rollback 困難性、migration safety。
4. **test / verification gap** — 変更に対応する automated test、manual verification、screenshot / capture、本番確認が不足していないか。
5. **保守性・style** — 上記を満たした後に扱う。好みだけの指摘は finding にしない。

設計 docs が不足していて review 判定できない、scope が曖昧、または parent / child issue と実装が矛盾する場合は、推測で approve しない。Review Gate に `[仮説]` と確認方法を残し、必要なら Design Consultation Gate または Progress Log Gate に戻す。

## Test / QA Role Boundary

project が tester、QA、manual verifier を分ける場合、tester は production code を直接修正しない。tester の責務は仕様から確認することであり、実装都合から期待値を作らない。

- tester は issue、parent、受入条件、Design Consultation Answer、Review Gate の既知 risk を読んで確認観点を作る。
- failure report は reproduction、expected、actual、environment、evidence、影響範囲、bug / spec misunderstanding / unnecessary work の仮判定を分ける。
- source change が必要な failure は、既存 issue に黙って混ぜず、project workflow に従って bug issue、child issue、または Progress Log Gate に切り出す。
- tester が修正案を書くことはできるが、それは implementation authority ではない。修正は実装 owner または project が定める actor が行う。

## Close Gate Checklist

Close Gate では少なくとも以下を照合する。項目を満たせない場合は close ではなく Progress Log である。

- problem / background / acceptance criteria が issue または parent issue から追える。
- owner または design role の判断が必要だった事項について、選択肢、採用案、却下案、理由が journal に残っている。
- Implementation Done、Review Request、Review、必要な QA Verification、必要な Production Verification が同一 issue または linked issue から replay できる。
- Review Gate findings は fixed / accepted risk / out of scope / child issue のいずれかに disposition されている。
- commit hash、target revision、deploy version など、後から差分を特定する anchor が durable record に残っている。
- child issue、manual verification、production verification、version consistency、retired queue residue の未完が残っていない。残る場合は close せず、理由と次 action を Progress Log Gate に残す。

## Pane Notification

- 標準通知は高レベル handoff primitive を使う: `mozyo-bridge handoff send --to <claude|codex> --source redmine --issue <issue_id> --journal <journal_id> --kind <implementation_request|design_consultation|review_request|review_result|implementation_done|reply|custom>`。reply では `mozyo-bridge handoff reply ...` または上位 alias `mozyo-bridge reply ...` を使う。primitive は receiver pane 解決、Layer B deterministic preflight、marker 付き typing、Enter 発行 (queue-enter / standard rail) を所有する。caller は通常 handoff / reply で `read` + `message` shell choreography を手で組み立てない。
- `notify-*` wrappers (`notify-codex`, `notify-claude`, `notify-codex-review`, `notify-claude-review-result`) は Redmine 互換 entrypoint として残す。内部では同じ primitive に乗る。新規 caller は、durable record に明示的な `--kind` を残せる `mozyo-bridge handoff send/reply` を優先する。
- `notify-*-legacy-task` は retired-queue cleanup wrapper であり、新規通知に使わない。
- 低レベルの `mozyo-bridge read`、`mozyo-bridge message`、`mozyo-bridge type`、`mozyo-bridge keys` は operator/debug primitives である。pane inspect、ad-hoc operator message、raw typing、raw keys 用であり、standard handoff/reply の代替ではない。
- Redmine journal を作る前に pane 通知しない。journal より先の通知は順序依存になり、監査 replay を壊す。
- pane 通知成功は review record ではない。pane 通知失敗も review failure ではない。唯一の判断 record は Redmine gate である。
- 受信者は、通知本文だけで動かず、指定された Redmine issue / journal を読む。durable Redmine anchor が利用可能な場合、`mozyo-bridge status`、`mozyo-bridge doctor`、pane scrollback から receiver state / issue state / gate state を推測しない。それらは operator/debug aids である。
- 退役済みの `.agent_handoff/tasks.yaml` queue、`read-next --wait`、Stop hook handoff wait は標準 transport ではない。legacy queue の残骸を drain する必要がある時だけ扱う。

## Handoff Startup Decision

Redmine gate (Review Request、Design Consultation など) を記録した後、sender は下記のどれで受信させるかを同じ gate に書く。gate と receive method の両方が Redmine に残るまで、handoff は delivered 扱いしない。

- **Standard path** — 高レベル primitive で receiver pane に通知する: `mozyo-bridge handoff send --to <claude|codex> --source redmine --issue <issue_id> --journal <journal_id> --kind <kind>`。reply では `mozyo-bridge handoff reply ...` / `mozyo-bridge reply ...` を使う。`notify-*` wrappers は compatibility entrypoints であり、standard と同じ primitive に乗る。Redmine gate には実行した command line、または同等の記録 (`notified <agent> via mozyo-bridge handoff send / notify-* journal <journal_id>`) を残す。
- **Receiver pane unavailable** — receiver が該当 agent terminal を開き、`mozyo-bridge init <agent>` を実行してから retry する必要があること、retry plan、attempted command を Redmine gate に記録する。chat には issue / journal id と pending operator action の短い pointer だけを書く。durable 手順を chat に再掲しない。
- **Notification fails or is unusable** — `not yet notified; receiver must read the gate manually` のように未通知状態を Redmine gate に明記し、attempted command、observed error、required receiver action を残す。chat には issue / journal id と un-notified state の短い pointer だけを書く。`.agent_handoff/tasks.yaml`、`read-next --wait`、Stop hook に fallback しない。
- **Sync handoff between two locally available agents** — 同じ host / session に agent がいても Redmine journal を省略しない。standard path と同じ。

`次の agent が拾う` だけで receive method がない報告は不完全であり、handoff delivered と扱わない。受信側にとって、すべての通知は Redmine gate への pointer であって命令本文ではない。

## 実装者 / 監査者境界

project が実装者と監査者 (または design consultation responder) を分ける場合、以下を適用する。単一 agent が両方を担当する project でも、action owner の境界は残る。

- 実装者は code、schema、tests、運用変更、Implementation Done Gate、Review Request Gate を担当する。
- 監査者は review、design consultation answer、rule interpretation、decision record を担当する。
- 監査者は通常開発 task の file を直接実装しない。監査者が通常開発 issue を受けた場合、標準 action は実装者への handoff である。
- `do it`、`implement`、`go ahead`、`対応して`、`実行せよ` のような命令形は、監査者が実装者境界を bypass する許可ではない。
- 監査者 direct edit は狭い例外に限る。明示的な direct edit 許可、既存ミスを記録する最小修正、または handoff すると進行中 release / CI を壊す緊急小修正。direct edit した場合は、適用した例外、ユーザー指示の引用、変更 files、verification、follow-up review 要否を Redmine に記録する。
- project が実装者 / 監査者 split を採用していない場合、task 中に勝手に split を作らない。採用するなら project rules に明示し、関係 agent に通知する。

## 判断の routing

owner に質問する前に、技術判断と owner 判断を分ける。

- 技術、設計、rule、既存仕様整合、UI structure、route、DB、authorization、spec / test methodology は design consultation 候補である。owner に投げる前に auditor または設計相談 role を通す。
- owner-only 判断は、権利・素材利用、法務文言、継続サービス / account、brand judgement、business priority、deadline、budget、release timing など。
- 技術判断を owner 判断として丸投げしない。owner は提示された選択肢を受け入れがちで、設計相談を飛ばしたコストは後の手戻りに隠れる。

## Scope Integrity

- 難しさは作業分割の理由であって、scope 縮小の理由ではない。owner が明示承認しない限り、acceptance criteria は維持する。
- scope は Redmine 上に残す。child issue、task、test issue に分けてもよいが、黙って落とさない。
- scope は UI だけではない。DB、model、controller、route、authorization、specification、manual verification、generated screenshot、seed data、既存 URL / data compatibility、operation flow を含み得る。
- Implementation Done に未完 scope を混ぜない。未完 scope は Progress Log Gate または child issue に出す。

## Verification Discipline

- project の authoritative verification command を実行する。docs catalog や generated file convention がある project では、それを使う。ただし、catalog / resolver tooling 自体は mozyo-bridge shared preset の必須依存にしない。
- UI / screen / operation flow 変更では、selector-level success を completion としない。screenshot、generated capture、visual review で意図した element、transition、state change を確認する。
- 非 UI 変更では、tests run、commands executed、observed output、remaining risks を具体的に記録する。`looks fine` は verification record ではない。
- verification が失敗または実行不能なら、誰かに通知する前に理由を Redmine に記録する。

## Stale And Retired Queue Handling

- retired local queue (`.agent_handoff/tasks.yaml` と `read-next --wait` fallback) は標準 transport ではない。通常運用に再導入しない。
- session restart 後や close 前に retired queue residue が疑われる場合だけ、project の stale-list command を実行し、残存 task を Redmine gate と照合する。Redmine を正本として close / fail / discard を決める。
- queue state だけで issue を close しない。passing Review Gate と明示的 owner approval に対応付ける。

## Completion

作業を complete 扱いする前に、以下を満たす。

1. requested work を acceptance criteria、scope、design consultation answer に照合して検証する。
2. material changes、verification、blockers、remaining risks、findings disposition を Redmine に記録する。
3. Review Gate を通す。Implementation Done だけでは completion ではない。Review Gate が Redmine に記録される前に chat で `complete` / `done` と報告しない。
4. issue status は project の Redmine workflow に従ってのみ更新する。final close は owner approval が支配する。audit-owned commit が必要な場合は、close 前に commit hash を Redmine journal に残す。
5. child issue、manual verification、generated capture confirmation、data-compatibility check、ops-flow check など、元 scope から未完のものが残っている場合、それは completion ではなく Progress Log である。parent issue を closed にしない。

## Audit-Owned Commit Authority

project が実装と監査を別 actor に分ける場合、監査 actor は Review Gate 承認済み diff を stage / commit できる。これは commit authority であって implementation authority ではない。監査 actor は通常開発 issue の file を修正してよいわけではない。

Preconditions:

- approval を記録した Review Gate journal が issue に存在し、journal id が捕捉されている。
- 別の実装 actor が review 対象 diff を作成している。監査 actor 自身が diff を作った場合、この authority ではなく direct edit として扱い、role-boundary exception に従って journal 化する。

Pre-commit checks:

- `git status` を実行し、dirty set を Implementation Done Gate の changed-paths list と照合する。
- Review Gate が承認した files だけを stage する。scope-outside 変更がある worktree で `git add -A` や `git add .` を使わない。
- `git diff --cached --stat` を実行し、必要なら `git diff --cached` で内容を確認する。staged set を Implementation Done Gate と行単位で照合する。
- 無関係な dirty files は除外する。別 issue に切る、stash する、または untouched のまま残す。scope-outside 変更を audit-owned commit に混ぜない。

Commit message reference (Redmine):

- `Refs: Redmine #<issue_id>` は必須。
- `Journal: <journal_id>` は必須。Review Gate approval の journal id を指す。
- subject line は通常の短い説明でよい。references は trailers または body に置き、`git log` だけで Redmine issue と承認 journal に replay できるようにする。

Post-commit recording:

- commit hash を同一 issue の Close Gate journal に記録する。Close Gate がまだ早い場合は Progress Log journal に記録する。hash は pane chat だけに置かない。
- Review Gate journal と commit-hash journal の両方が存在するまで、issue を closed status に移動しない。

Scope:

- 通常開発 issue にも、guardrail / rule / workflow issue にも、実装 / 監査 actor を分ける project では同じように適用する。
- split しない project では境界は collapsed するが、commit reference format と hash-journal requirement は残る。

## Prohibitions

- `Claude Code が常に実装、Codex が常に監査` のような固定 role split を shared preset に hard-code しない。split は project-configurable であり、project が採用した場合だけ境界が適用される。
- pane message、chat message、queue file を authoritative state として扱わない。
- retired `.agent_handoff/tasks.yaml` queue、`read-next --wait`、Stop hook handoff wait を通常運用に戻さない。
- `vibes/tools/mozyo_bridge` を runtime path として再導入しない。installed `mozyo-bridge` CLI を使う。
- source project 固有 path、Rails 固有 app path、private docs catalog、custom resolver script、source-project file conventions を shared preset の必須依存にしない。`project が提供している場合は使う` という条件付きに留める。
- `catalog.yaml` / docs resolver / nagger file conventions tooling の標準化は別タスクで扱う。この preset 変更に混ぜない。
- credential、token、personal data、private internal URL を repository file、Redmine note、pane message に記録しない。
- explicit な release task なしに、通常開発 task の中で release tag、version bump、publish を実行しない。
