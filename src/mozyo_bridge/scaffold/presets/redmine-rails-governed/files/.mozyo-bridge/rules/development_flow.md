# mozyo-bridge 開発フロー契約

`<target repo root>` で LLM agent が gate、役割、編集可否、引き継ぎ、完了条件を判定するための正本。`mozyo-bridge` の `redmine-rails-governed` preset から配布される。本 file は **target repo 側の正本**ではあるが、初期版は scaffold preset 側にある。変更は preset 側へ upstream し、`mozyo-bridge scaffold apply --backup` で再配布する。

## 正本性

```yaml
優先順位:
  - Redmine issue と Redmine journal
  - この文書
  - .mozyo-bridge/rules/docs_catalog_governance.yaml
  - .mozyo-bridge/docs/catalog.yaml
  - AGENTS.md / CLAUDE.md / local skill / runbook
chat_pane通知: 正本ではなく通知のみ
チケットシステム: Redmine
通知手段: mozyo-bridge
```

## 既定役割

```yaml
役割:
  claude_code: 実装者
  codex: 監査者
  owner: 最終判断者
実装者の責務: [code, schema, tests, 実装隣接docs]
監査者の責務: [review, 設計相談回答, 規約解釈, Redmine判断記録]
```

project が実装者 / 監査者 split を採用していない場合は、上記を採用しないでよい。ただし採用したら、本 file の境界を曖昧にしない。

## パス別編集権限

```yaml
実装ファイル:
  patterns:
    - app/**
    - spec/**
    - test/**
    - config/**
    - db/**
    - lib/**
  既定編集者: claude_code
  codex編集条件: codex_direct_edit gate が有効
ガードレール:
  patterns:
    - AGENTS.md
    - CLAUDE.md
    - .mozyo-bridge/rules/**
    - .mozyo-bridge/docs/catalog.yaml
    - .codex/skills/**
    - .claude/skills/**
  codex編集条件: ユーザーがガードレール変更を明示
ガードレール変更で触らないもの:
  - app/**
  - spec/**
  - test/**
  - config/**
  - db/**
  - lib/**
  - その他 project が実装ファイルと定義した path
```

実装ファイルのパターンは project に合わせて調整してよい。だが、ガードレール変更 issue で実装ファイルを併せて触らないという原則は維持する。

## Gate schema

```yaml
start:
  必須: [issue, parent_issue, 目的, 受け入れ条件, 参照docs, 未確認事項]
implementation_done:
  actor: 実装者
  必須: [変更ファイル, 実装意図, 前提, 未確認事項, 検証結果, docs更新, commit_or_diff]
review_request:
  actor: 実装者
  必須:
    - implementation_done_journal
    - commit_or_diff
    - 変更ファイル
    - review観点
    - 未確認事項
    - 受信agent
    - 受領方法
review:
  actor: 監査者
  必須: [対象commit_or_diff, resolved_docs, 照合規約, 指摘事項, 未確認事項, 再review要否, 結論]
  指摘事項_分類:
    - 事実: コード・設定・docs で確認済みの不整合のみ
    - 仮説: 確認すべき事項と確認方法を併記
close:
  必須:
    - 受け入れ確認
    - 指摘対応
    - 残留リスク
    - review結果
    - owner_close_approval (Review Gate とは別 journal)
    - commit_hash_record
    - close判断
codex_direct_edit:
  actor: codex
  有効条件:
    必須: [role:実装者, direct_edit:true, allowed_paths, reason, follow_up_review]
    根拠: Redmine journal または owner 明示指示
  無効marker:
    - "着手:codex"
    - "実装完了:codex"
    - "担当:codex"
    - "codex対応"
  禁止_並行表現:
    - "実行せよ"
    - "対応して"
    - "やって"
    - "implement it"
    - "go ahead"
    - "お願いします"
    - "進めて"
    上記の短い命令だけでは codex_direct_edit gate は有効化しない
```

## LLM 実行契約

```plantuml
@startuml mozyo_bridge_agent_gate_contract
start
$作業root確認(target_repo_root)
$central_presetを読む()
$layered_preset(redmine, redmine-rails, redmine-rails-governed)を読む()
$この契約を読む()
$docs_catalogを読む()
$redmine_issueを読む()
$parent_issueを読む()
$現在journalを読む()
$現在gateを解決()
$agent役割を解決()
if ($入力がticket_idまたは短い実行指示()) then (yes)
  $編集権限を否定("短い指示はfile edit許可ではない")
endif
if ($対象pathが分かる()) then (yes)
  $resolve_audit_docsを実行()
  $解決docs本文を読む()
endif
if ($agentがcodex()) then (yes)
  if ($対象が実装ファイル()) then (yes)
    if ($gate有効("codex_direct_edit")) then (yes)
      $allowed_pathsだけ編集()
      $直接編集理由を記録()
    else (no)
      $経過記録("Codexに実装編集gateなし")
      $claude_codeへ引き継ぎ()
      stop
    endif
  endif
endif
if ($gate有効("review_request")) then (yes)
  $codex_reviewを実行()
  $review_gateを記録()
  $claude_codeへ通知()
else (no)
  if ($agentがcodex() && $依頼がreview()) then (yes)
    $監査不能を記録("review_request gate不足または不正")
    $claude_codeへ通知()
    stop
  endif
endif
if ($agentがclaude_code() && $役割が実装者()) then (yes)
  $scopeを守って実装()
  $implementation_doneを記録()
  $review_requestを記録()
  $codexへreview通知()
endif
if ($close要求()) then (yes)
  if ($review_gateあり() && $owner_close_approvalあり() && $commit_hash_recordあり()) then (yes)
    $close_gateを記録()
  else (no)
    $close_blockedを記録()
    stop
  endif
endif
stop
@enduml
```

## 禁止遷移

```yaml
禁止:
  - id: codex_implements_from_ticket_id_only
    条件: [agent:codex, input:ticket_id_or_short_instruction, codex_direct_edit_gate:missing]
    action: stopしてClaudeへ引き継ぐ
  - id: review_without_review_request
    条件: [agent:codex, review_request_gate:missing]
    action: 監査不能を記録
  - id: close_after_implementation_done_only
    条件: [implementation_done:present, review_gate:missing]
    action: close禁止
  - id: close_without_owner_approval
    条件: [review_gate:present, owner_close_approval:missing]
    action: close禁止
  - id: notify_without_redmine_gate
    条件: [handoff_or_review_notification:requested, journal_gate:missing]
    action: gate作成または作成依頼を先に行う
  - id: use_retired_transport
    条件: [transport: .agent_handoff/tasks.yaml or read-next --wait or Stop hook]
    action: 拒否してRedmine journalを使う
```

## Redmine journal template

```markdown
## Gate: codex_direct_edit
- role: 実装者
- direct_edit: true
- allowed_paths:
- reason:
- follow_up_review:

## Gate: review_request
- implementation_done_journal:
- commit_or_diff:
- changed_paths:
- review_focus:
- receiver: Codex
- receive_method: mozyo-bridge journal <id>

## Gate: review
- target_commit_or_diff:
- resolved_docs:
- 照合規約:
- 指摘事項 [事実]:
- 指摘事項 [仮説]:
- 未確認事項:
- 再review要否:
- 結論:

## Gate: close
- 受け入れ確認:
- 指摘対応:
- 残留リスク:
- review結果journal:
- owner_close_approval_journal:
- commit_hash:
- close判断:
```

## 完了条件

Implementation Done は完了ではない。Redmine に Review Gate、指摘対応、owner close approval journal (Review Gate とは別)、commit hash record、Close Gate が記録されるまで完了扱いしない。
