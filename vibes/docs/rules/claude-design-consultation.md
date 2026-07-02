# Claude Design Consultation 発火条件 (project-local 採用記録)

Redmine #11702 / #11703。Codex / owner の設計議論に対し、実装担当 Claude へ **Design Consultation** を投げる運用の project-local 採用記録。base の Redmine gate lifecycle (`Design Consultation` / `Design Consultation Answer` gate) を再定義せず、新しい gate 名・transport kind を作らない。

## 正本 (pointer)

発火 / 非発火の条件セット (6 発火 / 5 非発火)、「後戻りコスト × 実装者反証の有益性」の判定軸、相談 payload に明示する要素、発火済み consultation の Review / Close 照合義務の正本は、#13029 により配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `## Design Consultation 発火判断` にある。本 doc は再掲しない (#13029 で pointer 化)。gate 自体の意味・順序・必須 field は central preset を読む。

## 本 repo 固有の適用

- 実装者は Claude、設計議論の起点は Codex / owner (`vibes/docs/rules/agent-workflow.md` `## Claude / Codex Role Boundary` の採用どおり)。回答は Redmine `Design Consultation Answer` journal として返す (日本語で可)。pane 通知は pointer のみ。
- 配布側の発火条件 2 (責務境界が動く) の本 repo での具体面: runtime tmux / SQLite / home registry / Redmine / OTel のいずれかの責務再配置。条件 3 (事故が高コストな領域) の具体面: 送信先境界、loopback、token、cross-session、pane target 解決。

### handoff コマンド形 (参考)

```
mozyo-bridge handoff send --to claude --source redmine --issue <id> --journal <consultation_journal_id> \
  --kind design_consultation --target <claude_pane> --target-repo <repo_root> --mode queue-enter \
  --summary '設計相談: <topic>。実装はまだ不要。#<id> journal #<jid> を読み、反証・懸念・推奨モデル・最小Task分割案を Design Consultation Answer として返してください。'
```

`--kind design_consultation` は base の transport kind をそのまま使う。新 kind を作らない。

## 禁止 / scope 外

- `AGENTS.md` / `CLAUDE.md` に本文を増やさない (router は薄く保つ)。本 rule は `vibes/docs/rules/` に置き、router からは catalog 経由で解決する。
- central preset (`.mozyo-bridge/rules/presets/**`) を本 rule で変更しない。preset へ昇格させたい場合は別途 preset 変更 task を立てる。
- Design Consultation を全 issue で必須化しない。
- Codex direct edit で本運用を進めない。設計 doc も Claude 実装 → Codex review の標準フローに乗せる (本 doc 自身がその例)。

## 参照

- 配布正本: `skills/mozyo-bridge-agent/references/workflow.md` `## Design Consultation 発火判断`。
- base: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md` の `Redmine Gate Lifecycle` / `判断の routing` / `Direct Request Triage`。
- 実例: #11639 (#56299 Consultation / #56318 Claude Answer / #56330 Codex synthesis)、#11695 / #11698 の派生 US。
