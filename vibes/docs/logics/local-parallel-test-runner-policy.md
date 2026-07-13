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
従い、pin すると divergent-cwd 解決を検証する test を壊すため。子 `PYTHONPATH` は
absolute な mozyo_bridge package dir に固定し、foreign cwd / relative `PYTHONPATH=src`
でも import が解決するようにする (system site-packages は自動、user-site は
`PYTHONUSERBASE` が担う)。

### over-partition と `--failfast`

parallel shard 数は worker 数 (`--jobs`) と **切り離す**。既定の shard 数は
`jobs * 4` (module 数 cap、`--shards` で明示可)で、`jobs` 個の worker が finer な shard
queue を drain する。これにより (a) load balance が改善し (遅い module が 1 個の太い
shard を占有して他が idle する事態を避ける)、(b) `--failfast` が意味を持つ: shard が
失敗したら未起動 (queue 中) の shard は launch せず `not run (--failfast)` として skipped
にする (in-flight の subprocess は kill せず完了させる)。full dogfood では 324 module を
jobs=10 で 40 shard に分割し、wall clock が serial 比で更に改善した。

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
`min(jobs, n_parallel_modules)` bin に LPT (longest-processing-time) で詰める。
weight は duration manifest (`--durations`、`tests profile --format json` 形も可) が
あれば実測秒、無ければ discovered test count。tie-break は module 名 → bin index で、
plan は入力で完全に決定される (同一 suite + 同一 jobs + 同一 weight → 同一 plan)。
これにより shard failure は出力される replay command で再現できる。

## reliability invariant (本 runner が緩めないもの)

- **test 集合と verdict は serial が正本。** parallel は同じ discover を使い、
  aggregate は parity を強制する。並列化は速度のためであり、coverage / outcome を
  一切弱めない (#13732 boundary)。
- **速度値を hard gate にしない。** verdict は test 集合の green/red のみ。壁時計は
  informational であり pass/fail 閾値ではない。
- **CI は不変。** 本 issue は `.github/workflows/**` と release gate docs を変更
  しない。段階的 CI gate は姉妹 issue #13734 が所有する。
