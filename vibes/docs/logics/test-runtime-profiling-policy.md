# Test Runtime Profiling and Slow-Test Budget

Redmine #12754 (parent US #12510 `140_DelegatedCoordinator・NestedHandoff`,
Version `モジュール分割・テスト影響範囲整備枠`)。テスト遅延を主観ではなく
**実測** で残し、slow test / noisy verbose output / integration-style mock test /
実装者の広め target 選択のどれが CI を遅くしているかを後から切り分けられる状態に
するための設計正本。

`mozyo-bridge tests resolve` (#12752) が「変更 source → focused test target」を
担うのと対になり、本 issue は「走らせたテストの runtime を測って予算と突き合わせる」
を担う。両者は同じ `tests` family / 同じ `f_150_ci_verification` feature に属する。

## 受け入れ条件との対応

| 受け入れ条件 | 実現 |
| --- | --- |
| local / CI で test runtime summary を出せる | `mozyo-bridge tests profile` が discovery を時間計測付きで実行し summary を出す。CI の test step が本 command を使う |
| slow test threshold と例外記録の置き場を定義する | repo root の `test_runtime_budget.yaml` (threshold + 例外 allowlist)。本 doc が契約正本 |
| verbose output の必要性を lane ごとに分ける | 下記「verbose lane policy」。default lane は quiet + summary、investigation lane のみ `-v` |
| runtime profiling は通常テストの信頼性を落とさない | 下記「reliability invariant」。profiling は additive、verdict は suite が正本、enforcement は opt-in |

## CLI: `mozyo-bridge tests profile`

正本実装:

- pure core: `src/mozyo_bridge/e_150_quality_architecture/f_150_ci_verification/domain/test_runtime.py`
  (budget parse / timing→summary 分類; I/O は `load_budget` の 1 read のみ)。
- handler: `.../application/commands_test_runtime.py` (`TimingTestResult` +
  discovery 実行 + 描画)。
- registrar: `.../application/cli_test_runtime.py` (`tests profile` を #12752 の
  `tests` family へ追加)。

### discovery は CI と同一

`tests profile` は `unittest.TestLoader().discover(start_dir, pattern, top_level_dir)`
を使い、flag (`--start-dir` / `--pattern` / `--top-level-dir`) は
`python -m unittest discover` と同義。default は `--start-dir tests`
`--pattern test*.py` で、これは現行 CI の `python -m unittest discover -s tests`
と同じ start dir・pattern・module 命名 (`unit.<context>.test_*` 等) を再現する。
profiling は discovery 結果に手を加えない。

### 計測値 (runtime summary)

- `total`: per-test wall clock の総和 (sum)。
- `test_count`: 計測したテスト数。
- `outcomes`: passed / failed / errored / skipped の内訳 (記録のみ、結果は変えない)。
- `slowest N`: duration 降順の上位 N (`--slowest`, default 20)。
- `slow tests`: threshold 以上のテストを `exempt` / `violation` に分類。
- `stale budget exceptions`: 今回 slow でなかった例外 entry (削除候補)。

`--format json` で機械可読出力 (`success` field 含む) も出せる。テスト本体の
出力 (dots / verbose / traceback) は stderr、summary は stdout に出る。

## slow-test threshold と例外記録の置き場 (`test_runtime_budget.yaml`)

repo root の `test_runtime_budget.yaml` が **threshold と例外記録の単一の置き場**。

```yaml
slow_test_threshold_seconds: 1.0
exceptions:
  - test_id: "scenarios.test_turnkey_e2e.TurnkeyAcceptanceTest.test_full_flow"
    reason: "integration-style end-to-end flow"
    owner_issue: 12754
```

- threshold built-in default は **1.0s** (PyLint のような業界 default は無いが、
  unit test が 1 秒超なら surface する価値がある、という slice 判断)。`--threshold`
  で per-run override、`slow_test_threshold_seconds` で per-repo override。
- 例外 entry は threshold 超過を許すテストと **理由** を記録する。必須 field は
  `test_id` (summary が印字する unittest dotted id) と `reason`。`owner_issue` は
  任意だが推奨。
- threshold を上げて個別の遅さを隠さない: 整合性のため、遅い integration-style test
  は global threshold を上げるのではなく `exceptions` に記録する (上げると配下の
  全 regression が見えなくなる)。
- 不正 budget は fail closed (`TestRuntimeError`)。budget 不在は default threshold /
  例外なしで動作 (profiler は budget 未整備の repo でも走る)。

例外 record location は本 file 一箇所に集約する。テストコード側に「遅くてよい」根拠を
散らさない。

## verbose lane policy (受け入れ条件 #3)

`-v` (unittest verbosity 2) は **per-test 名を全部印字** するため CI ログが膨れる。
本 issue は verbose の必要性を lane ごとに分ける:

```yaml
default_lane:        # 通常の local / CI test run
  verbosity: 1       # quiet dots
  rationale: |
    slow-test signal は -v の per-test 名ではなく runtime summary が担う。
    失敗時の traceback は verbosity 1 でも出るので診断は落ちない。
investigation_lane:  # 失敗調査 / flaky 切り分け
  verbosity: 2 (-v)  # opt-in
  rationale: |
    どのテストが走った/どこで止まったかを per-test で追う必要があるときだけ -v。
enforcing_lane:      # slow-test budget の強制 (opt-in)
  flag: --enforce
  rationale: |
    非 exempt の slow test を violation として exit 非ゼロにする専用 lane。
    timing variance を含むため default lane では使わない。
```

`tests profile` は default で verbosity 1。`-v` / `-q` が lane knob。CI の test step は
default lane (summary 付き、`-v` なし) を使う。verbosity は **どのテストが走るか・
pass/fail には一切影響しない**ので、`-v` を外しても reliability は不変。

## reliability invariant (受け入れ条件 #4)

profiling が通常テストの信頼性を落とさないための不変条件:

- **verdict は suite が正本**: process exit code は `result.wasSuccessful()` 由来。
  profiling は summary を *足す* だけで、どのテストが走るか・結果を変えない。
- **enforcement は opt-in**: slow test / budget violation は default では報告のみ。
  `--enforce` を付けた専用 lane のときだけ exit 非ゼロにする。timing は環境差・負荷で
  揺れるため、threshold 超過で default CI を fail させると flaky 化する = 信頼性低下。
  これを避けるため enforcement は default off。
- **計測は観測のみ**: `TimingTestResult` の各 `add*` は outcome を記録して `super()`
  に委譲するだけ。pass/fail 報告は素の `TextTestResult` と同一。
- **fail closed な budget**: 壊れた budget は slow-test signal を黙って無効化せず
  例外を上げる。

## CI 接続

`.github/workflows/test.yml` の **full lane** の test step が
`mozyo-bridge tests profile` (default lane)。これ一つで「テスト実行 (gate) +
runtime summary」を同時に得る (二重実行しない)。CI は fresh `pip install .` 後に
走るので installed == working tree であり、テスト *内容* は一致する。

### repo-root import parity (Redmine #13555)

ただし installed console-script と `python -m unittest` には **import path の差**が
ある。`python -m unittest discover -s tests` は起動 cwd (= repo root) を
`sys.path[0]` に載せるため、repo-root の test package が持つ
`from tests.support ...` / `from tests.unit ...` の cross-package import が
collection 時に解決する。一方 installed console-script entry point は起動 cwd を
`sys.path` に載せないため、同じ discovery が全 Python matrix で
`ModuleNotFoundError: No module named 'tests'` を出し collection error になった
(latest main Test run `29129232080`, head `32916f84`、2 collection errors)。

したがって `commands_test_runtime._run_suite` は **discovery の間だけ repo root を
`sys.path` に bootstrap** し、`python -m` の cwd semantics を再現する
(`_repo_root_importable`)。この bootstrap は:

- repo root が既に path に在る local `python -m` / editable-install lane では
  **no-op** (二重挿入せず、既存 entry を除去しない)。
- discovery 後に `sys.path` を復元する (in-process caller の isolation)。
- `top_level_dir` を **変えない**ため、discover される module 名 / test ID は不変
  (`tests` を top_level_dir とする現行 discovery 命名を維持。詳細は
  `tests-placement-discovery-policy.md`)。selection / verdict / runtime summary /
  slow budget semantics も不変。

これにより installed full lane と local `python -m` lane は test 内容だけでなく
**collectability も一致**する。regression pin は
`tests/regressions/test_issue_13555_installed_tests_profile_repo_root_import.py`
(installed-path 条件を isolated fake repo で再現)。

quick / full のレーン分割 (Redmine #12753) では、`tests profile` は全件を担う
**full lane** 専用。PR の **quick lane** は `tests resolve` で affected subset のみ
走らせる (full discover は走らせない)。詳細は
`ci-quick-full-lane-policy.md` を参照。

local で working-tree の src を測るときは editable install (`pip install -e .`)
または `python3 -m mozyo_bridge tests profile` を使う (他の `mozyo-bridge` CLI と
同じ運用 — installed 版が stale だと installed 版を測る点に注意)。

`--enforce` は CI default では付けない。budget enforcement を CI 化する場合は
別 lane / 別 step として、timing variance を吸収する threshold 設計と合わせて
follow-up で判断する。

## 非目標

- 既存テストの高速化そのもの (本 issue は計測と予算可視化)。
- pytest 等への test runner 差し替え (CI authority は `unittest discover` のまま;
  `tests-placement-discovery-policy` を継承)。
- timing の永続保存 / 時系列ダッシュボード (per-run summary のみ)。
- budget enforcement の CI default 有効化 (opt-in のまま)。
