# ADR: durable な担当 lane と交換可能な agent session

- status: accepted（設計決定。実装は未着手）
- date: 2026-08-10
- decision issue: Redmine #15228
- observed incident: Redmine #15227

## Context

host 再起動後、Claude / Codex の会話 session と assigned name は復元されたが、実際に
command を実行する shell から workspace / provider / lane の識別情報と一部の CWD が
失われた。pane は live に見えても standard handoff を実行できず、`session-start` は
既存 process を adopt するため修復にならなかった。

この障害で重要なのは会話の完全保存ではない。より大きなリスクは、途中の issue と
worktree が「作業中・レビュー待ち・保留・再開必要・終了」のどれかに分類されないまま
残り、数週間後の統合や release で初めて発見されることである。

既存正本はすでに次を分離している。

- workflow truth: Redmine issue / journal
- code state: Git branch / commit / worktree
- desired lane lifecycle: mozyo-bridge managed state
- current liveness: action-time の runtime observation
- provider conversation: Claude / Codex 等が任意に再開できる会話 state

本 ADR は、この分離の上に「どの LLM 側の担当単位が issue を受け持っているか」と、
担当 process を安全に交換する判断を定める。

## Decision

### 1. 担当の単位は provider session ではなく lane generation とする

本 ADR では、Redmine issue を現在受け持つ論理的な単位を **担当 lane** と呼ぶ。これは
人間の Redmine 担当者を置き換えない。最低限、次を結び付ける durable record である。

```yaml
durable_lane_assignment:
  workspace_id: <registered workspace>
  issue_id: <Redmine issue>
  lane_id: <managed lane>
  lane_generation: <positive generation>
  responsibility_scope: release | version | user_story | task
  workflow_role: <canonical role profile role>
  worktree_identity: <managed worktree identity | not_applicable>
  decision_journal: <current Redmine journal>
  state: active | review_waiting | parked | replacement_required | terminal
```

field の exact storage schema と command 名は実装 Task で決める。ただし authority の単位
`workspace + issue + lane + generation` と、Redmine journal への durable pointer は変更しない。
pane label、process ID、CWD、provider 名、会話 session ID のいずれか単独を担当 authority
にしない。

### 2. provider の会話 ID は任意の再開 hint に限定する

Claude / Codex / 後続 provider が会話再開用の ID を提供する場合、provider adapter はそれを
opaque な任意情報として保持してよい。共通層は値を解釈せず、次の用途に使わない。

- lane identity の決定
- routing / handoff authority
- CWD または worktree の決定
- workflow completion、review、close の判定

resume hint が無い、失効した、または provider が resume を提供しない場合は fresh session
を起動する。会話を復元できないことは、issue / worktree を引き継げない理由にならない。
resume hint が秘密または個人情報を含み得る場合は Redmine や tracked file に記録しない。

### 3. agent 自身の照会と coordinator の全体照合を分ける

agent 向け read surface は、文字 command 一つで少なくとも次を返す。

- issue ID、短い subject、責任範囲
- lane ID、generation、workflow role
- latest durable journal と現在 state
- branch / HEAD / clean・dirty / origin 到達性の既知状態
- blocker、next action、next actor

「自分」は同じ command context の sender identity、workspace anchor、lane lifecycle、worktree
binding が一致した場合だけ解決する。不一致または欠落時は推測せず
`replacement_required` 相当の typed result を返す。

coordinator 向け read surface は provider session 内の自己申告に依存せず、target Version / US
について Redmine、lane lifecycle、worktree、live runtime を照合し、次を列挙する。

- issue に担当 lane が無い
- active な担当 lane に検証済み runtime が無い
- lane / generation と live process が一致しない
- issue または lifecycle に結び付かない worktree がある
- dirty、unpublished、review waiting、replacement required の状態がある

### 4. 身元不明の managed session は延命せず交換する

agent の command-shell identity を検証できない場合、その session を正常な routing target と
して維持しない。手動 env 注入や pane label からの推測で修復せず、担当 lane の新しい
generationへ交換する。

交換前に、coordinator は次の客観的事実を bounded に取得する。

1. Redmine の latest journal と未解決 gate
2. branch / HEAD / origin 到達性
3. worktree の clean・dirty、未追跡変更の有無
4. lane lifecycle revision / generation / worktree binding
5. exact managed slot、runtime state、pending composer の有無
6. 必要な場合だけ、秘密を除いた bounded な直近出力

pane scrollback は補助資料であり authority ではない。agent の未記録の思考を完全保存する
ことも交換条件にしない。記録できない intent は推測せず、「客観的な state から fresh
session が再判断する」と Redmine に残す。

close の対象は workspace / lane / role / generation と exact に結び付いた managed slot に
限定する。inventory が unreadable、target が ambiguous、foreign occupant がある、または
replacement launch を同じ worktree へ行えることを証明できない場合は zero-close とする。
「自分を特定できない agent がいる」ことを、無関係な process を一括 close する根拠にしない。

交換 action は次の順序を一つの replayable operation として扱う。

1. action-time に Redmine / lifecycle / worktree / live slot を再検証する
2. preservation facts と置換理由を durable journal に記録する
3. exact old slot だけを close する
4. 同じ lane / worktree、次 generation で fresh session を起動する
5. startup state と実 command-shell identity / CWD を別々に検証する
6. new session へ issue + latest journal pointer を渡す
7. Redmine と worktree を再読させ、next action を新 session 自身に判断させる

途中で失敗した場合は、適用済み effect と owed action を記録し、旧 session が残っていると
推測して success にしない。

### 5. worktree と durable work state は session 交換で保持する

session 交換は reset、stash、branch delete、worktree remove、commit rewrite を行わない。
dirty worktree は自動破棄せず、boundary journal を先に作る。clean / pushed であっても、最新
commit、verification、review state は Redmine journal と照合する。

fresh session は会話履歴ではなく次から復元する。

- Redmine issue / latest journal
- cataloged docs
- lane lifecycle / worktree binding
- Git branch / HEAD / diff
- owner approval と未解決 gate

### 6. 責任階層は既存 role を責任範囲へ対応付ける

本 ADR は canonical role profile の語彙を変更しない。担当 lane の
`responsibility_scope` を使い、少なくとも次を表現可能にする。

| responsibility scope | 主な責任 |
| --- | --- |
| `release` | 複数 Version の統合、release readiness、残作業の最終確認 |
| `version` | Version 内の US、依存関係、完了条件の監査 |
| `user_story` | 一つの US と子 Task / Bug / Test、実装と review loop の完結 |
| `task` | 一つの Task / Bug / Test の一時的な実装 |

provider binding は別設定であり、責任範囲を `codex` / `claude` に固定しない。

### 7. release 前に未分類作業を列挙する

target Version の release readiness は、少なくとも次が未分類で残っていれば green にしない。

- open issue だが担当 lane / 明示的な parked decision が無い
- active lane だが valid runtime が無い
- orphan worktree、dirty worktree、unpublished commit
- review waiting、replacement required、callback / integration due
- Redmine、lane lifecycle、Git の issue / generation / worktree 対応が矛盾する

別 Version へ明示的に移されたもの、理由と再開条件を持つ parked work、terminal state は
未分類に数えない。時間経過だけで abandon や close を決めない。

## Rejected alternatives

### Provider session ID を担当 identity にする

provider 固有で、失効・fork・duplicate があり、CWDやcommand-shell identityを保証しないため
採用しない。

### Pane名またはassigned nameを唯一の正本にする

label はprocess世代を越えて残り、stale processをhealthyに見せ得るため採用しない。

### 会話sessionを可能な限り永久に保存する

保存コストが作業の分類・引継ぎより高くなり、壊れたsessionを延命する誘因になるため必須条件
にしない。resume可能なら利用するに留める。

### 身元不明processを全て即時closeする

foreign process、観測不能状態、対象の取り違えを区別できないため採用しない。exact managed
slotとreplacement可能性を検証できる場合だけ交換する。

### Redmineの人間担当者欄をLLM担当へ転用する

人間の説明責任とruntime ownershipを混同し、実装者・reviewer・coordinatorを一つのfieldで
表せないため採用しない。

## Consequences

- 会話sessionの一部を失っても、issueとworktreeを放置せずfresh sessionへ引き継げる。
- providerを追加しても、共通の担当 / recovery modelを変えずadapterだけを追加できる。
- 「paneがlive」を「作業が正常」と同一視しなくなる。
- replacementはdestructive effectを含むため、exact target、owner approval、action-time再検証を
  実装するまで自動化できない。
- Redmine、lane lifecycle、Git、runtimeの照合面が増える。単一DBや単一labelへ縮退させない。

## Implementation split

ADR採用後の実装は少なくとも次の子Taskへ分割する。

1. 担当 lane record と Redmine journal schema
2. agent向けcurrent-work read surface
3. coordinator向けVersion / US / worktree reconciliation surface
4. exact session replacement preflight / owner approval / actuator
5. optional provider resume adapter
6. release / drain queue への未分類作業 gate

各Taskはprovider fakeだけで完了させず、fresh sessionがissue IDとworktreeから再開するscenario
testを持つ。

## Related documents

- `vibes/docs/specs/session-continuity-user-harness.md`
- `vibes/docs/logics/session-boundary.md`
- `vibes/docs/specs/herdr-native-identity.md`
- `vibes/docs/logics/managed-state-model.md`
- `vibes/docs/logics/worktree-lifecycle-boundary.md`
- `vibes/docs/specs/delegated-coordinator-decision-records.md`
- `vibes/docs/tasks/herdr-lane-operations.md`
