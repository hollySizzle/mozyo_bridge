# LLM 規約文書作成規約

LLM agent が規約を読み、行動へ変換するための rule authoring 正本。`mozyo-bridge` の `redmine-rails-governed` preset から配布される。

## 正本性

```yaml
対象: LLMが読む規約、gate、workflow、tool-specific入口、skill入口
正本配置: .mozyo-bridge/rules/**
適用先:
  - AGENTS.md
  - CLAUDE.md
  - .mozyo-bridge/skills/**
  - .codex/skills/**
  - .claude/skills/**
  - .mozyo-bridge/docs/catalog.yaml
禁止:
  - 同じ内容の複数正本
  - 入口文書への詳細規約埋め込み
  - 未検証のコピー
```

## 基本原則

```yaml
custom_instruction:
  役割: 薄いルーター
  書くもの: 正本path, 最低限の停止条件, 初期読み込み順
  書かないもの: 詳細gate, 長い手順, 重複した規約本文
canonical_rule:
  役割: 判断の正本
  書くもの: 優先順位, 役割, 編集権限, gate, 完了条件, invalid marker
runbook:
  役割: 実運用の手順
  書くもの: コマンド, 通知方法, 失敗時の扱い
```

## 言語

```yaml
日本語運用:
  規約本文: 日本語
  gate名: 必要なら英語可
  理由: agent が規約の言語へ応答文体を寄せるため
英語のみ禁止対象: agent が日本語で報告・記録すべき repo の runtime 規約
```

project が英語運用 / multilingual 運用を採用する場合は、本 file 冒頭でその choice を明示し、agent への適用言語を統一する。

## 形式選択

```yaml
Markdown:
  用途: 正本全体、説明、短い見出し、コードブロック保持
YAML:
  用途: 優先順位、権限、必須項目、invalid marker
PlantUML activity + swimlane:
  用途: workflow、gate、分岐、停止条件、handoff、approval、actor責務、agent実行契約
PlantUML macro / function:
  用途: validation、禁止事項、durable record 出力を図の近くに圧縮する
Mermaid:
  用途: 人間にも図として見せる必要がある関係図
txt:
  用途: 構造より短文規約を優先する場合
```

## LLM 実行契約

```plantuml
@startuml llm_rule_authoring_contract
start
$対象読者を決める("LLM agent")
$正本scopeを1つに決める()
$入口文書を薄いrouterにする()
$詳細規約をcanonicalへ移す()
if ($同じ内容が複数pathにある()) then (yes)
  $正本以外を削除またはrouter化()
endif
if ($agentの行動を制御する()) then (yes)
  $自然文だけでなくgateを構造化()
  $必須項目を列挙()
  $invalid_markerを列挙()
endif
if ($判断に順序がある()) then (yes)
  $優先順位をYAMLで書く()
endif
if ($分岐または停止条件がある()) then (yes)
  $PlantUML風DSLで関数的に書く()
endif
$catalogまたはresolverへ接続()
$生成物を再生成()
$syncとvalidationを実行()
stop
@enduml
```

## Flow 型 guardrail authoring

workflow / gate / handoff / approval / close / retirement のように actor と順序が重要な guardrail は、原則として PlantUML activity diagram + swimlane 記法で書く。swimlane は actor ごとの責務境界であり、箇条書き checklist の代替ではなく、実行契約の本体である。

```yaml
使う条件:
  - 複数 actor が関与する
  - handoff / callback / approval / close / retirement がある
  - stop condition や branch がある
  - actor ごとの責務や禁足事項を誤読すると workflow が壊れる
使わない条件:
  - 単なる静的 schema
  - verification command list
  - journal field list
  - file placement / catalog registration の静的 checklist
```

PlantUML macro / function は少数 primitive に絞る。推奨 primitive は次の 3 つである。

```plantuml
!procedure $validate($rule)
:validate: $rule;
!endprocedure
!procedure $forbid($rule)
:forbid: $rule;
!endprocedure
!procedure $record($anchor)
:record: $anchor;
!endprocedure
```

swimlane activity を使う場合、Markdown の補足は次に限定する。

```yaml
markdownに残す:
  - 目的と非目標
  - 用語、alias、非同義語
  - actor authority (実行責務ではなく権限境界)
  - routing の静的判定条件
  - schema、必須 journal field、verification command などの静的 checklist
  - 参照正本と catalog / generated file の接続
  - 図へ入れると可読性が落ちる前提、例外、後続 issue
swimlaneへ寄せる:
  - 誰が何をするか
  - 実行順序
  - handoff / callback / approval / close / retirement
  - stop condition
  - actor ごとの validation / forbid / record
markdownに重複させない:
  - swimlane にある実行責務の再掲
  - `$validate` / `$forbid` にある禁足事項の長い箇条書き
  - retry path や command detail の過剰展開 (runbook へ逃がす)
```

flow 型 guardrail は次の section を持つ。

```yaml
必須:
  - 目的
  - 用語と表記ゆれ
  - actor authority
  - routing / stop の静的判定条件
  - PlantUML activity + swimlane
  - 参照正本
  - 検証
禁止:
  - swimlane と同じ実行責務を Markdown に再掲する
  - 複数文書に同じ判断材料を重複させる
  - macro / function を増やしすぎて図だけで読めなくする
```

## Gate 記述規約

```yaml
gate:
  必須: [actor, 有効条件, 必須入力, 許可範囲, 失敗時の停止動作]
  推奨: [invalid_marker, 記録先, 通知先, 検証コマンド]
  禁止:
    - "「適宜」「必要に応じて」だけの判断委譲"
    - owner不明
    - path不明
```

## 条件駆動 guardrail 設計

抽象的注意喚起 (`適宜` / `必要に応じて` / `太ったら`) だけの guardrail は agent 間で判断が揺れる。guardrail は観測可能な trigger と durable-record 出力を持つ条件駆動構造で書く。条件は agent の判断を奪う checklist ではなく、判断を durable record 化させる trigger である。

```yaml
三層モデル:
  hard_gate:
    意味: 原則 stop。分解 / owner 判断 / design decision なしに先へ進めない条件
    出力: 停止 + 該当条件 + escalation 先を durable record に残す
  soft_trigger:
    意味: checkpoint journal を開き、分解か継続かを理由付きで判断する条件
    出力: checkpoint journal (該当trigger / 判断 / 理由 / 次アクション)
  judgment_override:
    意味: 条件に該当しても単一issue継続で進めてよいが、理由を残す条件
    出力: override理由を replayable に durable record へ記録
共通必須:
  - 観測可能な trigger (主観語だけで条件を定義しない)
  - durable-record 出力 (journal / gate)
禁止:
  - trigger も出力もない「適宜」「必要に応じて」「太ったら」だけの委譲
  - 判断記録を伴わない機械的強制だけの条件
```

### 例: Scope Decomposition Checkpoint

```yaml
分類: soft_trigger
trigger: 1 issue が次の複数を併せ持つ
  - product direction / 設計判断
  - implementation
  - diagnostics / 調査
  - tests
  - docs
  - 独立した複数の受け入れ条件
checkpoint: 実装継続前に checkpoint journal を開く
判断: 親US + 子issueへ分解 / 単一issue継続 を理由付きで選ぶ
override: 単一issue継続なら override理由を残す (judgment_override)
記録先: 対象issueのRedmine journal
```

## 正本分離

```yaml
1ファイル1責務:
  agent_workflow: agent 実行契約、役割、編集権限、引き継ぎ、完了条件
  llm_rule_authoring: LLM向け規約文書の作り方
  docs_catalog_governance: catalog、resolver、generated file の統治
分割禁止:
  - 同じ判断材料をagent別、部署別、画面別に分ける
  - 正本とコピーの差分を人間の記憶で管理する
```

## サイズ

```yaml
目安: 200行未満
圧縮方法:
  - 詳細手順はrunbookへ逃がす
  - 例外を列挙しすぎず invalid marker で止める
  - 関係説明は文章より構造体を優先する
  - 入口文書へ再掲しない
```

## 検証

```yaml
必須:
  - mozyo-bridge docs validate
  - mozyo-bridge docs generate-file-conventions --check
  - mozyo-bridge docs audit-impact --all-changed --check-generated
  - git diff --check
旧参照確認:
  command: rg --hidden -n "<old_path_or_old_id>" .
```
