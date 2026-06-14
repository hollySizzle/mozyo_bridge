# Worktree runbook — manual catalog registration note

このファイルは `mozyo-bridge scaffold apply <governed-preset> --with-worktree-runbook` の opt-in 配布物である。同じ option で配られる 2 つの runbook docs を、採用先 repo の docs catalog へ **operator が手動で** 登録するための手順 / snippet を置く。

> scaffold は operator 所有の `.mozyo-bridge/docs/catalog.yaml` 本体を **書き換えない**。ship するのは `.mozyo-bridge/docs/catalog.yaml.example` と本 note のみ。catalog 登録は operator が下記 snippet を自分の `catalog.yaml` へ貼って行う。自動登録はしない。

## 配布される docs

- `vibes/docs/logics/worktree-lifecycle-boundary.md`
- `vibes/docs/logics/sublane-worktree-operating-runbook.md`

これらは distribution 元 repo の同名 doc と byte 単位で同期されている (drift は sync-check test が検出する)。本 note 自身は scaffold-only artifact であり catalog source-of-truth ではない。

## 手動登録手順

1. 採用先 repo に `.mozyo-bridge/docs/catalog.yaml` が無い場合は、まず `.mozyo-bridge/docs/catalog.yaml.example` を複製して有効化する。
2. 下記 snippet の 2 entry を、自分の `catalog.yaml` の `documents:` 配下へ追記する。`related_document_refs` のうち採用先 repo に存在しない id は、その repo の事情に合わせて削除 / 差し替える (本 snippet の refs は distribution 元 repo の id をそのまま写したもの)。
3. `mozyo-bridge docs validate --repo .` と関連 generator / coverage check を実行し、登録が clean であることを確認する。

## catalog snippet

```yaml
  - id: logic-worktree-lifecycle-boundary
    type: logic
    status: active
    canonical_path: vibes/docs/logics/worktree-lifecycle-boundary.md
    purpose: git worktree lifecycle (add/remove、branch/path 命名、削除 policy、N-lane 並列 policy) を core CLI に入れず skill / runbook / operator recipe で扱う責務境界と汎用 runbook の方針正本。core は identity / discovery / safety primitive に限定する。
    audit_role: worktree_lifecycle_boundary_logic
    related_document_refs:
      - logic-sublane-worktree-operating-runbook

  - id: logic-sublane-worktree-operating-runbook
    type: logic
    status: active
    canonical_path: vibes/docs/logics/sublane-worktree-operating-runbook.md
    purpose: sublane / worktree 運用を他プロジェクトで再現する時系列順 portable runbook (prereq / mapping / lane 作成 / gateway dispatch / sublane 実装境界 / coordinator callback・owner approval 集約 / review・merge・push・CI・retirement / known friction)。規約本文は既存正本へリンクする sequenced spine。
    audit_role: sublane_worktree_operating_runbook_logic
    related_document_refs:
      - logic-worktree-lifecycle-boundary
```

## 注意

- snippet の `related_document_refs` は最小限に絞ってある。distribution 元 repo にあった `logic-cockpit-sublane-operating-model` / `skill-workflow-reference` / `rule-public-private-boundary` / `spec-project-map` 等は採用先 repo に存在しないことが多いため、ここでは互いの 2 doc のみを相互参照させている。必要に応じて自分の repo の id を足す。
- 配布される runbook docs 本文には `[[wikilink]]` 形式の他 doc 参照が残る。採用先に対応 doc が無い場合、それらは「将来 / 元 repo 由来の参照」として扱い、必須登録ではない。
- worktree lifecycle の core CLI command 化は non-goal。runbook は operator recipe であり、`git worktree add/remove` を mozyo-bridge core に持ち込まない。
