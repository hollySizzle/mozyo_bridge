# Cockpit サブレーン運用モデル

Redmine #11850 由来の旧運用モデル文書。現在の一次正本は [[logic-coordinator-sublane-development-flow]] である。

この文書は互換 pointer として残す。サブレーンの actor、identity / routing / display / governance の分離、main lane Claude 境界、callback、owner approval aggregation、stall sweep、retirement drain は [[logic-coordinator-sublane-development-flow]] に統合済みである。

## 扱い

- 新規判断は [[logic-coordinator-sublane-development-flow]] に書く。
- 本文をこの文書へ再追加して、同じ workflow を別の完全な規約本文として復活させない。
- Redmine #11850 以降の観測履歴を確認したい場合は、このファイルの git history を読む。

## 検証

- `PYTHONPATH=src python3 -m mozyo_bridge docs resolve vibes/docs/logics/cockpit-sublane-operating-model.md --repo . --format text`
- `PYTHONPATH=src python3 -m mozyo_bridge docs validate --repo .`
