# サブレーン帯域 / admission policy

この文書は互換 pointer である。サブレーン帯域、admission、pipeline dispatch、Post-Dispatch Fill Loop、drain order、mozyo_bridge dogfood soft profile の実行正本は [[logic-coordinator-sublane-development-flow]] の `## 帯域 / admission / pipeline fill` に統合済み。

新しい規約本文はこの文書へ追加しない。管制塔 / サブレーン開発フローに関する判断は `vibes/docs/logics/coordinator-sublane-development-flow.md` を読み、worktree lifecycle の責務境界だけ [[logic-worktree-lifecycle-boundary]] を読む。

## 移管理由

#12351 で、複数 sublane を積極利用する規約が `coordinator-sublane-development-flow.md`、本 doc、`agent-workflow.md`、skill reference に分散し、agent が入口によって `Post-Dispatch Fill Loop` を見落とす risk が確認された。実行時に読むべき正本を減らすため、帯域判断はサブレーン開発フロー spine へ一本化する。

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/coordinator-sublane-development-flow.md vibes/docs/logics/sublane-bandwidth-policy.md --repo . --format text`
