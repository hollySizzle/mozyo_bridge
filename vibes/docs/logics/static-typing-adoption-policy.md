# Static Typing Staged Adoption

Redmine #12642 (parent Feature #12533 `140_ソース配置管理`, Version #276
`OOP-first architecture and static typing`)。OOP-first architecture を static
typing とセットで段階導入するための **tool / command / 最初の適用 boundary** の
設計正本。設計思想そのものは `vibes/docs/logics/object-oriented-architecture-policy.md`
(`## static typing policy`) を正本とし、本 doc はそれを「実際に走る gate」へ
落とし込む adoption 計画である。

#12633 / OOP-first policy の結論はこうである: class だけ増やして型を弱いまま
にすると、動的 duck typing の object 群になり保守性は上がらない。よって OOP-first
で追加する public object boundary (value object / result object / Protocol port /
use-case injection) は型検査可能にする。本 doc はその検査を最小・保守的な形で
**今** 1 つの bounded context に対して稼働させ、tranche ごとに広げる方針を固定する。

## 受け入れ条件との対応

| 受け入れ条件 (#12642 Done) | 実現 |
| --- | --- |
| static typing 導入方針と最初の適用 boundary が決まっている | tool = `mypy`、最初の boundary = `e_150_quality_architecture/f_150_ci_verification/domain` (下記)。 |
| selected tool と command が docs / verification に記録されている | 本 doc + `pyproject.toml` の `[tool.mypy]` + `[project.optional-dependencies] typecheck`。command は `python -m mypy`。 |
| 追加する型 gate が既存 workflow を不必要に壊さない | gate は default で **island だけ** を検査し、repo 全体 strict を要求しない。CI 強制は本 tranche では入れない (下記「CI を今は入れない理由」)。 |
| focused verification / docs validate / diff check green | `python -m mypy` green、対象 island の focused unittest green、`docs validate` / `git diff --check` green。 |

## ツール選定: `mypy`

`mypy` を採用する。`pyright` ではない。理由:

- **Python-native / 設定が pyproject 完結**: `[tool.mypy]` で全設定が pyproject.toml
  に入り、新しい設定 file を増やさない。`pyright` は node / npm 配布が前提で、
  本 repo に node toolchain 依存を持ち込む。本 repo は Python CLI package であり、
  dev 依存は Python 側に閉じたい。
- **opt-in dev 依存に閉じる**: `pip install mozyo-bridge[typecheck]` (= `mypy>=1.8`)
  でだけ入る optional-dependency にする。runtime 依存は増やさない。auto-install も
  しない。
- **段階導入と相性**: `files` で検査対象を island に限定し、`[[tool.mypy.overrides]]`
  で island だけ strict にできる。tranche ごとの ratchet を 1 file で表現できる。

`pyright` を将来採用する余地は残す (CI editor 補完など)。本 doc が固定するのは
「最初の gate は mypy」であって「pyright 永久禁止」ではない。

## コマンド

正本コマンド (repo root から):

```bash
python -m mypy
```

- 引数なしで `[tool.mypy].files` の island だけを検査する。
- 事前に `pip install mozyo-bridge[typecheck]` (または `pip install 'mypy>=1.8'`)。
- `python_version = "3.10"` (package の `requires-python = ">=3.10"` に合わせる)。
- `mypy_path = "src"` で src-layout の `mozyo_bridge.*` を解決する。

## 最初の適用 boundary

最初の typed island:

```
src/mozyo_bridge/e_150_quality_architecture/f_150_ci_verification/domain/
  ├─ test_impact.py    (#12752 module-to-test impact resolver)
  └─ test_runtime.py   (#12754 test runtime profiling / slow-test budget)
```

この boundary を最初に選ぶ理由:

- **pure domain / 副作用ほぼなし**: I/O は最小 (`list_test_files` / `load_budget`)
  で、型検査が外部境界の stub 不足に引っかからない。
- **value object / result object の集合**: `SourceTarget` / `TestImpact` /
  `ImpactPlan` / `RuntimeBudget` / `RuntimeSummary` など frozen dataclass の
  result/value object が中心で、OOP-first policy `## static typing policy` の
  優先順位 1 (value object と result object を型で固定する) にそのまま対応する。
- **workflow-control hot path ではない**: quality / CI-verification context で
  あり、handoff / lane / delegated coordinator の判断 hot path に触れない
  (#12642 implementation request の「workflow-control hot path を避ける」に従う)。
- **既に full annotation 済み**: 両 module は `from __future__ import annotations`
  と完全な signature を持ち、island だけ strict にしても green になる。

### 本 tranche で入れた behavior-preserving 型修正

`test_impact.py` の `_resolve_numbered` で `target.epic` が `str | None` のまま
`+` 連結されており、`numbered_source` target は構造上必ず epic を持つという不変条件
が型で表現されていなかった。gate を green にするため `assert target.epic is not None`
を関数頭に追加して不変条件を明示し、型を narrow した。runtime 挙動は不変
(正しい呼び出しでは assert は発火しない)。これは OOP-first policy が言う
「value/result object を型で固定する」修正の最小例である。

## gate 設定 (`[tool.mozyo].mypy` 相当 = `pyproject.toml` `[tool.mypy]`)

保守的な「最初の gate」の形:

- **検査対象を island に限定** (`files`): default の `python -m mypy` は island
  だけを見る。repo 全体は検査しない。
- **island の外へ追わない** (`follow_imports = "silent"`): typed island が untyped
  module を import しても、その先を strict 検査しない / error にしない。
- **stub 無し third-party を許容** (`ignore_missing_imports = true`): `yaml` など
  stub を持たない依存で gate を割らない。
- **island だけ strict** (`[[tool.mypy.overrides]]` で
  `disallow_untyped_defs` / `disallow_incomplete_defs` / `check_untyped_defs` /
  `warn_return_any`): full annotation を island に対してだけ要求する。
- **repo 全体 strict は要求しない**: `disallow_untyped_defs` 等を global には
  かけない。#12642 Non-Goals (初期段階で全 repo strict を要求しない /
  style-only lint flood を起こさない) に従う。

## 段階導入の優先順位 (OOP-first policy の写し)

`object-oriented-architecture-policy.md` `## static typing policy` の優先順位を
gate 拡張の順序として再掲する:

1. Value object と Result object を型で固定する。← **本 tranche で着手**
2. External port を `Protocol` で固定する。
3. Use case constructor injection を型で固定する。
4. CLI adapter の `argparse.Namespace` 依存を command input object へ寄せる。
5. `mypy` を bounded context 単位で段階的に適用する。← **本 tranche で着手**

## ratchet 計画 (tranche ごとの拡張手順)

新しい OOP-first tranche で公開 object boundary を型付けしたら:

1. その module / package の path を `[tool.mypy].files` に追加する。
2. 必要なら `[[tool.mypy.overrides]]` の `module` に dotted pattern を追加し、
   island ごとに strict 度を上げる。
3. `python -m mypy` を green にしてから commit する。green でない型エラーは
   behavior-preserving に修正するか、修正できない場合は対象 module を `files` に
   入れない (= まだ typed island に昇格させない)。
4. island が repo の主要 surface を覆ったら、global strict 化と CI gate 化を
   別 issue で検討する。

1 commit で repo 全体を型検査対象にしない。bounded context ごとに island を
増やす。

## CI を今は入れない理由 (本 tranche では安全に保留)

`test.yml` / `testpypi.yml` に mypy step を **追加しない**。理由:

- CI gate 変更は task-level review 例外 (guardrail / CI / packaging) に該当し、
  US-level audit とは別の owner 承認経路が要る。#12642 implementation request も
  「CI enforcement は scope 外、入れるなら安全な理由を記録」としている。
- typed island は今 1 つで、CI に入れる価値より flaky / 運用負荷のリスクが先に来る。
  island が広がってから CI gate 化する方が安全 (ratchet 計画 step 4)。
- gate は opt-in dev 依存 (`mozyo-bridge[typecheck]`) と local command で完結する。
  CI を変えずに local / pre-commit で回せる。

CI へ入れる判断は island が育ってから別 issue で行う。本 doc はその前段の
local / repo-config gate を正本化するに留める。

## verification

本 tranche の確認 (#12642 expected verification):

- `python -m mypy` → `Success: no issues found`。
- 対象 island の focused unittest (`tests.unit.e_150_quality_architecture.f_150_ci_verification.test_test_impact`
  / `...test_test_runtime`) green。
- `python3 -m mozyo_bridge docs validate --repo .` green。
- catalog を触ったため `docs validate --check-file-coverage` /
  `docs generate-file-conventions --check` / `docs audit-impact --all-changed --check-generated` green。
- `git diff --check` clean。

## 関連

- `vibes/docs/logics/object-oriented-architecture-policy.md` — 設計思想正本。
- `vibes/docs/logics/test-runtime-profiling-policy.md` /
  `vibes/docs/logics/tests-placement-discovery-policy.md` — 最初の island が属する
  CI-verification context の設計正本。
