# Shared kernel freeze policy

Redmine #12640 (parent Feature #12533 `140_ソース配置管理` / Version #276 `OOP-first architecture and static typing`)。

`src/mozyo_bridge/shared/**` を **frozen kernel** として固定し、これ以上 utility dump として
増殖させないための設計正本。OOP-first architecture policy
(`object-oriented-architecture-policy.md`) の「新しい value / path / error / compatibility
concern を `Manager` / `Helper` / `Util` 的な共有 dump へ足さず、bounded context 側の object
boundary へ寄せる」方針を、`shared/` という具体 surface に対して positive かつ enforceable な
guardrail として接続する。

この文書は方針正本であり、source の一括移動手順書ではない。`shared/` を「動かさない理由」と
「増やさない規約」と「freeze を機械的に守らせる test guardrail」を定義する。

## 背景 — なぜ freeze するか

`src/mozyo_bridge/shared/` は現状 3 module しか持たない最下層 kernel である。

- `errors.py`: `die` / `warn` (stderr 出力 + `SystemExit`)。CLI presentation primitive。
- `paths.py`: repo root / home / queue / tmux conf 解決、`normalize_path_unicode`、
  `REPO_ROOT_MARKERS` / `READ_MARK_PREFIX` などの path identity primitive。
- `name_compat.py`: workspace-anchor / project-defaults rename 互換 (`CompatResolution` value
  object + `resolve_compat_path`)。

`shared` という名前は放置すると「どこにも置き場がない関数の捨て場」になりやすい。OOP-first policy
の anti-pattern「`Manager` / `Helper` / `Util` だけを増やし、責務を名前で曖昧にする」がそのまま
`shared/` の劣化形になる。よって本 US は **新規 `shared/` module 追加を既定で禁止** し、kernel を
現在の 3 module に freeze する。

## kernel 不変条件 (freeze の本質)

`shared/` が最下層 kernel であることの実体は、**他 bounded context へ upward import しない** という
一方向依存の不変条件である。`source-layout-bounded-context-migration.md` が `shared/` を一貫して
「移動しない fixed kernel (移動は循環リスクのみ増やす)」と分類してきた理由はここにある。

freeze の不変条件:

- `shared/**` の module は、bounded context package
  (`mozyo_bridge.e_*` / `mozyo_bridge.application` / `mozyo_bridge.core` / `mozyo_bridge.domain` /
  `mozyo_bridge.infrastructure` / `mozyo_bridge.scaffold` / `mozyo_bridge.docs_tools`) を import しない。
- `shared/**` が依存してよいのは標準ライブラリと、必要なら同じ `mozyo_bridge.shared.*` 内のみ。
- これにより、全 context が `shared` を安全に下層 dependency として共有でき、循環が発生しない。

この不変条件は `tests/integration/e_150_quality_architecture/test_shared_kernel_freeze.py` で
機械的に検証する (後述)。

## per-module 配置判断 (#12640)

本 US は `errors.py` / `paths.py` / `name_compat.py` の移動先または残置理由を決める。

| module | 現状 consumer | 判断 | 理由 |
| --- | --- | --- | --- |
| `errors.py` | 全 context 横断 (約 21 source: core/state, application, scaffold, e_110/e_130/e_150) | **kept kernel (residual)** | `die` / `warn` は全 context が使う CLI presentation primitive。単一 bounded context へ移すと、その context へ全 context が upward import する逆方向依存・循環を生む。foundational kernel に留める。 |
| `paths.py` | 全 context 横断 (約 28 source) | **kept kernel (residual)** | repo/home/path identity 解決は最下層 identity primitive で全 context が依存する。移動は `errors.py` と同じ循環リスクのみ増やす。foundational kernel に留める。 |
| `name_compat.py` | `core/state/workspace_registry.py` / `core/state/workspace_defaults.py` の 2 source のみ (+ 1 test) | **residual / candidate owner = `core/state`** | 唯一の consumer が `core/state` に閉じる workspace 識別 compat 概念であり、cross-cutting ではない。OOP-first の「compatibility concern を bounded context へ寄せる」に従えば候補配置先は `core/state/name_compat.py`。ただし #12493 / #12590 / #12622 が `name_compat` を `errors` / `paths` と並ぶ fixed kernel と ratify 済みで、`core/state` も "fixed surface held" と記録されている。worker 一存でこの ratified 分類を supersede せず、実移動は owner / design consultation の sign-off を要する residual として記録する。move する場合も `sys.modules` facade idiom で legacy import path を温存し、fallback 無し撤去は `fallback-retirement-ledger.md` 経由でのみ行う。 |

判断: 本 US の behavior-preserving scope では **3 module とも physical move を行わない**。`errors` /
`paths` は foundational kernel として確定残置、`name_compat` は候補配置先を記録した上で owner 承認待ちの
residual とする。freeze 不変条件と increment-禁止 guardrail を導入することが本 US の主成果である。

## guardrail (増殖を止める仕組み)

freeze を「doc に書いただけ」で終わらせず、機械的に守らせる。

1. **enforceable test**:
   `tests/integration/e_150_quality_architecture/test_shared_kernel_freeze.py` が次を検証する。
   - `src/mozyo_bridge/shared/` の `*.py` 集合が frozen set
     (`__init__.py` / `errors.py` / `name_compat.py` / `paths.py`) と完全一致する。新 module 追加で fail。
   - 各 `shared/**` module が bounded context package を import しない (kernel 不変条件)。
   新たな value/path/error/compat concern を `shared/` へ足そうとすると test が fail し、review 前に
   止まる。これが #12640 acceptance の「`shared` 増殖を止める guardrail relation」の実体である。
2. **catalog relation**: 本 doc は `logic-shared-kernel-freeze` として
   `.mozyo-bridge/docs/catalog.yaml` に登録し、`logic-object-oriented-architecture-policy` /
   `logic-source-layout-bounded-context-migration` と相互参照する。docs catalog の governed node と
   して freeze 方針を追跡可能にする。
3. **review gate hook**: `shared/` に新 module を足す変更は、本 freeze policy の例外として扱い、
   review gate で「なぜ bounded context / `core/state` へ置けないか」を durable record に説明させる。
   既定は拒否、例外は明示記録。

## 新しい concern の置き場 (規約)

`shared/` へ足さない。代わりに:

- 単一 bounded context に閉じる concern → その context package
  (`mozyo_bridge.e_<order>_<slug>/f_<order>_<slug>/<layer>/`) の `domain` / `application` /
  `infrastructure` layer へ置く。
- managed-state / workspace identity に紐づく state-scoped compat / value → `core/state/`。
- 真に全 context 横断で外部副作用を持たない最下層 primitive のみが kernel 候補になりうるが、その場合も
  本 freeze の例外として review gate で正当化する。kernel への追加は既定で禁止。

## 関連正本

- `object-oriented-architecture-policy.md` (#12633 / Version #276): OOP-first / value object /
  port-adapter の責務境界。本 freeze はその「共有 dump を増やさない」方針の具体接続。
- `source-layout-bounded-context-migration.md` (#12492 ほか): `shared/` を fixed kernel として
  「移動しない」と分類してきた layout 移行計画正本。本 doc はその分類に positive freeze guardrail と
  per-module residual rationale を追加する。
- `fallback-retirement-ledger.md`: legacy import path facade の撤去台帳。`name_compat` を将来 move
  する場合の facade retirement はこの台帳経由で行う。
- `bounded-context-map.md`: bounded context 定義。`core/state` 等の配置先判断の上流。
