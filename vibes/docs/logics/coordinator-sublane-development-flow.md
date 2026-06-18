# 管制塔 / サブレーン開発フロー

Redmine #12200。`mozyo_bridge` の通常開発が cockpit-visible sublane 前提へ移行したため、管制塔とサブレーンの責務分担を 1 つの spine として定義する。

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

flow 型 guardrail を作る場合は、原則として次を含める。

- `目的`: 何を減らすための flow か。
- `役割`: actor ごとの責務。管制塔 / sublane / Owner を混ぜない。
- `routing 条件`: 管制塔で決める条件、sublane へ渡す条件、停止条件。
- `PlantUML activity + swimlane`: 誰が何をするかを図で固定する。
- `用語と表記ゆれ`: 正規語、alias、非同義語を分ける。
- `参照正本`: 既存 rule / runbook / catalog への参照。本文を複製しない。
- `検証`: catalog validate、generated check、audit-impact、resolve、diff check。

## 役割

### Owner

- product / release / version close の最終判断を持つ。
- 通常開発 US の close approval は、central preset の standing delegation 条件を満たす限り管制塔へ事前委任できる。
- Version close、production publish、release/tag/publish、credential、destructive、security-sensitive な判断は管制塔へ委任しない。

### 管制塔 Codex

- 最初の prompt / pane marker / ticket ID を受け取り、Redmine と catalog docs を source of truth として読む。
- 作業を分類し、sublane に委譲するか、管制塔だけで扱うかを決める。
- 実装型作業を自分では実装しない。
- 実装型作業を main lane Claude に直接渡さない。ただし read-only 調査、要約、draft、Design Consultation 準備は例外。
- coordinator-owned 仕様決定を行い、Redmine journal または cataloged design doc に durable record として残す。
- sublane の Review Gate を読むか実施し、owner close approval を集約し、integration / Close Gate / status close を処理する。
- routine retirement 条件を満たす sublane を owner 確認なしに退役させる。
- sublane 承認と退役の後に、後続 Version / US の提案を owner に出す。
- 後続 Version / US 作成後、必要な仕様決定と管制塔自身の guardrail 更新要否を再評価する。
- 新規セッションへ渡す prompt 例を、Redmine issue ID とともに示す。

### main lane Claude

- 管制塔が使う補助 actor である。
- 実装型 diff を作らない。
- 許される用途は read-only 調査、要約、draft、Design Consultation 補助、または管制塔が明示した非実装作業に限る。
- 通常開発実装が必要になった場合は、専用 sublane へ移す。

### target-lane Codex

- cross-lane dispatch の gateway である。
- durable anchor を読み、自 lane の Claude へ same-lane handoff する。
- sublane の review result、owner_waiting、blocked、implementation_done、review_request など handoff-worthy state を coordinator へ callback する。
- coordinator-owned 仕様決定を勝手に確定しない。必要なら option / tradeoff / recommendation として coordinator へ戻す。

### sublane Claude

- bounded implementation worker である。
- Redmine issue / journal と catalog docs から実装する。
- 実装判断は行えるが、coordinator-owned 仕様決定は確定しない。
- implementation_done / review_request を durable record に残し、verification と residual risk を再現可能にする。
- owner close approval を収集しない。

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

### sublane で決めてよい

- 1 UserStory 内に閉じる local implementation detail。
- helper 関数、class split、test file 分割、internal naming。
- coordinator 決定済み方針から機械的に導ける edge case。
- migration や利用者影響が無い小さい error message detail。

### escalation

実装中に coordinator-owned 仕様決定が必要になった場合、sublane は実装を止め、Redmine に design consultation / blocked / owner-action-needed を記録し、coordinator Codex へ callback する。

## 標準フロー

PlantUML の activity diagram + swimlane 記法で、誰が責務を持つかを明示する。管制塔と sublane の境界を読むための図なので、細かい retry path はここに複製しない。

```plantuml
@startuml
|管制塔 Codex|
start
:管制塔が prompt / marker / ticket ID を受け取る;
:Redmine issue / journal / Version / catalog docs を読む;
:作業形状を分類する;

if (coordinator-owned 仕様決定が必要?) then (yes)
  :管制塔が仕様決定を Redmine / cataloged doc に記録;
endif

if (実装型?) then (yes)
  :blocking queue を drain;
  :sublane admission を判定;
  if (dispatch 可能?) then (yes)
    :dispatch decision を Redmine に記録;
    :target-lane Codex gateway へ handoff;
    |target-lane Codex|
    :target-lane Codex が same-lane Claude へ handoff;
    |sublane Claude|
    :sublane Claude が実装;
    :Implementation Done / Review Request を記録;
    |target-lane Codex|
    :target-lane Codex が coordinator へ callback;
    |管制塔 Codex|
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
  :commit-bearing work の integration disposition を記録;
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

## sublane retirement

管制塔は、US close 後に sublane retirement を必ず検討する。retirement は後続提案より前に行う。

routine retirement の条件:

- issue が close 済み、または scope が explicit defer 済み。
- commit-bearing work が target branch に統合済み、または patch-equivalent / explicit deferral が durable record にある。
- worktree が clean、または残 diff が disposable local runtime state と判定済み。
- active review / owner_waiting / blocked / callback_due が無い。
- lane identity が明確で、削除対象 worktree を取り違えない。

条件を満たす場合、管制塔は owner 確認なしに退役してよい。条件を外れる場合は Redmine に理由を残し、retirement を止める。

## 後続 Version / US 提案の順序

1. active sublane の callback / review / owner / integration / close / retirement を drain する。
2. 管制塔自身の guardrail 更新要否を評価する。必要なら repo-local autonomous lane として更新し、Redmine に記録する。
3. 後続 Version の目的、scope、非目標、due date を提案する。
4. owner 承認後に Version を作成する。
5. Version ごとの UserStory を作成し、親 Feature と relation を設定する。
6. 実装前に必要な coordinator-owned 仕様決定を Redmine / cataloged doc に残す。
7. 新規セッション prompt 例を、開始すべき issue ID と durable anchor とともに提示する。

## 失敗として扱う例

- 実装型 work を理由なく main lane Claude へ直接渡す。
- invisible worker / hidden subagent を sublane として扱う。
- sublane が coordinator-owned 仕様決定を実装 commit 内で黙って確定する。
- review approval と owner close approval を同じ journal に混ぜる。
- commit-bearing work を integration disposition なしに close する。
- close 済み lane を退役検討せず、新規 sublane を増やす。
- Version close を owner approval なしに進める。
- 後続 Version / US を作った後に、前提となる仕様決定を未記録のまま実装へ流す。

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
