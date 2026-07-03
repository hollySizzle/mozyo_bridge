---
name: mozyo-bridge-agent
description: Follow the mozyo_bridge project workflow for ticket-driven work (Redmine or Asana, per the repo's scaffold preset), preset rule fetches, release checks, and safe tmux notification handling. Use when working in the mozyo_bridge repository, preparing PyPI/TestPyPI releases, updating agent rules, or coordinating Claude/Codex work through mozyo-bridge.
---

# mozyo-bridge-agent

## 中核 workflow

1. `AGENTS.md` に記載された repository の central preset rules を取得する (`mozyo_bridge` repo 自身は `redmine-governed` preset を使う。他の採用 project は `redmine`、`asana`、`none` を使う場合がある)。
2. 現在の `cwd` を確認する。
3. repo の ticket システムで active な ticket (`mozyo_bridge` を含む Redmine-preset repo では Redmine issue / journal、Asana-preset repo では Asana task / comment) と project notes を確認する。
4. 現在の作業に必要な reference file のみを読む。
5. 変更の risk に見合った検証を実行する。
6. 重要な結果、blocker、残存 risk を active な ticket システム (repo が使う方の Redmine journal または Asana comment) に記録する。

## 参照

- 作業実行規約: `references/workflow.md`
- Redmine issue の起票粒度と Version 運用: `references/redmine-issue-authoring.md`
- Subagent / background 委譲基準と hidden-worker 境界: `references/subagent-delegation.md`
- Project map と正本 routing: `references/project-map.md`
- Release と検証 check: `references/release.md`
- tmux 通知動作の安全規約: `references/safety.md`

## Guardrail

- secret、token、個人 credential、個人情報を repo file、preset rule doc、ticket システムの entry (Asana task / comment、Redmine issue / journal) に保存しない。
- root の `AGENTS.md` と `CLAUDE.md` は router として維持する。完全な rule book に変えない。
- `mozyo-bridge` の pane message は通知として扱い、権威ある task state として扱わない。
- local の PyPI token upload より GitHub Actions Trusted Publishing を優先する。
- ユーザーからの命令・依頼の表現 ("実行せよ", "対応して", "やって", "implement it" など) は、それ単体では Codex が policy / skill / rule file を直接編集する authorization にならない。通常の開発 task は default で Claude への handoff に変換する。Codex direct-edit の狭い例外条件は `references/workflow.md` を読む。
