# Worktree runbook — manual catalog registration note

このファイルは `mozyo-bridge scaffold apply <governed-preset> --with-worktree-runbook` の opt-in 配布物である。同じ option で配られる worktree runbook doc を、採用先 repo の docs catalog へ **operator が手動で** 登録するための手順 / snippet を置く。

> scaffold は operator 所有の `.mozyo-bridge/docs/catalog.yaml` 本体を **書き換えない**。ship するのは `.mozyo-bridge/docs/catalog.yaml.example` と本 note のみ。catalog 登録は operator が下記 snippet を自分の `catalog.yaml` へ貼って行う。自動登録はしない。

## 配布される docs

- `vibes/docs/logics/worktree-lifecycle-boundary.md`

この doc は distribution 元 repo の同名 doc と byte 単位で同期されている (drift は sync-check test が検出する)。本 note 自身は scaffold-only artifact であり catalog source-of-truth ではない。

> 旧 `vibes/docs/logics/sublane-worktree-operating-runbook.md` は distribution 元 repo の Redmine #12215 で正本へ統合のうえ物理削除された (規約本文は `coordinator-sublane-development-flow.md` へ統合)。よって本 option はこの runbook doc を配らない。

## 手動登録手順

1. 採用先 repo に `.mozyo-bridge/docs/catalog.yaml` が無い場合は、まず `.mozyo-bridge/docs/catalog.yaml.example` を複製して有効化する。
2. 下記 snippet の entry を、自分の `catalog.yaml` の `documents:` 配下へ追記する。`related_document_refs` のうち採用先 repo に存在しない id は、その repo の事情に合わせて削除 / 差し替える (本 snippet の refs は distribution 元 repo の id をそのまま写したもの)。
3. `mozyo-bridge docs validate --repo .` と関連 generator / coverage check を実行し、登録が clean であることを確認する。

## catalog snippet

```yaml
  - id: logic-worktree-lifecycle-boundary
    type: logic
    status: active
    canonical_path: vibes/docs/logics/worktree-lifecycle-boundary.md
    purpose: git worktree lifecycle (add/remove、branch/path 命名、削除 policy、N-lane 並列 policy) を core CLI に入れず skill / runbook / operator recipe で扱う責務境界と汎用 runbook の方針正本。core は identity / discovery / safety primitive に限定する。
    audit_role: worktree_lifecycle_boundary_logic
```

## 注意

- snippet は `related_document_refs` を持たない。distribution 元 repo にあった `logic-sublane-worktree-operating-runbook` / `logic-cockpit-sublane-operating-model` / `skill-workflow-reference` / `rule-public-private-boundary` / `spec-project-map` 等は採用先 repo に存在しない (一部は元 repo でも退役済み) ため、ここでは cross-ref を付けない。必要に応じて自分の repo の id を足す。
- 配布される doc 本文には `[[wikilink]]` 形式の他 doc 参照が残る。採用先に対応 doc が無い場合、それらは「将来 / 元 repo 由来の参照」として扱い、必須登録ではない。
- worktree lifecycle の core CLI command 化は non-goal。runbook は operator recipe であり、`git worktree add/remove` を mozyo-bridge core に持ち込まない。
