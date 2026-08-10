# Local Parallel Test Runner (isolated process shards)

Redmine #13733 (parent US #13732 `ローカル並列全件テストと段階的CIゲートで検証待ちを短縮する`,
Version `モジュール分割・テスト影響範囲整備枠`)。現行 single-process
`unittest discover` と **同一 test 集合・同一 green/red verdict** を、isolated
process shard で安全に並列実行する local CLI の設計正本。

`mozyo-bridge tests profile` (#12754) が「走らせた test の runtime を測る」を、
`mozyo-bridge tests resolve` (#12752) が「変更 source → focused target」を担うのと
同じ `tests` family / 同じ `f_150_ci_verification` feature に属する。本 issue は
「全件を isolated shard で速く回す」を担う。CI trigger / workflow の変更、
TestPyPI/PyPI publish、version bump は non-goal (#13732 boundary)。

## 受け入れ条件との対応

| 受け入れ条件 | 実現 |
| --- | --- |
| serial と parallel で同一 test 集合・同一 verdict | parent も worker も authoritative な `TestLoader().discover(start_dir, pattern, top_level_dir)` を `_repo_root_importable` 下で使う。parent が discover した module→id を worker に配り、worker は割当 module を **同名 load** して実行するため、shard 群が走らせた id の union は discover 集合に一致する |
| shard failure を aggregate green にしない | aggregate は **全 shard passed かつ union(ran ids) == discovered ids** のときだけ green。failure / timeout / worker crash / collection import error はいずれも fail-closed で red (`domain/test_parallel.py` の `aggregate`) |
| parallel run が live Herdr lane/process へ副作用を出さない | shard ごとに固有 `HOME` / `TMPDIR` / `MOZYO_BRIDGE_HOME` を与え、live cockpit-session env pin (`TMUX` / `TMUX_PANE` / `MOZYO_WORKSPACE_ID` / `MOZYO_LANE_ID` / `MOZYO_AGENT_ROLE`) を除去する。fresh `HOME` は parity を壊さないよう **functional** に保つ (`PYTHONUSERBASE`=実 user-base で nested `python` の user-site 解決、`GIT_AUTHOR_*`/`GIT_COMMITTER_*` で git identity)。`MOZYO_REPO` は inherit (下記) |
| 各 shard の stdout/stderr/exit を収集する | subprocess の stdout/stderr を bounded tail + `returncode` として全 shard の `ShardResult` に保持し、JSON/text output に surface (failure 時は再現可能な一次 evidence) |
| deterministic shard plan + `--jobs` + fail-fast + replay | module weight (duration manifest or discovered test count) で LPT bin-packing。shards は jobs より **over-partition** (既定 `jobs*4`、module 数 cap)し bounded pool で drain。`--jobs`(既定 host CPU 数)、`--shards`、`--failfast`(失敗観測後は queue 中 shard を launch せず skipped)、shard ごとの `python -m unittest -v tests.<module>` replay を出力 |
| serial discover との count/outcome parity・失敗系 fail-closed の固定 | 上記 parity + fail-closed を unit (planner/aggregator/policy) と integration (fixture tree の end-to-end) の regression で固定する |
| current host の実測記録・速度を hard gate にしない | 速度値は pass/fail 閾値に **しない**。verdict は test 集合の green/red のみ。壁時計は informational として出力・issue journal に記録する |

## CLI: `mozyo-bridge tests parallel`

正本実装:

- pure core: `src/mozyo_bridge/e_150_quality_architecture/f_150_ci_verification/domain/test_parallel.py`
  (shard plan / aggregate verdict / policy parse; I/O は `load_policy` の 1 read のみ)。
- handler: `.../application/commands_test_parallel.py`
  (`cmd_tests_parallel` = discovery + shard 実行 + aggregate + 描画、
  `cmd_tests_shard_worker` = 1 shard の実行 + JSON result 出力)。
- registrar: `.../application/cli_test_parallel.py` (`tests parallel` と hidden
  `_shard-worker` を #12752 の `tests` family へ追加)。
- policy 文書: repo root の `test_parallel_policy.yaml` (serial bucket + 既定 jobs/timeout)。

### discovery parity は構造で保証する

parent の `_discover_module_tests` は `tests profile` / `python -m unittest
discover -s tests` と **同一の** `discover` call を `_repo_root_importable`
(#13555 の cross-package import fix を共有) 下で実行し、discovered test を dotted
module でグルーピングする。各 shard の worker は割当 module を `loadTestsFromName`
で **同名 load** する — その際 `sys.path` を discover と同じ状態 (top-level dir =
`tests/` と repo root の双方) に整えるため、worker が走らせる id は discover が同
module に対して生成する id の subset に一致する。

collection 時に import できない module があると `discover` は
`unittest.loader` sentinel を返す。base suite が clean に import しない状態では
安全に shard 化できないため、parent はここで **fail-closed** する (shard 化して
から気付くのではなく、shard 化前に red)。

### aggregate は fail-closed

`aggregate(plan, results)` が green を返すのは次の **すべて** を満たすときだけ:

- 全 shard が `passed` (worker が success を report し returncode 0)。
- `union(shard が走らせた id)` が discovered 集合に **完全一致** (欠落 = dropped
  shard / mid-run で死んだ worker、余剰 = plan 外の test 実行)。
- observed shard 数 = planned shard 数。

したがって次はいずれも aggregate を green にできない: test failure / error、shard
timeout (kill)、worker crash (result 未出力)、collection import error、module の
取りこぼし。これが受け入れ条件「shard failure を aggregate green にしない」の
機械的固定である。

### isolation: shard ごとに固有 HOME/TMP/state、ただし fresh HOME を functional に保つ

acceptance #3 は「各 shard へ **固有 HOME**/TMPDIR/MOZYO state を与える」ことを要求する。
`_shard_env` は shard ごとに固有 `HOME` / `TMPDIR` / `TMP` / `TEMP` /
`MOZYO_BRIDGE_HOME` (home-scoped SQLite state store) を作り、live cockpit-session pin
(`TMUX` / `TMUX_PANE` / `MOZYO_WORKSPACE_ID` / `MOZYO_LANE_ID` / `MOZYO_AGENT_ROLE`) を
除去する。

課題は **fresh HOME が parity を壊さない**ことである。素の fresh HOME は (a) interpreter
の user site-packages (PyYAML 等が pip-user-install される場所) を隠し、(b) git identity
を持たないため、nested `python -m mozyo_bridge` (`test_pre_commit_hook` が hook 経由で
spawn) や `git commit` を行う hermetic test を壊す (R1 dogfood で観測した 5 test 赤の
実因)。是正は「固有 HOME を諦める」ではなく「固有 HOME を機能させる」:

- `PYTHONUSERBASE` = parent の実 user-base を子へ渡す → fresh HOME でも nested `python`
  が user-site を解決する。
- `GIT_AUTHOR_*` / `GIT_COMMITTER_*` を deterministic に設定 → `git commit` が operator
  `~/.gitconfig` 非依存で成立する。

`MOZYO_REPO` は **inherit** する (pin しない): repo 解決が serial と同じ cwd/env 規則に
従い、pin すると divergent-cwd 解決を検証する test を壊すため。

Redmine #14757 が本 runner に加えたのは次の 2 点で、shard の既存契約は変えない。正本は
`test-process-home-isolation.md` を読む (本 doc に複製しない)。

- **parent discovery も isolated process へ移した。** shard は隔離済みでも、parent は
  authoritative discovery を自 process で行い、全 test module を operator home 解決下で
  import していた。`cmd_tests_parallel` は最初に自分自身を fenced child へ re-exec し、
  run の前後で operator 共有 home を照合する (変化していれば shard が全て緑でも red)。
- **shard env に `XDG_*` pin と process home fence を additive に足した。** 本 runner の
  **per-shard `HOME` pin は維持する** — #13733 自身の acceptance であり
  `test_issue_13733_shard_env_hermetic.py` が pin している。#14757 の rail は `HOME` を
  repurpose しない側なので、2 rail は `HOME` の 1 点でのみ異なる。この divergence は意図的
  で、根拠は `test-process-home-isolation.md` の該当節にある。

### shard env は「serial env + isolation」であり、それ以外を足さない

isolation のために足してよいのは、**shard の外へ意味が漏れない** pin だけである。
`HOME` / `TMPDIR` / `MOZYO_BRIDGE_HOME` / `PYTHONUSERBASE` / git identity は shard 固有の
資源を指すのでこれを満たす。一方 **`PYTHONPATH` は満たさない**: shard 子 process の env は
test 本体が spawn する nested subprocess へそのまま継承されるため、runner が入れた entry は
「runner が test に見せたい import 経路」ではなく「test が起動する任意の process の import
経路」になる。

これを破ったのが Redmine #13735 j#78390 F1 である。runner は子の import 解決のために
`PYTHONPATH=<mozyo_bridge package の親 dir>` (source mode では `<repo>/src`) を注入していた。
wheel を build して throwaway venv へ nested `pip install` する test がこれを継承すると、
`src/mozyo_bridge.egg-info` の同居により pip が **same version already installed** と判定して
install を skip し、**exit 0 のまま console script を作らない**。結果 serial green / parallel red
という決定論的な verdict 破れが生じた (`--jobs 1` でも再現)。serial bucket は救済にならない:
serial shard も同じ `_shard_env` を通るため、module を移しても env は変わらない。

したがって現行契約は次の 2 点である。

- **runner**: `PYTHONPATH` は inherit したまま **verbatim で素通し**する (serial と同一)。子の
  runtime 解決は env ではなく **in-process** で行う — shard は `python -c <bootstrap>` で起動し、
  bootstrap が絶対 path の package dir を `sys.path` へ挿入してから CLI `main` を呼ぶ。`sys.path`
  は process-local なので、foreign cwd / 非 install な source checkout でも子は親と同じ runtime を
  import しつつ、その経路は grandchild へ継承されない。
- **test-side**: installed artifact を検証すること自体が目的の test (wheel install → venv →
  console script) は、nested subprocess の env を `tests/support/nested_python.py` の
  `hermetic_python_env()` で組み、`PYTHONPATH` を落として caller の env に依存しない。

regression: `tests/regressions/test_issue_13733_shard_env_hermetic.py` が (a) `_shard_env` が
`PYTHONPATH` を注入せず inherit 値を書き換えないこと、(b) shard の test 本体と **その子 process**
が injected path を見ないこと (live probe)、(c) hostile な ambient `PYTHONPATH=<repo>/src` 下でも
nested pip が実際に wheel を install して console script を提供すること、を固定する。

### over-partition と `--failfast`

parallel shard 数は worker 数 (`--jobs`) と **切り離す**。既定の shard 数は
`jobs * 4` (module 数 cap、`--shards` で明示可)で、`jobs` 個の worker が finer な shard
queue を drain する。これにより (a) load balance が改善し (遅い module が 1 個の太い
shard を占有して他が idle する事態を避ける)、(b) `--failfast` が意味を持つ: shard が
失敗したら未起動 (queue 中) の shard は launch せず `not run (--failfast)` として skipped
にする (in-flight の subprocess は kill せず完了させる)。full dogfood では 324 module を
jobs=10 で 40 shard に分割し、wall clock が serial 比で更に改善した。

## local full-suite の実行 (Redmine #15229)

#15229 の起点は「ローカル全件テストが 45 分超で未完走」という owner 観測である。測定した
結果、支配的な要因は個々の test の実時間待機ではなく **どの entrypoint をどの runtime で
起動したか** であった。本節は local full の推奨経路と、時間値の扱いを固定する。時間値は
`## reliability invariant` のとおり **informational** であり pass/fail 閾値ではない。

### 推奨経路

| 目的 | command |
| --- | --- |
| local full の既定 | `PYTHONPATH=src python3 -m mozyo_bridge tests parallel --jobs 10` |
| focused / 集合と verdict の正本 | `mozyo-bridge tests run [-- <targets>]` |
| CI full lane | GitHub Actions `Integration batch` |

serial 直列 discovery は **test 集合と green/red verdict の正本**であり続ける
(`## reliability invariant`)。ただし *日常の全件確認* に直列 `python -m unittest discover` を
選ぶ理由はない: 本 runner は同一 discover call を使い aggregate が parity を強制するため、
集合も verdict も serial と一致する。

### 実測 (2026-08-10 / macOS 14 core / `jobs=10` / #15229 head)

| 測定 | 値 |
| --- | ---: |
| `tests parallel --jobs 10` (既定 40 shard) | 16,894 tests / wall **151s** / PASS |
| 同 `--shards 659` (1 module = 1 shard) | wall **243s** / PASS |
| module 単位 wall の総和 (659 shard 実測) | 2,252s (うち 659 回の shard 起動が約 1s/shard) |
| 最遅の単一 module | 111s (`integration...f_160_release_version_governance.test_release_helpers`) |
| CI `Integration batch` (full + build + smoke) | 12m24s (#15138 j#102341) |

読み方は 3 点である。

- **直列全件が 25〜45 分かかるのは単一の病理ではない。** module 単位 wall は long tail で、
  30s 超が 11 module / 546s、10〜30s が 20 module / 364s を占める。上位は real subprocess・
  wheel build・synthetic Git repo・installed launcher を回す integration / regression であり、
  「1 個の異常な待機を消せば直列が速くなる」形ではない。**直列を速くするのではなく、
  local full の既定 entrypoint を parallel runner にする**のが本 issue の結論である。
- **wall clock の下限は最遅 module である。** 既定 40 shard の 151s は最遅 module 111s に
  対してほぼ最適に近い。`--shards` を module 数まで上げると shard 起動が支配して **遅くなる**
  (243s)。更に詰めるなら shard 数ではなく `tests profile --format json` の duration manifest を
  `--durations` に渡して weight を実測へ寄せる (`## deterministic plan と weight`)。
- **途中の `F` を final traceback まで保持する経路は runner 側にある。** 直列 discovery を
  途中で中断すると traceback を失うが、本 runner は失敗 test id・shard の stdout/stderr tail・
  shard 単位の replay command を aggregate 出力に載せ、`--format json` で機械可読に残す
  (`## CLI` / `### aggregate は fail-closed`)。早期に閉じたいときは `--failfast` を使う。
  ただし `--failfast` は queue 中 shard を止めるので、**残りの同型欠陥を見落とす**: #15229 の
  初期観測では 1 件目で閉じたため、同一 helper に由来する 2 件目と別 module の 3 件目が
  見えていなかった。欠陥類型を数えるときは failfast なしで 1 回通す。

### runtime provenance — CLI 自身の runtime が検証対象になる

`tests parallel` / `tests profile` は parent 自身を fenced child へ re-exec し、その bootstrap は
**呼び出した CLI 自身の** package dir を `sys.path` へ挿入する (機構の正本:
`test-process-home-isolation.md` の `## 3 つの入口`)。したがって **どの CLI で起動したかが、
どの runtime を検証したかを決める**。worktree の `src/` を検証したい場合は worktree の runtime で
起動する — それが上表の `PYTHONPATH=src python3 -m mozyo_bridge` 形の理由である。

installed binary (`mozyo-bridge`) から起動すると installed package が検証対象になる。#15229 で
実測した形は次である: installed `0.20.1` は worktree の `src/` より古く、`mozyo_bridge` は
discovery より前に installed 側から import 済みになるため、test module 自身の
`sys.path.insert(0, ROOT / "src")` は **既に import 済み package の `__path__` を変えられず inert**
である。結果、worktree 固有 symbol を参照する 13 module が collection import error になり、full run
は 2.9s で fail-closed した。fail-closed 自体は正しい挙動だが、教訓は **version 文字列の一致は
module 集合の一致を意味しない**ことである (installed も source も `0.20.1` を名乗っていた)。

`tests run` はこの影響を受けない: fenced child が literal `python -m unittest` を **新しい
interpreter** で起動するため `mozyo_bridge` は事前 import されておらず、corpus 規約 (各 test /
support module 自身が repo-local `src/` を `sys.path` へ挿入する。正本: `tests/__init__.py` の
module docstring) どおりに解決する。

## serial bucket 方針 (明示 bucket と、既定が空である根拠)

受け入れ条件は「Herdr/tmux/real process/shared state を触る parallel-unsafe test を
**明示 serial bucket** へ置く」ことを要求する。本実装はその bucket 機構
(`test_parallel_policy.yaml` の `serial_modules` fnmatch pattern → 非並列の単一
serial shard) を実装する。integration regression が非空 serial policy を fixture で
実際に行使する。

**既定の serial bucket は空である。これは evidence-based な決定であり oversight では
ない。** 根拠:

- discovered `tests/` suite は placement policy
  (`tests-placement-discovery-policy.md`) により hermetic である。real tmux /
  network / owner / Redmine / host-global singleton を触る work は `smoke/**`
  (tests/ discovery root の **外**) にあり、本 runner は走らせない。
- shard は process 隔離 + 固有 HOME/TMPDIR/MOZYO state + session pin 除去を持つため、
  cwd を書き換える (`os.chdir`) test も home-scoped state store を触る test も、
  process をまたいで隔離される。したがって discovered module に「自分専用の process
  shard で走らせて危険」なものは無く、serial bucket は空になる。

**entry を足すべきとき:** module が per-process 隔離下でも unsafe である証拠がある
とき — すなわち isolated HOME/TMPDIR/MOZYO state と除去した session pin では分離
されない資源 (固定 TCP port、HOME/TMPDIR 外の固定 on-disk path、real OS-level
singleton) を奪い合うとき。証拠は所有 Redmine issue に記録する。

## deterministic plan と weight

`plan_shards` は module を serial (policy match) と parallel に分け、parallel を
LPT (longest-processing-time) で bin に詰める。bin 数 (= parallel shard 数) は
`jobs` (=concurrent worker 数) と **切り離す**: `shard_count` 明示時はその値、無指定
時は既定 `jobs * DEFAULT_SHARDS_PER_JOB` (=`jobs*4`)、いずれも parallel module 数で
cap する (`--shards` で明示可)。この over-partition により `jobs` worker が finer な
shard queue を drain し、load balance が改善し `--failfast` が queue 中 shard を止め
られる (`### over-partition と --failfast`)。weight は duration manifest (`--durations`、
`tests profile --format json` 形も可) があれば実測秒、無ければ discovered test count。
tie-break は module 名 → bin index で、plan は入力で完全に決定される (同一 suite + 同一
jobs + 同一 shard_count + 同一 weight → 同一 plan)。これにより shard failure は出力
される replay command で再現できる。

## reliability invariant (本 runner が緩めないもの)

- **test 集合と verdict は serial が正本。** parallel は同じ discover を使い、
  aggregate は parity を強制する。並列化は速度のためであり、coverage / outcome を
  一切弱めない (#13732 boundary)。
- **速度値を hard gate にしない。** verdict は test 集合の green/red のみ。壁時計は
  informational であり pass/fail 閾値ではない。
- **CI は不変。** 本 issue は `.github/workflows/**` と release gate docs を変更
  しない。段階的 CI gate は姉妹 issue #13734 が所有する。
