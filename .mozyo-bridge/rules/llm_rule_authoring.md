# LLM 規約文書作成規約

LLM agent が規約を読み、行動へ変換するための rule authoring 正本。`mozyo-bridge` の `redmine-governed` preset から配布される。

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
PlantUML風DSL:
  用途: gate、分岐、停止条件、agent実行契約
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
