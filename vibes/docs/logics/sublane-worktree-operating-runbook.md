# Sublane / Worktree Operating Runbook

Redmine #11929 由来の旧 runbook 文書。現在の一次正本は [[logic-coordinator-sublane-development-flow]] である。

この文書は互換 pointer として残す。work unit と branch / worktree / lane / pane の対応、dispatch、same-lane Claude routing、callback sweep、review / integration / retirement の時系列手順は [[logic-coordinator-sublane-development-flow]] に統合済みである。

## 扱い

- 新規手順は [[logic-coordinator-sublane-development-flow]] に書く。
- 本文をこの文書へ再追加して、同じ workflow を別の完全な runbook として復活させない。
- Redmine #11929 以降の旧 runbook 履歴を確認したい場合は、このファイルの git history を読む。

## 検証

- `PYTHONPATH=src python3 -m mozyo_bridge docs resolve vibes/docs/logics/sublane-worktree-operating-runbook.md --repo . --format text`
- `PYTHONPATH=src python3 -m mozyo_bridge docs validate --repo .`
