# 管制塔 / サブレーン開発フロー

Redmine #12200。`mozyo_bridge` の通常開発が cockpit-visible sublane 前提へ移行したため、管制塔とサブレーンの責務分担を 1 つの spine として定義する。

この文書は repo-local の **一次 spine** である。管制塔 / サブレーン開発フローに関する dispatch、callback、review、close、integration、retirement の順序と責務はこの文書を先に読む。旧 `cockpit-sublane-operating-model.md` と `sublane-worktree-operating-runbook.md` の規約本文は本書へ統合済みであり、旧 docs は互換 pointer と履歴参照だけを残す。

この文書は詳細規則の複製ではない。既存の `agent-workflow.md`、`sublane-bandwidth-policy.md`、`sublane-worktree-operating-runbook.md`、skill workflow reference、central preset を、どの順序で読むかを決める地図である。

## 用語と表記ゆれ

owner / user は状況に応じて、同じ運用単位を `管制塔`、`メインレーン`、`メインセッション`、`メインユニット`、`coordinator`、`main lane` と呼ぶことがある。これは人間の記憶と会話上の揺れとして許容する。

本 flow では、これらの語が実装依頼や owner-facing 判断の文脈で出た場合、原則として **管制塔 Codex** を指すものとして解釈する。つまり、owner-facing、dispatch、仕様決定、audit、US close、integration、retirement、後続計画を担う actor である。

ただし、次は区別する。

- `main lane Claude`: 管制塔が補助的に使う Claude pane。read-only 調査、要約、draft、Design Consultation 補助はできるが、通常開発実装者ではない。
- `default lane` / `primary checkout`: checkout / workspace identity の概念。意思決定 actor ではない。
- `Owner`: product、Version close、release、production publish、credential / destructive / security-sensitive 判断の承認者。管制塔とは別である。

ユーザーが `メインでやって`、`メインレーンで判断して`、`管制塔で処理して` と言った場合、それは通常 **管制塔 Codex が判断・routing・audit を行う** という意味であり、main lane Claude に実装 diff を作らせてよいという意味ではない。

## 目的

- 管制塔が owner-facing、仕様決定、dispatch、audit、US close、retirement、後続計画を担当する。
- 通常開発の実装 diff は cockpit-visible sublane へ委譲する。
- 仕様決定と実装判断を混ぜない。
- US close と Version close の承認境界を分ける。
- close 済み sublane を退役させ、cockpit / worktree / agent context を残し続けない。
- ルールを既存 guardrail へ追記し続けるのではなく、本 flow を参照 spine として使う。

## 文書言語

この repo の LLM 向け規約本文は日本語で書く。英字の固定フィールド名、gate 名、CLI option、コード識別子、branch 名、path はそのまま保持してよいが、見出しと説明本文を英語だけで置かない。

```yaml
language_policy:
  prose: 日本語
  headings: 日本語
  allowed_literal_tokens:
    - fixed field names
    - gate names
    - CLI options
    - code identifiers
    - branch / path names
  forbid:
    - operator-facing 規約本文を英語のみで追加する
    - skill / runbook へ英語本文を置き、repo-local 日本語 spine と意味を分岐させる
```

LLM 向け規約文書の一般 authoring rule は `.mozyo-bridge/rules/llm_rule_authoring.md` の `## 言語` を正本とする。本 flow では、サブレーン開発フロー固有の適用として「本文は日本語、固定フィールド名は literal token」と明示する。

## ルール配置判断

guardrail は書けばよいものではない。agent が迷った事実を durable record 化するために書くが、配置を誤ると「読まれるべき rule」が増えるだけで、実行時の判断精度は下がる。

新しい超大 rule を作る前に、管制塔は次を確認する。

```yaml
placement_order:
  1_existing_spine:
    条件: 既存 flow / runbook / policy の責務内で説明できる
    action: 既存文書へ短い section または参照を追加する
  2_authoring_rule:
    条件: LLM 向け規約文書の書き方、正本分離、形式選択、gate 構造化そのもの
    action: `.mozyo-bridge/rules/llm_rule_authoring.md` の upstream / central preset 更新候補として扱う
  3_catalog_governance:
    条件: catalog、resolver、generated file、coverage、audit-impact の統治
    action: `.mozyo-bridge/rules/docs_catalog_governance.yaml` の upstream / central preset 更新候補として扱う
  4_new_repo_local_logic:
    条件: 既存 spine に入れると責務が混ざり、かつ project-local に閉じる判断軸がある
    action: `vibes/docs/logics/**` に小さい spine を作り catalog 登録する
  5_new_central_rule:
    条件: 複数 project に配布すべき実行契約で、既存 authoring / catalog / workflow へ自然に入らない
    action: central preset 昇格 issue を作る。repo-local で巨大 rule を先に固定しない
```

新規 rule / logic を増やす soft trigger:

- 既存文書へ入れると、読者 actor、責務、停止条件、検証責務が混ざる。
- 1 つの判断を 2 つ以上の既存文書へ重複記載しそうになる。
- PlantUML activity + swimlane で actor 境界を描かないと、管制塔 / sublane / Owner の責務が誤読される。
- 表記ゆれ、alias、非同義語を明示しないと、次セッションで routing が壊れる。

新規 rule / logic を増やさない hard stop:

- 「念のため」「あとで迷いそう」だけで、観測可能な trigger と durable-record 出力が無い。
- 既存 spine へ 5-10 行で足せる。
- 入口文書、router、skill reference へ詳細本文を複製しようとしている。
- central preset 配布面 (`.mozyo-bridge/rules/**`、skill、scaffold preset) の話なのに、repo-local doc で恒久正本化しようとしている。

flow 型 guardrail の書き方、PlantUML activity + swimlane の使い方、Markdown 補足境界、`$validate` / `$forbid` / `$record` primitive は `.mozyo-bridge/rules/llm_rule_authoring.md` を正本とする。この文書にはサブレーン開発フロー固有の判断だけを残す。

## 役割

詳細な実行責務は `標準フロー` の swimlane を読む。ここでは actor の authority だけを定義する。

```yaml
Owner:
  authority: product / release / Version close / production publish / credential / destructive / security-sensitive approval
管制塔 Codex:
  authority: owner-facing, dispatch, coordinator-owned design decision, audit, US close, integration disposition, sublane retirement, follow-up planning
main lane Claude:
  authority: read-only 調査 / 要約 / draft / design consultation 補助。通常開発実装者ではない
target-lane Codex:
  authority: cross-lane gateway / same-lane Claude handoff / coordinator callback
sublane Claude:
  authority: bounded implementation / implementation_done / review_request。owner close approval は収集しない
```

## 運用モデル

cockpit-visible sublane では、次の 4 つを混同しない。

```yaml
identity: workspace / lane / role / pane の durable な事実
routing: どの agent が handoff を受け取り、行動してよいか
display: pane / window / tab / iTerm / tmux view の見せ方
governance: どの Redmine gate が実行や close を承認しているか
```

window layout は人間が関連作業を見やすくするための display であり、routing の source of truth ではない。隣に pane が見えていても、lane 境界や project 境界を越えた direct send の承認にはならない。

### レーンと actor

- **管制塔 Codex** は coordinator、auditor、owner-facing actor である。owner への質問、close approval 回収、Redmine gate 解釈、review conclusion、release / push / CI coordination、sublane 作成・退役、PoC finding の Redmine / repo-local docs 記録を担当する。
- **target-lane Codex** はその lane の gateway である。durable Redmine anchor を読み、自 lane に属する request か確認し、same-lane Claude へ route し、blocked / review-ready / owner-action-needed を管制塔へ callback する。
- **sublane Claude** は bounded implementation worker である。pane scrollback ではなく Redmine journal から実装し、implementation_done / review_request / verification / residual risk を再現可能に残す。owner close approval は回収しない。
- **main lane Claude** は補助 actor である。長い journal / diff / log の要約、candidate 抽出、read-only 調査、draft wording、非権威的な option 比較には使えるが、通常開発実装者でも owner-facing coordinator でもない。

main lane Claude が implementation request を受け取った場合は、実装前の設計矛盾・scope 不足・invariant 衝突を design consultation として整理してよい。ただし、調査や reroute 用の事実整理を終えたら停止する。実装 diff は専用 sublane / worktree に移して、target-lane Codex gateway 経由で same-lane Claude へ渡す。

### レーン作成単位

一つの作業単位は次の対応で扱う。対応は Redmine issue / journal に記録し、pane 配置から推測しない。

```text
work unit = 1 issue
          + 1 branch
          + 1 git worktree
          + 1 lane
          + 1 Codex pane
          + 1 Claude pane
```

worktree の add / remove は素の git で行う。mozyo-bridge core は Git worktree manager ではない。具体 path / branch 命名、local soft profile、private cockpit composition は operator runtime policy であり OSS default に混ぜない。

```text
git worktree add <worktree-path> -b <branch>
mozyo cockpit ...
mozyo-bridge init claude   # / codex
mozyo-bridge agents targets --session <cockpit-session>
```

## 実行 runbook

この節はサブレーン作成から退役までの時系列手順である。判断規約は本書の各節を正とし、旧 runbook へ再分散しない。

1. Redmine issue / journal / parent / Version / 参照 docs を読む。
2. work unit と branch / worktree / lane / pane の対応を Redmine に記録する。
3. dispatch 前に bandwidth admission を確認する。未読 review_request、owner_waiting、blocked callback、retire_ready lane が残る場合は先に drain する。
4. cross-lane handoff は target-lane Codex gateway へ送る。Claude への direct delivery は same-lane addressing に限定する。
5. target-lane Codex が durable anchor を読み、same-lane Claude へ実装依頼を submit 完結で渡す。`--no-submit` / `--mode pending` は operator / debug fallback であり標準 dispatch default にしない。
6. sublane Claude が implementation_done / review_request を Redmine に記録する。commit hash を gate に書く場合は origin reachability を先に確認する。
7. sublane は handoff-worthy state で管制塔 Codex へ callback する。callback は Redmine durable anchor への pointer であり、work log ではない。
8. 管制塔 Codex が review / owner close approval / integration disposition / Close Gate を処理する。
9. close 後、管制塔 Codex が retirement drain を実行する。retire_ready / retired journal で destructive 操作の前後を bracket する。
10. callback / review / owner / integration / close / retirement を drain してから、後続 Version / US 提案へ進む。

### callback 欠落時の sweep

callback は pointer なので、欠けても durable progress は消えない。管制塔は新しい sublane を開く前に active lane の Redmine journal を sweep し、次を分類して記録する。

- `progress_without_callback`: durable progress はあるが coordinator callback / ack が無い。既存 journal を拾って review / close flow へ進め、done な work を再 dispatch しない。
- `no_progress_after_handoff`: delivery anchor はあるが新しい durable journal が無い。期待 gate を明示して再通知または blocker 化する。
- `callback_delivery_failed`: callback 試行が失敗している。target 解決、window-binding preflight、stale CLI などの失敗理由を読む。
- `callback_not_attempted`: durable progress はあるが callback / receive-method journal が無い。process gap として記録し、必要なら sublane 側へ補正を依頼する。

この sweep は owner approval や close を self-authorize しない。review gate、owner close approval、status close はそれぞれ別 gate として処理する。

## 仕様決定 routing

管制塔が持つ仕様決定は、後戻りコストが高いもの、横断的なもの、または authority / safety に触れるものである。

### 管制塔で決める

- 複数 UserStory、複数 Version、複数 provider / module / surface に影響する判断。
- file path、config file name、schema version、source-of-truth、config precedence。
- workflow authority、owner approval、review authority、close approval、routing authority、handoff / send safety、credential / secret / auth / permission / billing / destructive-operation、release / publish approval に関わる判断。
- user-facing behavior、operator UX、diagnostics、validation command の標準。
- migration、backward compatibility、public/private boundary、future plugin API への制約。
- 「どちらでも実装できる」が、選択により今後の roadmap が変わる判断。
- sublane 間で file / invariant / merge order が衝突する判断。

### サブレーンで決めてよい

- 1 UserStory 内に閉じる local implementation detail。
- helper 関数、class split、test file 分割、internal naming。
- coordinator 決定済み方針から機械的に導ける edge case。
- migration や利用者影響が無い小さい error message detail。

### エスカレーション

実装中に coordinator-owned 仕様決定が必要になった場合、sublane は実装を止め、Redmine に design consultation / blocked / owner-action-needed を記録し、coordinator Codex へ callback する。

## 標準フロー

PlantUML の activity diagram + swimlane 記法で、誰が責務を持つかを明示する。管制塔と sublane の境界を読むための図なので、細かい retry path はここに複製しない。

validation / 禁止事項 / durable record は、図の流れから離れた長い箇条書きにせず、必要に応じて `$validate` / `$forbid` / `$record` で近接させる。

```plantuml
@startuml
!procedure $validate($rule)
:validate: $rule;
!endprocedure
!procedure $forbid($rule)
:forbid: $rule;
!endprocedure
!procedure $record($anchor)
:record: $anchor;
!endprocedure

|管制塔 Codex|
start
:管制塔が prompt / marker / ticket ID を受け取る;
:Redmine issue / journal / Version / catalog docs を読む;
$validate("pane / chat message を正本にしない");
:作業形状を分類する;

if (coordinator-owned 仕様決定が必要?) then (yes)
  :管制塔が仕様決定を Redmine / cataloged doc に記録;
  $record("coordinator-owned design decision");
endif

if (実装型?) then (yes)
  $forbid("管制塔 Codex が通常実装 diff を直接作る");
  $forbid("main lane Claude へ実装型 work を直接渡す");
  :blocking queue を drain;
  :sublane admission を判定;
  if (dispatch 可能?) then (yes)
    :dispatch decision を Redmine に記録;
    $record("dispatch decision");
    :target-lane Codex gateway へ handoff;
    |target-lane Codex|
    $validate("cross-lane は target-lane Codex gateway 経由");
    :target-lane Codex が same-lane Claude へ handoff;
    |sublane Claude|
    :sublane Claude が実装;
    $forbid("coordinator-owned 仕様決定を実装 commit 内で黙って確定");
    :Implementation Done / Review Request を記録;
    $record("implementation_done / review_request");
    |target-lane Codex|
    :target-lane Codex が coordinator へ callback;
    |管制塔 Codex|
    $validate("callback は Redmine durable anchor への pointer");
    :管制塔が Review Gate を処理;
  else (no)
    |管制塔 Codex|
    :stop_and_drain / blocker / main_lane_exception を記録;
    stop
  endif
else (no)
  |管制塔 Codex|
  :管制塔が owner-facing / audit / planning / design consultation として処理;
endif

if (Review approved?) then (yes)
  :owner close approval を確認または standing delegation で記録;
  $validate("Review approval と owner close approval を分離");
  :commit-bearing work の integration disposition を記録;
  $validate("commit-bearing work に integration disposition がある");
  :Close Gate を記録;
  :US status を close;
  :routine retirement 条件を確認;
  if (retire_ready?) then (yes)
    :sublane を退役;
  endif
  :後続 guardrail 更新要否を評価;
  :後続 Version / US を提案し owner 承認を求める;
  |Owner|
  :後続 Version / US 提案を承認または差し戻す;
  |管制塔 Codex|
  :承認後に Version / US を作成;
  :必要な仕様決定を記録;
  :新規セッション prompt 例を issue ID 付きで提示;
else (no)
  |管制塔 Codex|
  :findings を Redmine に記録;
  $record("review findings");
  |target-lane Codex|
  :sublane Claude へ修正依頼;
endif
|管制塔 Codex|
stop
@enduml
```

## US close と Version close

- US close は管制塔 Codex が担当する。Review Gate approved、owner close approval journal、integration disposition、Close Gate が揃った場合、standing delegation 条件下で status close できる。
- Review Gate approval は close approval ではない。owner close approval journal は別に記録する。
- commit-bearing work は、target branch merge / push / patch-equivalence / explicit deferral のいずれかを Close Gate の basis に含める。
- Version close は owner approval を要求する。管制塔は readiness summary、残 open issue、release / publish scope、follow-up version を提示し、owner 承認後に閉じる。

## サブレーン退役

管制塔は、US close 後に sublane retirement を必ず検討する。retirement は後続提案より前に行う。

routine retirement の条件:

- issue が close 済み、または scope が explicit defer 済み。
- commit-bearing work が target branch に統合済み、または patch-equivalent / explicit deferral が durable record にある。
- worktree が clean、または残 diff が disposable local runtime state と判定済み。
- active review / owner_waiting / blocked / callback_due が無い。
- lane identity が明確で、削除対象 worktree を取り違えない。

条件を満たす場合、管制塔は owner 確認なしに退役してよい。条件を外れる場合は Redmine に理由を残し、retirement を止める。

### 退役 fixed fields

retirement は destructive 寄りの操作なので、pane kill / worktree remove の前後を Redmine journal で挟む。閉じた lane は default retire candidate だが、dependency ancestor lane は downstream merge / rebase が消費されるまで保持できる。

```yaml
retirement_state:
  candidate: retire_candidate
  ready: retire_ready
  retained: retain_until_downstream_consumed
  blocked: retire_blocked
  done: retired
fixed_fields:
  - retirement_state
  - lane
  - worktree
  - pane
  - redmine_issue_state
  - retain_reason
  - downstream_consumed
  - retire_blockers
  - safety_preflight
  - durable_anchor
```

`retire_ready` に進める前に、次の `safety_preflight` がすべて true であることを記録する。

```yaml
safety_preflight:
  redmine_closed: true
  worktree_clean: true
  origin_reachable: true
  pending_prompt_absent: true
  callback_drained: true
  target_identity_known: true
```

次のいずれかが残る lane は `retire_blockers` に記録し、`retire_ready` にしない。

- active lane。
- review pending。
- owner approval pending。
- unresolved callback。
- dirty worktree。
- pending prompt。
- unpushed commit。
- unknown target identity。

退役後は `retired` journal に removed / killed した worktree、pane、branch、`durable_anchor` (`retire_ready` journal) を残す。retirement は close を自己承認しない。既に close 済み、または explicit defer 済みの lane だけを対象にする。

## 後続 Version / US 提案の順序

1. active sublane の callback / review / owner / integration / close / retirement を drain する。
2. 管制塔自身の guardrail 更新要否を評価する。必要なら repo-local autonomous lane として更新し、Redmine に記録する。
3. 後続 Version の目的、scope、非目標、due date を提案する。
4. owner 承認後に Version を作成する。
5. Version ごとの UserStory を作成し、親 Feature と relation を設定する。
6. 実装前に必要な coordinator-owned 仕様決定を Redmine / cataloged doc に残す。
7. 新規セッション prompt 例を、開始すべき issue ID と durable anchor とともに提示する。

## 失敗として扱う例

`標準フロー` の `$forbid` / `$validate` に反する状態は失敗として扱う。特に、main lane Claude への実装直送、hidden subagent の sublane 扱い、owner close approval と Review Gate の混同、integration disposition なし close、retirement 未検討の lane 放置、Version close の owner approval bypass は invalid である。

## 参照正本

- `vibes/docs/rules/agent-workflow.md`
- `vibes/docs/logics/sublane-bandwidth-policy.md`
- `vibes/docs/logics/sublane-worktree-operating-runbook.md`
- `vibes/docs/logics/worktree-lifecycle-boundary.md`
- `vibes/docs/logics/cockpit-sublane-operating-model.md`
- `skills/mozyo-bridge-agent/references/workflow.md`
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md`
- `.mozyo-bridge/rules/llm_rule_authoring.md`
- `.mozyo-bridge/rules/docs_catalog_governance.yaml`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/coordinator-sublane-development-flow.md --repo . --format text`
