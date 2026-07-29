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
- **上の `/` は OR である (literal)。** 「複数 module をまたぐ」か「複数 bounded
  context をまたぐ」かの**いずれか**を満たせば該当する。**単一 bounded context でも、
  複数 module をまたぐ operator / coordinator 視点の通し受入なら scenarios である。**
  根拠 [実測、base `dd62e957`、AST の import 解析]: `tests/scenarios/` 12 file が import
  する `mozyo_bridge.e_*` から bounded context を導出すると、複数 context をまたぐ 3 /
  単一 context のみ 6 / `e_*` context を持たない 3 であり、**12 file 中 9 file が複数
  bounded context をまたいでいない**。repo が実際に採ってきた読みは OR である
  (Redmine #14662 j#92449 裁定 3 / Review j#92458)。
- **surface (実 filesystem / 実 subprocess に触れるか) は該当条件ではない。** 本 type が
  課すのは後述の hermetic 要件だけであり、**実 FS に触れることは要求していない**
  (根拠は本節の定義そのもの)。したがって **pure な test も scenarios になり得る**。
  **surface で population を切ってから type を割り当てない。**
  > 本判断の根拠は上記の定義であり、corpus 集計ではない。参考値として既存 12 file のうち
  > **6 file は temp dir を作らない** [実測、base `dd62e957`] が、**この集計から「実 FS を
  > 使わない」は導けない** — temp dir 検出は使用の**下限**であり、非発火は不使用の証明に
  > ならない (実際 6 file はいずれも `Path(__file__).resolve()` 等で実 FS を叩く)。
  > 同種の「下限の非発火から不使用を主張する」誤りは `### unit / integration 境界の既知の矛盾`
  > でも禁じている。
- cross-cutting なので **bounded context で細分しない**。context は filename /
  docstring に書く。
- 例: turnkey e2e acceptance (`logic-turnkey-e2e-acceptance`)、cross-project
  cockpit smoke の hermetic 版、3 層 window/lane acceptance の自動化部分
  (#12497-#12500 系)。
- 破壊的・実 host を要する acceptance は本 tests/ ではなく `smoke/**` に置く。
  scenarios は CI で hermetic に回せるものに限る。

### regressions

- 対象: 過去に確定した defect の **再発防止 pin**。**1 ファイル =** 1 つの修正済み
  症状 / Redmine issue に対応する characterization。
- **判定の主語は file であって test method ではない** (Redmine #14662 j#92449 裁定 1-2 /
  Review j#92458)。file 単位の連言 **R3-a ∧ R3-b ∧ R3-c** をすべて満たすときだけ本 type
  に該当する:
  - **R3-a**: その file が **単一の**修正済み症状 / **単一 Redmine issue** に対応する。
    命名 `test_issue_<id>_*.py` がその宣言である。
  - **R3-b**: file 内の **全 test** の主張が、その症状の **再来検出**であって、module の
    公開 contract の主張ではない。
  - **R3-c (grouping rule)**: file の `<id>` は **pin 対象の defect が修正された Redmine
    issue** である。同一 issue で修正された defect を pin する test は同一 file に置く。
    **journal id / finding id は file identity ではない。**
  - 症状 / 主張が **混在する file は本 type に該当しない** → `## 配置決定木` の分岐 4 / 5
    へ落ちる。
- **R3-b と owning issue の特定は著者宣言であり、機械導出しない。** 本規則が一意にするのは
  **基準**であって判定の機械化ではない (決定木の分岐 1 / 4 / 5 が概ね observable なのに
  対し、分岐 2 と分岐 3 は *その test が存在する理由* を問う判断分岐である)。
- **provenance anchor (docstring が持つ Redmine issue / journal / finding id) は
  R3-a / R3-b / R3-c いずれの根拠にもしない。** anchor は repo 全体の普遍的な記録
  convention であり、bucket 間の識別力を持たない [実測、base `dd62e957`: module docstring
  に anchor を持つ率は unit 305/309・integration 114/120・regressions 96/98・
  scenarios 12/12・flat 9/9 と **全 bucket で 95-100%**。method 単位でも、anchor を持つ
  test の **54% が既に regressions 以外**にある。anchor の絶対件数は anchor 検出 regex に
  依存するが (#14662 j#92449 は 247 件中 134 件、本 doc の再実測は 236 件中 128 件)、
  **どちらでも 54%** である]。`## Anti-patterns` を読む。
- cross-cutting なので **bounded context で細分しない**。
- **命名 (normative な必要条件)**: `tests/regressions/` の file は (1) filename が `<id>`
  を **1 つだけ**持ち (`test_issue_<id>_*.py`)、(2) **module docstring がその同じ `<id>`
  を名指す**。docstring には原因 commit も残す。
  **必要条件であって十分条件ではない** — 同じ命名の file は他 type にも存在し得るため、
  命名だけでは分岐 3 は成立しない (現に `tests/unit/` に `test_issue_<id>_*.py` 命名の
  **17 file** が存在する [実測、base `dd62e957`: #14219 が 14 / #14203 が 2 / #14150 が 1。
  #14662 j#92449 はいずれも feature 実装 test と判定している]。disposition は同 journal の
  backlog D = owner decision pending であり、本 doc は改名も許容も決めない)。
  > 履歴: 旧版が併記していた `test_<症状>_regression.py` は `<id>` を持たず上記 (1) を
  > 満たさないため **superseded** (該当する既存 file は無い)。現行 `tests/regressions/`
  > 98 file はいずれも `test_issue_<id>_*.py` 命名で、うち **96 file が (1)(2) を満たす**。
  > 残る 2 file (`test_issue_14203_pair_recovery_anchor_delivery.py` = module docstring
  > なし / `test_issue_14203_recovered_worker_delivery.py` = `R18:` としか書かず `#14203`
  > を名指さない) は **非適合であり、documented exception を grant しない**。disposition は
  > #14662 j#92449 backlog C = coordinator の owner decision pending。
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
対応表である。tests layout はこの対応表を **再利用**し、Redmine の日本語表示名や
数字始まりの `110_...` component を importable package path へそのまま焼き込まない。
Redmine の番号・順序は `bounded-context-map.md` の mapping metadata として保持する。

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
- Feature-level split が必要な大箱 context では、さらに import-safe な Feature slug で
  細分してよい: `tests/<type>/<context>/<feature_slug>/`。例:
  `tests/integration/execution_platform/delegated_coordinator_nested_handoff/`。
  Redmine Feature の番号 (`140`) は mapping metadata に保持し、directory component には
  入れない。
- **scenarios / regressions** は cross-cutting のため context で細分しない。
- **support** は context で細分しない (横断 helper)。context 固有 helper が必要に
  なったら `tests/support/<context>/` を後から足してよいが、初期は flat。

## 目標 directory layout (To-Be / #12490 が実体化済み)

context トークンは #12488 canonical (`execution_platform` / `operations_cockpit` /
`governance_distribution` / `adapter_provider` / `quality_architecture` /
`external_agent_ui`)。

```text
tests/
  __init__.py                  # package marker (discover -s tests では import されない)
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
> `src/` の sys.path 投入は **各 test module が self-insert** する (repo の既存
> idiom)。`python -m unittest discover -s tests` は `tests` を top_level_dir と
> して扱い `tests/__init__.py` を import しないため、`tests/__init__.py` の
> bootstrap には依存しない。これにより full / subpackage / single-file の
> isolated discovery がすべて自己完結する (Redmine #12490 j#64426 review fix)。

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
   `/` は **OR** であり、単一 bounded context でも複数 module をまたぐ operator 視点の
   通し受入なら該当する。**実 FS / 実 subprocess に触れるかは該当条件ではない**
   (`### scenarios`)。
3. **修正済み defect の再発防止 pin か?** → `tests/regressions/`
   (`test_issue_<id>_*.py`)。終了。
   判定は **file 単位**で **R3-a ∧ R3-b ∧ R3-c** (`### regressions`) を満たすときだけ
   成立する。test method 単位の述語へ読み替えない。**provenance anchor は根拠にしない。**
4. **単一 unit を隔離検証するか (collaborator は fake)?** →
   `tests/unit/<context>/`。`<context>` = subject の primary src module の
   bounded context。
5. **それ以外 (複数の実 collaborator を hermetic に結線)** →
   `tests/integration/<context>/`。

分岐 1 / 4 / 5 は概ね code から observable だが、**分岐 2 と分岐 3 は *その test が
存在する理由* を問う判断分岐**であり、著者宣言で決める。observable でない述語を機械化
しようとしないこと (Redmine #14662 j#92449 裁定 2)。

一意性の tie-breaker:

- unit / integration が複数 context に触れる場合、配置は **primary
  subject-under-test** (振る舞いを characterize している側) の context に従う。
  真に context 横断の受入なら integration ではなく scenario (分岐 2) に倒す。
  (これは *十分条件* 側の記述であり、分岐 2 の `/` が OR であること (`### scenarios`) と
  矛盾しない。)
- unit vs integration は **実 collaborator の数**で決める (1 = unit、複数 =
  integration)。**この tie-breaker は下記 `### unit / integration 境界の既知の矛盾`
  の対象であり、#14660 family scope では family 限定の literal rule が優先する。**
- 破壊的 / 実 host / 実 network を要する受入は本 tests/ ではなく `smoke/**`。

### unit / integration 境界の既知の矛盾と #14660 family 限定の解消

**認定する矛盾 (Redmine #14662 j#92449 裁定 4 / Review j#92458)。** `### unit` は
collaborator を「**subject-under-test 以外の**」と定義する。したがって **実 filesystem が
唯一の非 subject collaborator である test** は、

- 上の tie-breaker「実 collaborator が 1 = unit」では unit を指し、
- `### unit`「I/O・sleep・実 subprocess を持たない」では unit を否定され、
- `### integration`「**複数の**実 collaborator」にも入らない。

**どの分岐にも一意に入らない。** この矛盾は本 doc に実在する。

**#14660 family (legacy mirror test family) に限定した解消:**

```text
unit        : 実 外部 collaborator が 0 (subject は数えない)。
              実 FS / 実 subprocess / sleep / 実 network を持たない。
integration : 実 外部 collaborator が 1 以上で、hermetic (temp dir / in-memory /
              fake client) に閉じる。
```

実 filesystem は実外部 collaborator として数える。これにより実 FS 単独ケースは 1 分岐に
だけ入る。この family scope 内では tie-breaker「1 = unit」は本規則と両立せず、本規則が
**置換**する (従属ではない)。**判定の正本は各 test を読むことであり、構文的検出器の出力
ではない。**

**scope の境界 (重要):**

- 本 literal rule の scope は **#14660 family と、その family の新規配置に限る**。
  `### unit` / `### integration` / 上の tie-breaker の **global 記述は変更していない**。
- **global canonical rule への昇格は行っていない。** 昇格は完全な実 FS 使用 inventory の
  取得方法を前提とする別 decision (#14662 j#92449 backlog B、coordinator / owner の
  `owner decision pending`) が所有する。それが決まるまで rule scope は family のままである。
- **既存 corpus への例外は grant しない。** 既存 corpus の本 rule への適合状況は**未測定**
  である。実 FS 使用 file の構文的集計 (#14662 j#92449: unit 89/309・integration 77/120・
  regressions 77/98・scenarios 6/12) は `tempfile` 系 API の検出のみによる**下限**であり、
  かつ**検出器依存**である (検出 API 集合をわずかに広く取った再実測では integration が
  78/120 になる。unit / regressions / scenarios は一致)。下限で例外集合を定義すると、
  未検出の既存 file が後日 flag されたとき**新規違反か既存例外かを判定できない**。
  `## #12490 migration contract` の documented exception は 4 file を exact path で列挙し
  理由と解消条件を持つ形であり、**同型ではない**。したがって本 doc は既存 file への
  exception を granted と書かない。

## #14660 legacy mirror family 裁定 (Redmine #14662 R4)

`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`
(以下 **#14660 family**、127 test) の分割・移設に本 doc の決定木を適用した結果の裁定を固定する。
これは **base `dd62e957` 時点の現行 path であって移設先ではない** — 127 件の行き先は本節の基準に
基づく #14660 の分類で決まり、本 doc は先取りしない。正本は Redmine #14662
Implementation Done **j#92449** (R4 裁定) / Review **j#92458** (承認、指摘 0)、coordinator
intake は j#92461。上記 `### scenarios` / `### regressions` / `### unit / integration
境界の既知の矛盾` / `## Anti-patterns` の改訂は同じ裁定の反映である。

### 移設 hold と解除条件

**本 doc の改訂 (T-P = Redmine #14664) が canonical doc へ land するまで、#14660 の配置
matrix 確定と物理移設を開始しない。** 裁定 journal が approved になっただけでは開始しない。
agent は catalog 経由で doc を読むため (central preset `### 回答前 Doc 解決`)、journal に
裁定があっても doc が旧記述のままなら次の読者が同じ誤導出を再生産する。**本節が land した
時点で T-P の条件は満たされる** (T-P 以外の resume 条件 — #14660 の park state と
coordinator の resume 判断 — は #14660 / coordinator が所有する。本 doc は T-P だけを解く)。

```text
T0 (#14662 裁定)  →  T-P (本 doc 改訂 = #14664)  →  T1  →  {T2, T3, T5, T6}
                                                     T3  →  T4
```

| 単位 | 所有 |
| --- | --- |
| T-P (本 doc の改訂) | #14664。`tests/**` / `src/**` に触れない doc-only |
| 配置 matrix の確定 / Appendix A の訂正 | #14660 |
| T1 / T5 / T6 (物理移設) | #14592 配下の各移設 Task |

### family の配置基準 (適用時の要約)

決定木の順序 (support > scenarios > regressions > unit > integration) を保つ。**各行の
scope は同じではない**: scenarios 行と issue regression 行は上の **global 定義の要約**、
shared support fake 行の閾値と pure unit / real-file integration 行の literal rule は
**#14660 family 限定**である。

| 分類 | 行き先 | 判定 |
| --- | --- | --- |
| shared support fake | `tests/support/` | test ではない fixture / helper / builder / fake で、**2 つ以上の移設先 test module** が使うもの。`test_` prefix を付けない。単一 module 専用は module-local |
| scenarios | `tests/scenarios/` | 複数 module をまたぐ operator 視点の通し受入 (`### scenarios`。判断分岐 → 著者宣言)。単一 bounded context でも成立し、**実 FS の有無は問わない** |
| issue regression | `tests/regressions/test_issue_<id>_*.py` | **R3-a ∧ R3-b ∧ R3-c** (`### regressions`。file 単位・著者宣言)。provenance anchor は根拠にしない。命名条件は必要条件 |
| pure unit | `tests/unit/<context>/` | **実外部 collaborator 0** (subject は数えない。family scope の literal rule) |
| real-file integration | `tests/integration/<context>/` | **実外部 collaborator 1 以上**で hermetic に閉じる (family scope の literal rule) |

support の閾値 (2 module 以上) の根拠 [実測、base `dd62e957`、AST の import 解析]: 現行
`tests/support/` の 7 file は **全て 2 つ以上の test module から import されて**おり
(import 元が 1 module 以下の support file は存在しない)、consumer は unit 16 /
regressions 14 / scenarios 7 / integration 6 file と **bucket をまたぐ**。

分岐 2 / 分岐 3 は判断分岐なので、どの test が該当するかは **#14660 の著者宣言**とし、本
裁定は基準のみを定める。**候補を特定の class に限定しない** — pure cluster も含め
**全 127 件について分岐 2 を評価する**。127 件の per-test 割当は #14660 が所有する。

### 移設の検算

**無条件に使える検算は 1 本だけである:**

```text
unit + scenarios + regressions + integration = 127
```

決定木が各 test にちょうど 1 つの行き先を与えることから従う **partition の恒等式**であり、
検出器にも surface にも依存しない (support へ抽出する fixture は test ではないので 127 の
分配先にならない)。

これを、移設前後で collected test 数が一致することの確認 (`## discovery / CI 方針` の移行契約と
同型) と併せて使う。**恒久に残るのは count 一致を確認する command であって特定の数値ではない**:

- **D1 (family focused)** = 本 family の test 数 **127**。family 内で閉じた定数であり、
  上の partition 恒等式と同じ根拠を持つ。
- **D2 (repository discovery)** = `unittest.defaultTestLoader.discover('tests').countTestCases()`。
  **各物理移設 Task が自身の exact pre-move base で `N` を測り、post-move が同じ `N` である
  ことを検証する**。`N` は family と無関係な test の増減でも動くため、**特定の数値を本 doc に
  固定しない**。

> **数値の出所と、裁定からの correction の明示。** #14662 j#92449 は D2 を `13,207` という
> 数値ごと「移設後に残る恒久不変条件」と書いている。`13,207` は **#14660 characterization が
> 自身の base で測った snapshot** (#14660 j#92381 / j#92393) であり、**本 doc の base
> `dd62e957` で同じ command を実行した実測は `13,343`** である。数値を恒久不変条件として持つと
> base が進むたびに偽陽性になるため、本 doc は不変条件を **「各 Task が自 base で測り一致を
> 見る command」へ分離**する。これは j#92449 の literal に対する **policy correction** である
> (裁定の意図 = 移設で test を落とさないこと、は変えていない)。

**surface 集計は diagnostic であって acceptance invariant ではない** [出所: **#14660 Appendix A.2
の構文的導出**。同 characterization の base での値であり、本 doc が実測した値ではない]:

| surface | tests |
| --- | ---: |
| pure (FS 非依存) | 23 |
| real tree | 96 |
| subprocess | 8 |

使い方: 「構文的検出器が `real_fs` / `subprocess` と分類した test が unit に置かれている」
ことを見つけたら、**検出器の false positive か配置誤りのどちらかなので、どちらかを特定
せよ**という *調査の trigger* とする。**reject 条件・上界・must としては使わない。**

> **撤回済み:** 旧案の `unit ≤ 23` と `scenarios + integration ≤ 104` は **両方撤回**
> されている。分岐 2 / 分岐 3 は分岐 4 / 5 より先に評価され surface を必要条件にしない
> ため **pure からも scenarios / regressions が出る**し、`pure(真) ⊆ pure(構文的検出)` も
> 保証されない。**正しい分類を reject し得る値を検算に使わない** (#14662 j#92449)。

### 導出器 (#14660 Appendix A) の位置づけ

#14660 の Appendix A に置かれた分類導出器は **migration-time artifact** であり、Appendix A
に**据え置く** (`tests/support/` へ昇格しない)。理由: 導出器は移設前の単一 file を引数に
取り、移設完了時点で subject が消えて実行不能になる — 恒久 gate に見せかけた一時 gate を
CI に足さない。`### support` の定義 (test から import される共有 fixture) にも合わない。

- drift window は「本裁定 → T1 / T5 完了」に限定し、その窓の drift 検出は各移設 Task の
  完了条件とする。移設完了時に **superseded** と明記して retire する。
- 移設後に残る恒久不変条件は **collected test 数の一致確認** であり、**script ではなく
  command** である (D1 は family 定数、D2 は各移設 Task が自 base で測る。`### 移設の検算`)。
- **構文的検出器 (Appendix A.2) は分岐 4 / 5 の候補抽出と上記 diagnostic trigger に有効で
  あり、「実外部 collaborator が 0 か」の判定の正本ではない。** 判定は各 test を読んで行う。
- 必須の訂正 (#14660 所有): 上記 `### regressions` の裁定により **A.3 の分岐 3 判定は
  無効**であり、`### scenarios` の裁定により **A.3 が分岐 2 を評価していない**ことが顕在化
  している。#14660 は A.3 に両方を明記するか、該当 arm を撤回する。

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

- Redmine の日本語表示名や数字始まりの `110_...` component を tests directory にそのまま焼き込む
  (対応表で結び、package path は import-safe slug にする)。
- type 軸と context 軸を二重 top-level にして配置を多義にする (type-first に固定)。
- `discover` のコマンド文字列を移行のために書き換える (不変が契約)。
- サブディレクトリの `__init__.py` を省いて nested test を false green にする。
- scenarios / regressions を context で細分し、横断テストの置き場を曖昧にする。
- support に `test_*.py` を置いて helper を test として走らせる。
- **provenance anchor (docstring が持つ Redmine issue / journal / finding id) を分岐 3 の
  判定根拠にする** (anchor は全 bucket で 95-100% 発火する記録 convention であり、
  分類器ではない。`### regressions`)。
- **file 単位の規則 (分岐 3 / `### regressions`) を test method 単位の述語へ読み替える**
  (誤適用の発生点。1 file 内に混在があれば file ごと分岐 4 / 5 へ落とす)。
- **正本が結合子を定義していない列挙 (`複数 module / 複数 bounded context`) を、既存
  corpus に当てずに AND / OR のどちらかへ決め打つ** (分岐 2 の `/` は OR。`### scenarios`)。
- **surface (実 FS / 実 subprocess を使うか) で population を切ってから type を割り当てる**
  (決定木は support > scenarios > regressions > unit > integration の順であり、分岐 2 と
  分岐 3 は surface を必要条件にしない)。
- private path / secret-shaped literal を support / fixtures に書く
  (`rule-public-private-boundary`)。
- 実 network / 実 owner / 実 publish を unit / integration に持ち込む (smoke へ)。

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --repo . --check`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
