# サブレーン開発フロー — runtime profile (opt-in)

このファイルは `mozyo-bridge scaffold apply <governed-preset> --with-sublane-flow` の opt-in 配布物である。サブレーン開発フロー (issue ごとに git worktree / lane / pane / branch / ticket gate を対応させる多 agent 並列開発) を、採用先 repo の **runtime-active な参照** として有効化したときの entrypoint をまとめる。

> このオプションは default-off である。`--with-sublane-flow` を渡さない plain な `scaffold apply` では、本 doc も、生成される `AGENTS.md` / `CLAUDE.md` の sublane read-route 節も配置されない。single-lane / 小粒 project は default のまま運用できる。

## このオプションが有効化するもの

- 生成される `AGENTS.md` / `CLAUDE.md` に「サブレーン開発フロー (opt-in profile)」という薄い read-route 節が追加される。router 本文には workflow 詳細を inline せず、この doc と配布済み skill workflow reference を指すだけにとどめる。
- 本 doc (`vibes/docs/profiles/sublane-flow-runtime-profile.md`) が配布される。

## 規律の正本 (どこを読むか)

サブレーン規律の portable な正本は、`mozyo-bridge-agent` skill に同梱される workflow reference である。adopter は次を読む:

- `skills/mozyo-bridge-agent/references/workflow.md`
  - `## Post-Dispatch Fill Loop` — pipeline-first dispatch / coordinator-blocking 語彙 / Drain Order
  - `## Sublane Coordinator Callback` — callback drain
  - `## Sublane Completion Guardrails` — downstream resume
  - `## Sublane Retirement Drain` — lane 退役
  - `## Owner Approval Aggregation` — owner 承認の集約点
  - `## Existing-Project Sublane Adoption` — 既存プロジェクトへこのフローを導入する adopter-facing 手順 (read-only preflight / child 分解 / dispatch / scaffold+rules+catalog adoption / verification / origin-reachable commit / callback recovery / close 順序)

既にあるプロジェクトへ governed scaffold と本フローを **導入する** 手順 (新規 bootstrap ではなく、既存 router / catalog / docs を壊さない adoption) は、上記 `## Existing-Project Sublane Adoption` を正本として読む。private な絶対 path・cockpit 構成・並列 lane 数は配布版に含まれないため、adopter は自身の private operating profile として別途定義する。

git worktree lifecycle (add/remove、branch/path 命名、削除 policy、N-lane 並列 policy の責務境界) を扱う companion doc は `--with-worktree-runbook` で配られる `vibes/docs/logics/worktree-lifecycle-boundary.md` である。両オプションは独立しており、worktree lifecycle の運用 recipe も runtime に置きたい場合は併せて opt-in する。

## profile に含めないもの (private operating policy)

次の operator 固有要素は portable profile に含まれない。adopter は自身の private operating profile として別途定義する:

- 並列 lane 数や上限
- cockpit / pane の構成・配置
- 絶対 path
- session / window の命名規約

## 手動 catalog 登録 (任意)

scaffold は operator 所有の `.mozyo-bridge/docs/catalog.yaml` 本体を **書き換えない**。本 doc を自分の docs catalog で管理したい場合は、`.mozyo-bridge/docs/catalog.yaml`(無ければ `.mozyo-bridge/docs/catalog.yaml.example` を複製して有効化) の `documents:` 配下へ手動で entry を追記し、`mozyo-bridge docs validate --repo .` で clean を確認する。自動登録はしない。
