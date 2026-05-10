# Agent Workflow Rules

## 目的

この文書は `mozyo_bridge` repository で作業する AI agent の実行規約である。root の `AGENTS.md` / `CLAUDE.md` は router に留め、詳細規約はこの文書に置く。

## 作業開始

- セッション開始時に Notion のグローバル規約を fetch する。
- 現在の `cwd` が対象 repository root、またはその配下であることを確認する。
- Asana project `mozyo_bridge` の project notes を確認する。
- active な Asana task を確認する。該当 task がない場合は、実装前に作成する。

## Asana 運用

- Asana は実行キューである。
- Task は実行単位であり、目的、作業対象、成果物、完了条件を持つ。
- 作業が完了、block、または scope 変更された場合は、該当 task の comment または notes を更新する。
- chat message を durable な作業ログとして扱わない。
- task scope が膨らんだ場合は、黙って削らず follow-up task に分割する。
- Claude の通常開発完了 comment には、次の最小証跡を短く残す。
  - Notion global rules を fetch したこと。
  - `mozyo-bridge-agent` skill を loaded したこと。
  - active Asana task と project notes を確認したこと。
  - 追加で参照した relevant rule / reference がある場合は、その path または source。
- 上記は監査可能性のための証跡であり、全 reference を毎回読むことを要求しない。

## Secret Handling

- PyPI / TestPyPI token、API key、personal credential、個人情報を repository、Asana、Notion に記録しない。
- `.env`、`.env.*`、`.pypirc` は local-only の secret surface とし、ignored のままにする。
- production publish は local token upload を標準 route にしない。

## mozyo-bridge の扱い

- `mozyo-bridge` は notification transport であり、review、completion、task state の source of truth ではない。
- pane message を受けた agent は、作業前に Asana task または明示された source of truth を確認する。
- marker が観測される前に Enter を送る safety behavior を壊さない。
- `.agent_handoff/tasks.json` は retired queue の棚卸し用であり、standard notification fallback として扱わない。

## User Interaction And Escalation

- Claude は active Asana task の scope 内では自律的に作業する。通常はユーザーへ直接質問しない。
- Claude は以下に該当する場合だけ Codex へ escalation する。
  - Asana task の目的、成果物、完了条件が曖昧である。
  - 規約、Notion、Asana、repository docs の間に矛盾がある。
  - shared skill、scaffold preset、repo-local policy の境界判断が必要である。
  - destructive、irreversible、release、publish、tag、version bump など外部影響のある操作判断が必要である。
  - secret、credential、個人情報、権限、認証に触れる可能性がある。
  - ユーザー意図の解釈が複数あり、間違えると作業が無駄になる。
  - audit finding への対応方針が source of truth から決めきれない。
- Codex は escalation を受けたら、既存の source of truth から判断できるかを先に確認する。判断できる場合はユーザーへ質問せず、判断と根拠を Asana に記録する。
- Codex は source of truth だけでは推測になる場合に限り、ユーザーへ問い合わせる。ユーザーとの対話窓口は原則 Codex に統一する。
- ユーザーが Claude に直接指示した場合、Claude は必要に応じて Asana comment または Codex への通知で source of truth を更新してから続行する。

## Claude / Codex Role Boundary

- 通常開発 task の実装者は Claude とする。Codex は通常開発 task を直接実装しない。
- Codex は escalation、audit、ユーザー対話窓口、source of truth からの判断整理を担当する。
- Codex が自律フロー反映確認 task を受けた場合、検証対象となる通常開発 task を選定し、Claude へ handoff する。
- Codex は handoff 前に、選定理由、対象 task、既存 worktree 差分の扱い、Codex の後続 audit 役割を Asana に記録する。
- Codex が誤って通常開発 task を直接実装した場合、その実行は自律フロー反映確認の成功条件に数えない。
- 上記の誤実装が発生した場合、確認 task を未完了に戻し、誤実装を Asana に correction として記録したうえで、Claude 実装から Codex audit までの flow をやり直す。

## Policy / Skill Authoring Boundary

- 自律フロー、規約、skill、handoff、audit、release / distribution gate の変更では、Codex は方針整理、文案作成、ユーザー対話、audit を担当する。
- 上記の repo ファイル変更実装者は原則 Claude とする。Codex は通常時、規約や skill reference の repo ファイルを直接編集して commit しない。
- Codex が直接実装してよい例外は、ユーザーが明示的に Codex direct edit を指示した場合、Claude に引き継ぐと作業が壊れる緊急小修正、または既存の誤実装を記録するための最小 correction に限る。
- Codex が例外として直接実装した場合は、Asana に `Codex direct edit` として理由、変更ファイル、verification、後続確認の要否を記録する。
- 自律フローや role boundary の変更を Codex が直接実装した場合でも、変更後の反映確認 requirement は免除されない。

## Audit Handoff (Claude → Codex)

- Claude が code、documentation、設定を作成、修正、削除した task は完了前に必ず Codex に audit を依頼する。documentation のみの変更でも省略しない。
- 依頼経路は `mozyo-bridge message codex <text>` を標準とする。`message` 送信前には同 pane を `mozyo-bridge read codex` で確認する (mozyo-bridge の message safety guard)。
- audit 依頼 message には次を含める。
  - Asana task の URL
  - 変更ファイルの一覧
  - 実施した verification
  - 重点的に audit してほしい観点
- Codex の audit feedback が Asana コメントまたは明示的な通知として返るまで、Claude 側の task を completed として扱わない。pane に echo されただけの応答を audit pass と判定しない。
- audit で issue が指摘された場合は修正 commit を打ち、同じ Codex pane に再 audit を依頼する。
- この mandatory audit rule は `mozyo_bridge` repository の project-local policy であり、shared skill や scaffold preset へ一般化しない。
- `mozyo-bridge scaffold rules <preset>` ではユーザーが ticket system preset を明示選択する。選択された preset の workflow だけを適用し、他 preset やこの repo 固有の audit policy を混ぜない。

## Workflow Change Verification

- 自律フロー、skills、rules、handoff、escalation、release / distribution gate を変更した場合は、変更後に新規セッションで反映確認を行う。
- 反映確認は `mozyo_bridge` 本体の通常開発 task で行う。検証対象の規約や skill そのものを変更する task を検証対象にしない。
- 反映確認の通常開発 task は Claude が実装し、Codex は handoff と audit を担当する。Codex は検証対象 task を直接実装しない。
- task の大小や production 影響の有無では検証対象を判定しない。判定軸は、検証対象の自律フロー規約、skill、workflow、release / distribution gate を直接変更する作業かどうかである。
- 反映確認では、agent が起動時規約、Asana task、source of truth、handoff / escalation、audit、verification 記録を想定どおり扱ったかを確認する。
- 反映確認の結果は Asana に記録する。問題があれば follow-up task を起票する。

## 禁止事項

- root の `AGENTS.md` / `CLAUDE.md` に詳細規約を大量貼り付けしない。
- `vibes/tools/mozyo_bridge` を runtime path として再導入しない。
- Redmine / Rails / vibes 前提の別 project 規約を、この repository に無断で持ち込まない。
- generated build outputs を commit しない: `build/`, `dist/`, `*.egg-info/`, `__pycache__/`。
