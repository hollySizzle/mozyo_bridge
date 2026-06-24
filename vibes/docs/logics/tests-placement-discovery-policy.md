# Tests Placement / Discovery Policy

Redmine #12489 (parent Feature `110_テスト構造管理` #12530)。RSpec 的な test type
分類と #12488 の bounded context 分類を組み合わせ、`mozyo_bridge` の tests 配置規約と
`unittest` discovery / CI 方針を固定する設計正本。

本 doc は **方針の正本**であり、既存テストの物理移動は行わない。フラット
`tests/*.py` から本 layout への移行は #12490 が所有する。source layout 側の
bounded context 正規化は #12492 / #12493 が所有する。

## 結論

```text
test type axis     : unit / integration / scenarios / regressions / support
bounded context axis: #12488 catalog から導く ASCII snake_case トークン
combine            : type-first ディレクトリ。unit / integration のみ context で細分する。
discovery authority : python -m unittest discover -s tests -v (CI 不変)
migration contract : #12490 が tests/ と各サブディレクトリに __init__.py を追加する。
```

新規テストの配置は `## 配置決定木` の上から順に一意に決まる。型の境界が曖昧な
ときは決定木の早い分岐が勝つ (support > scenarios > regressions > unit >
integration)。

## test type 分類 (RSpec 寄せ)

各 type の責務を一意に定義する。`unittest.TestCase` ベースであることは全 type 共通。

### unit

- 対象: 単一の src module / pure function / class を **隔離**して検証する。
- 協働者: subject-under-test 以外の collaborator は fake / stub / 注入 seam で置く。
  実 network / 実 tmux / 実 owner / 実 Redmine には触れない。
- 速度: 最速。I/O・sleep・実 subprocess を持たない。
- 例 (現行 flat から): `test_attention_state.py`, `test_pane_resolver`-系の
  hermetic 部分, `test_module_health.py`。

### integration

- 対象: **複数の実 collaborator** を結線したときの振る舞いを検証する。ただし依然
  hermetic (temp dir / in-memory DB / fake tmux client) に閉じる。
- unit との境界: 実 collaborator が 1 つ (残りは fake) なら unit、実 collaborator
  が複数で配線そのものを見るなら integration。
- 実 network / 実 owner / 実 push / 実 publish には触れない。それらは scenario か
  smoke (`smoke/**`, 本 tests/ 外) の領域。
- 例: state_store の ATTACH/migration 一連、docs catalog resolver と generated
  file の整合、handoff record の end-to-end 生成 (transport は fake)。

### scenarios

- 対象: 複数 module / 複数 bounded context をまたぐ **acceptance / workflow** の
  end-to-end 受入。operator / coordinator 視点の「通しで動く」を主張する。
- cross-cutting なので **bounded context で細分しない**。context は filename /
  docstring に書く。
- 例: turnkey e2e acceptance (`logic-turnkey-e2e-acceptance`)、cross-project
  cockpit smoke の hermetic 版、3 層 window/lane acceptance の自動化部分
  (#12497-#12500 系)。
- 破壊的・実 host を要する acceptance は本 tests/ ではなく `smoke/**` に置く。
  scenarios は CI で hermetic に回せるものに限る。

### regressions

- 対象: 過去に確定した defect の **再発防止 pin**。1 ファイル = 1 つの修正済み
  症状 / Redmine issue に対応する characterization。
- cross-cutting なので **bounded context で細分しない**。
- 命名: `test_issue_<id>_*.py` または `test_<症状>_regression.py`。docstring に
  Redmine issue / 原因 commit を残す。
- 新規機能の通常テストは regressions に置かない。あくまで「直したバグが戻らない」
  ことの番人。

### support

- 対象: テストではない **共有 fixture / helper / builder / fake**。
- `test_*.py` 命名を**使わない** (discover に拾わせない)。package 化のため
  `__init__.py` は持つ。
- 例: 共通の fake tmux client、record builder、temp workspace factory。
- private path / secret-shaped literal / personal home を置かない
  (`rule-public-private-boundary`)。abstract placeholder のみ使う。

## bounded context 軸 (#12488 連携)

bounded context の正本カタログは #12488 (Redmine Epic/Feature catalog,
`110_...` 表示名) と、それを repo の ASCII snake_case directory 名へ正規化する
対応表である。tests layout はこの対応表を **再利用**し、Redmine 階層を焼き込まない。

bounded context の **frozen な canonical ASCII トークン**は #12488 の対応表
`vibes/docs/specs/bounded-context-map.md` (`## repo bounded context 定義` /
`## 対応表`) を単一正本とする。tests / source はこのトークンを共有する:

| Redmine Epic (#12488) | tests/source bounded context (ASCII canonical) |
|---|---|
| `110_実行基盤・Routing` (#12501) | `execution_platform` |
| `120_運用Cockpit・表示` (#12502) | `operations_cockpit` |
| `130_統治・Scaffold配布` (#12503) | `governance_distribution` |
| `140_Adapter・Provider基盤` (#12504) | `adapter_provider` |
| `150_品質・アーキテクチャ統治` (#12505) | `quality_architecture` |
| `160_外部AgentUI連携` (#12506) | `external_agent_ui` |

> 正本: 上表のトークンは `bounded-context-map.md` の snapshot であり、naming の
> 一次正本は同 doc + Redmine catalog (#12488)。`external_agent_ui` は
> `experimental/vscode-agent-pane/` の PoC で `src/mozyo_bridge/` runtime tests を
> 持たないため、現状 tests サブディレクトリは作らない (該当テストが現れた時点で
> 追加)。
>
> 履歴: 本 doc 初版は provisional な working-set トークン (`routing` / `cockpit` /
> `governance` / `adapter` / `quality` / `agent_ui`) を仮置きしていたが、coordinator
> decision (Redmine #12490 j#64403, Option A) で #12488 canonical トークンへ統一し、
> short token は **superseded** とした。tests と source は `bounded-context-map.md`
> の canonical トークンを共有する。

組み合わせ方:

- **unit / integration** は context で細分する: `tests/unit/<context>/`,
  `tests/integration/<context>/`。`<context>` は subject-under-test の primary src
  module が属する bounded context (上表)。
- **scenarios / regressions** は cross-cutting のため context で細分しない。
- **support** は context で細分しない (横断 helper)。context 固有 helper が必要に
  なったら `tests/support/<context>/` を後から足してよいが、初期は flat。

## 目標 directory layout (To-Be / #12490 が実体化済み)

context トークンは #12488 canonical (`execution_platform` / `operations_cockpit` /
`governance_distribution` / `adapter_provider` / `quality_architecture` /
`external_agent_ui`)。

```text
tests/
  __init__.py                  # src/ を sys.path へ bootstrap (discovery 順非依存)
  unit/
    __init__.py
    execution_platform/__init__.py     test_*.py
    operations_cockpit/__init__.py     test_*.py
    governance_distribution/__init__.py test_*.py
    adapter_provider/__init__.py       test_*.py
    quality_architecture/__init__.py   test_*.py
  integration/
    __init__.py
    execution_platform/__init__.py     test_*.py
    operations_cockpit/__init__.py     test_*.py
    governance_distribution/__init__.py test_*.py
    adapter_provider/__init__.py       test_*.py
    quality_architecture/__init__.py   test_*.py
  scenarios/
    __init__.py                test_*.py
  regressions/
    __init__.py                test_issue_<id>_*.py
  support/
    __init__.py                <helpers, not test_*.py>
```

存在しない context サブディレクトリは作らない (空 package を量産しない)。該当
テストが現れた時点で追加する。#12490 初回移行では `scenarios` / `regressions` /
`support` と `external_agent_ui` に該当ファイルが無かったため、それらのディレクトリは
作成していない。

> 移動後の `ROOT` 解決: フラット時代の `Path(__file__).resolve().parents[1]` は
> `tests/<type>/<context>/` への移動で 2 階層深くなるため `parents[3]` に更新する。
> import 順非依存の `src/` bootstrap は `tests/__init__.py` に集約する。

## discovery / CI 方針

### 正本コマンド (不変)

CI と開発の discovery 正本は次の 1 コマンドであり、移行後も**文字列を変えない**:

```text
python -m unittest discover -s tests -v
```

(`.github/workflows/test.yml` の "Run unit tests" step。)

### nested discovery の必須条件 (検証済み)

`unittest discover` は default pattern `test*.py` でサブディレクトリへ再帰するが、
**サブディレクトリが import 可能な package である**ことを要求する。実測:

- `tests/` 配下に `__init__.py` が無い現行 flat 構造では、`tests/<sub>/test_*.py`
  は **silently 未 discover** になる (top-level の `tests/test_*.py` だけが走る)。
- `tests/` と各サブディレクトリに `__init__.py` を置くと、nested test は
  `<sub>.test_foo` として discover され、コマンドは不変のまま全件走る。

したがって #12490 の移行契約は厳格である:

1. `tests/__init__.py` を追加する。
2. `unit` / `integration` / `scenarios` / `regressions` / `support` と、その下の
   各 `<context>` サブディレクトリすべてに `__init__.py` を置く。
3. `__init__.py` を入れ忘れた階層のテストは **false green** (0 件 discover でも
   exit 0) になる。移行 PR は移行前後で **collected test 数が一致**することを
   検証する (例: 移行前の総数を記録し、`discover` の `Ran N tests` を突き合わせる)。

### module 名の一意性

- 現行 flat (top_level_dir = `tests`, package 無し) では module basename が
  **全 tests でグローバル一意**である必要がある。
- package 化後は module が `<sub>.<context>.test_foo` で namespace されるため、
  別 context 間の basename 重複は許される。とはいえ basename は subject を表す
  descriptive な名前を維持する。

### pytest の位置づけ

`pyproject.toml [tool.pytest.ini_options] pythonpath = ["src"]` により `pytest` は
開発 convenience として使えるが、**CI gate の authority ではない**。gate は上記
`unittest discover`。package 化後 `pytest` を併用する場合は import-mode の差異
(同名 module の衝突解決) に注意し、CI の判断は `unittest discover` に従う。

## 配置決定木 (新規テストの一意な配置)

新規テストファイルを書くとき、上から順に最初に該当した分岐で配置を確定する:

1. **テストではない共有 helper / fixture / fake か?** → `tests/support/`
   (`test_` prefix を付けない)。終了。
2. **複数 module / 複数 context をまたぐ通し受入 (workflow / acceptance) か?** →
   `tests/scenarios/`。終了。
3. **修正済み defect の再発防止 pin か?** → `tests/regressions/`
   (`test_issue_<id>_*.py`)。終了。
4. **単一 unit を隔離検証するか (collaborator は fake)?** →
   `tests/unit/<context>/`。`<context>` = subject の primary src module の
   bounded context。
5. **それ以外 (複数の実 collaborator を hermetic に結線)** →
   `tests/integration/<context>/`。

一意性の tie-breaker:

- unit / integration が複数 context に触れる場合、配置は **primary
  subject-under-test** (振る舞いを characterize している側) の context に従う。
  真に context 横断の受入なら integration ではなく scenario (分岐 2) に倒す。
- unit vs integration は **実 collaborator の数**で決める (1 = unit、複数 =
  integration)。
- 破壊的 / 実 host / 実 network を要する受入は本 tests/ ではなく `smoke/**`。

## #12490 migration contract (実施結果)

本 doc が固定し、#12490 が実装した (Redmine #12490 j#64403 coordinator Option A):

- フラット 91 ファイルのうち 87 を決定木 + #12488 canonical context map に従って
  `tests/<type>/<context>/` へ移動した。`__init__.py` package 化を行い、`discover`
  コマンド不変・collected 数一致 (移行前後ともに `Ran 2100 (skipped=2)`) を検証した。
- `.mozyo-bridge/docs/catalog.yaml` の `fc-cockpit-grouped-projection-source` /
  `fc-presentation-state-db-source` / `fc-state-store-source` が列挙していた個別
  test path を移動後 path へ追随させ、`generate-file-conventions --check` を緑に保った。
- 残 4 ファイル (`test_repo_local_config.py` / `test_repo_local_config_loader.py` /
  `test_cli_repo_local_config_wiring.py` / `test_runtime_config_instruction.py`) は
  subject (`repo_local_config` / runtime-config instruction) が #12488 map の 6
  context のどれにも一意に割り当たらないため、**flat 直下に残す documented exception**
  とした。理由: 複数 context (governance_distribution / quality_architecture /
  adapter_provider) に等しく妥当で、guess を避け fail-closed する。期限・解消条件:
  #12488 map が repo-local config の context を定義した時点、または owner 判断で
  context を確定した時点で移動する (Feature #12533 source 配置と同期)。
- module-health gate / CI / docs full discovery の最終監査は #12494 が所有する。

## Anti-patterns

- Redmine の Epic/Feature 階層を tests directory にそのまま焼き込む (対応表で結ぶ)。
- type 軸と context 軸を二重 top-level にして配置を多義にする (type-first に固定)。
- `discover` のコマンド文字列を移行のために書き換える (不変が契約)。
- サブディレクトリの `__init__.py` を省いて nested test を false green にする。
- scenarios / regressions を context で細分し、横断テストの置き場を曖昧にする。
- support に `test_*.py` を置いて helper を test として走らせる。
- private path / secret-shaped literal を support / fixtures に書く
  (`rule-public-private-boundary`)。
- 実 network / 実 owner / 実 publish を unit / integration に持ち込む (smoke へ)。

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --repo . --check`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
