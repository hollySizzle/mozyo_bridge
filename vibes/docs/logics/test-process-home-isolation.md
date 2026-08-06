# Test-Process Home Isolation (operator shared home)

Redmine #14757 (parent #13490 `人間・coordinator・gateway・workerの単一入口E2Eを成立させる`,
Version `ワークフロー単一ステップ実行入口整備枠`)。通常の test process が operator の
**共有 mozyo-bridge home** を読み書きしないようにする verification rail の設計正本。

`tests profile` (#12754) が runtime を測り、`tests parallel` (#13733) が shard で速く回し、
`tests resolve` (#12752) が focused target を選ぶのと同じ `tests` family / 同じ
`f_150_ci_verification` feature に属する。本 issue は「test process が共有 state に
届かないこと」を担う。

## 何が起きたか (defect の 2 形)

| 形 | 実測 anchor | 機序 |
| --- | --- | --- |
| schema forward-migration | #14477 j#94521 / j#94527 / j#94528 | test が green のまま operator 共有 store を v7→v8 へ migrate した。`patch.dict(os.environ, {}, clear=True)` が `MOZYO_BRIDGE_HOME` を落とし、fallback `~/.mozyo_bridge` が `expanduser()` (HOME も無いときは passwd DB) 経由で共有 home に解決した |
| registry row 挿入 | #14757 j#100381 | `test_issue_14741_recovery_launch_cause.py` の `_reach_preflight` 2 test が `MOZYO_BRIDGE_HOME` を **関数引数**にだけ渡し、production `_prepare_session_locked` は `os.environ` から解決して `register_workspace(repo_root)` を `home=` 無しで呼んでいた。1 実行あたり temp workspace 2 行が operator live registry に入り、実 registry が 174→176→178 と増えた |
| `/tmp` ancestor marker | #14761 j#94732 | 空の `/tmp/.git` と `/tmp/.mozyo-bridge/workspace-anchor.json` が配下 temp dir の classification を汚染した。除去後 ambient failure が 38 failures / 5 errors → 3 まで減った |

共通点は「**green な suite は隔離の証拠にならない**」ことである。#14477 の run は
verdict 上 PASS だった。

## 二重機構 — pin と fence

pin だけでも fence だけでも足りない。

### pin (env、child へ継承される)

runner は task-specific temp root を 1 つ作り、次を pin する。

```text
MOZYO_BRIDGE_HOME                  = <root>/mozyo-home
TMPDIR / TMP / TEMP                = <root>/tmp
XDG_CONFIG_HOME / _CACHE_ / _DATA_ / _STATE_HOME = <root>/xdg/*
```

同時に live cockpit-session pin (`TMUX` / `TMUX_PANE` / `MOZYO_WORKSPACE_ID` /
`MOZYO_LANE_ID` / `MOZYO_AGENT_ROLE`) を **除去**する。pin は child process に継承される
ので、境界が親 process の外へ伸びるのはこの層による。

`XDG_*` が必須なのは `paths.CONFIG_HOME` が `XDG_CONFIG_HOME` 未設定時に
`Path.home()/.config` を使うためで、`MOZYO_BRIDGE_HOME` の pin だけでは覆えない。

### `HOME` を repurpose しない (acceptance 1)

**`HOME` は pin しない。** temp dir を指させると (a) interpreter の user site-packages が
隠れ、(b) operator の git identity が消える。#13733 はこれを `PYTHONUSERBASE` と合成
committer で補修しなければならなかった。本 rail の隔離は resolver 側で行うので、fenced
process は動く `HOME` を保持する。

### fence (process-local、`os.environ` の編集に耐える)

```text
MOZYO_BRIDGE_TEST_HOME_FENCE = <root>/mozyo-home   # 未 pin 解決の着地点
MOZYO_BRIDGE_TEST_HOME_DENY  = <operator home>[:...]  # 解決を拒否する root
```

正本は `src/mozyo_bridge/shared/paths.py` の `mozyo_bridge_home()`。この 2 変数は
**import 時に 1 度だけ** `_PROCESS_HOME_FENCE` へ取り込まれ、以降 call ごとに
`os.environ` を読み直さない。これが `clear=True` に耐える理由である
(現行 corpus の env-clear file は 35。`## 対象 inventory`)。

fenced process での `mozyo_bridge_home()`:

- `MOZYO_BRIDGE_HOME` が無い (clear された) → **fence root**。operator default には
  決して落ちない。
- `MOZYO_BRIDGE_HOME` が denied root (またはその subtree) を指す → `OperatorHomeFenceViolation`
  を **raise** する。値を返してから後で気付くのではなく、resolver で拒否する
  (acceptance 4)。
- それ以外 → 従来どおり解決する。

**unfenced process (= すべての production 実行) の挙動は byte-invariant である。** fence
env が無ければ `_PROCESS_HOME_FENCE is None` で、旧実装と同一の 1 行に落ちる。

`HomeFence.__init__` は root と denied の両方を `resolve()` する。macOS の temp root は
symlink 経由 (`/var/...` ⇄ `/private/var/...`) なので、片側だけ resolve すると deny が
一致せず fence が **黙って通してしまう**。

## 作用前拒否 — audit hook (R2 で追加、正本層)

R1 は「変更を検出して run を red にする」だけだった。review j#100407 R1-F1 / j#100408
はこれを退けた: acceptance 4 は **write attempt の拒否**、acceptance 7 は **変更 0** を
要求しており、row が着地した後で赤くしてもどちらも満たさない。R2 で **作用前拒否**を
第一級の層として追加した。

### 機構は実測で決めた (2 案を棄却)

| 案 | 結果 | 実測 |
| --- | --- | --- |
| `PYTHONPATH` 上の `sitecustomize.py` | **棄却** | `env={}` child は `PYTHONPATH` を継承せず、`sitecustomize.py` 自体が読まれない (j#100410) |
| task venv の site-packages に `sitecustomize.py` | **棄却** | Homebrew python が **stdlib dir** に自前の `sitecustomize.py` を同梱しており、`sys.path` 上 stdlib が site-packages より前なので **shadow される**。`'sitecustomize' in sys.modules` は True になるのに `__file__` は Homebrew 版で、`env={}` child の既存 file 上書きが **成功した** (j#100411 実測 2) |
| task venv の site-packages に **`.pth`** | **採用** | `site` は各 site-packages の **全 `.pth`** の `import` 行を実行するため、shadow される単一 name が無い。`env={}` child が既存 file を上書きしようとして **`PermissionError` / 内容不変** (j#100411 実測 3) |

**2 案目は「効いているように見えて効いていない」形だった。** `sys.modules` に入っている
ことを確認するだけで採用していたら、穴を「対策済み」と称して残していた。**injection
channel は「読まれたか」ではなく「拒否が実際に起きたか」で検証する。**

### 現行構成

run ごとに fence root 内へ task venv (`--without-pip --system-site-packages`、実測
0.06 秒) を作り、その site-packages へ

- `_mozyo_test_fence.py` — audit hook 本体
- `zz_mozyo_test_fence.pth` — `import _mozyo_test_fence` の 1 行

を置き、**suite をその interpreter で実行する**。site-packages 探索は interpreter 相対で
env に依存しないため、`env={}` の child でも hook が入る。**`PYTHONPATH` は使わない**ので、
#13733 j#78390 F1 (`src/` が nested pip を壊した) の懸念面が設計から消える。

- **denied root は生成 module 内の literal**。`os.environ` から読むと、この guard が
  survive するために存在する `clear=True` 自身に無効化される。
- hook は write 系 audit event (`open` の書込 mode / `os.mkdir` / `os.rename` /
  `os.remove` / `sqlite3.connect` の非 read-only 等) を `PermissionError` で拒否し、試行を
  fence root 内 ledger へ記録する。**read は拒否しない** (目的は「変更させない」であり、
  read 拒否は parent の consistent snapshot を壊すだけで acceptance に寄与しない)。
- **install 検証を毎 run 実行する** (`verify_audit_fence`)。`env={}` の probe child で拒否が
  起きることを確認し、起きなければ `AuditHookInstallError` で **run 自体を拒否**する。
  棄却案 2 の failure mode (成功したと報告しつつ実際は無防備) を再発させないため。
  self-check 自身の試行は ledger をリセットして除く。

### 「変更 0」の根拠は attribution に移した

acceptance 7 の一次証拠は **ledger が空であること**である。snapshot の tier 粒度を根拠に
しない — finding_1 が示したとおり、directory 比較では (a) 既存 row の in-place 更新、
(b) 既存 directory 配下の write、(c) 既存 file の内容上書き のいずれも見えない。ledger は
「この process tree が何も試みなかった」を言えるが、directory 比較はそれを言えない。

**ledger が missing なら failure** として扱う (runner が空で作るので、消えていれば fence が
設定どおり動いていない)。**拒否された試行も run を fail させる**: write は防げているが、
試みた test は defect であり黙って通してはならない。

## snapshot guard — hook が届かない writer 用の粗い backstop

hook は **その interpreter で起動した process** に入る。非 Python process や、hook を持たない
interpreter で起動された process は覆えないため、run の前後で **`ambient_homes()` の全 root**
の論理 snapshot を照合する (R1 は deny 集合が複数なのに 1 件しか監視せず、fence env を失った
child が他方へ書けた = finding_2)。

**これは acceptance 7 の一次証拠ではない** (上記のとおり ledger が担う)。OS sandbox でもない:
同じ code path が macOS と Linux CI で同一に走る (`sandbox-exec` は Linux に無い)。

### guarded tier

| tier | 内容 | 捕まえる形 |
| --- | --- | --- |
| `entries` | home 直下の child 名集合 | 新しい store / lock / credential file の出現 |
| `schema` | 全 SQLite の `PRAGMA user_version` + schema object 集合 | #14477 の v7→v8 |
| `identity` | 全 SQLite の table ごと row 数 + `registry.sqlite` の `workspace_id` 集合 | #14741 の row 挿入 |
| `backups` | `backups/` 配下の相対 path 集合 | migration の pre-write backup 痕跡 |
| `existence` | home 自体の有無 | test process が operator home を新規作成した |

snapshot は **値を持たない** (digest と count のみ。journal に記録されるため)。

### 読み取りは transactionally consistent、`immutable=1` は禁止 (acceptance 5)

各 store は `file:<path>?mode=ro` で開き、online backup API で in-memory DB へ複写して読む。
backup は read transaction 内で走るので、他 process が書いている live DB でも point-in-time で
一貫する。`immutable=1` は使わない — 「file は変化し得ない」は稼働中 cockpit では偽で、torn
read を schema 変更として誤報する。読めなかった store は `unreadable` として **guard を fail**
させる (「見られなかった」を「変わっていない」と読み替えない)。

### backstop 側の既知の限界 (hook が覆う範囲では無関係)

- SQLite sidecar (`-journal` / `-wal` / `-shm`) は `entries` tier から除外する。任意の
  process が transaction を開くだけで出現・消滅し、**guard 自身の read も含む**ため。
- row の内容は比較せず row 数を比較する。operator の cockpit が既存 row の timestamp を
  継続的に更新するため。**この 2 つの carve-out で見逃す形は、いずれも hook が作用前に
  拒否する**ので、acceptance の根拠が carve-out に依存しない。
- guarded tier は operator 自身の live cockpit も動かし得る (lane 起動が registry へ 1 行)。
  guard は出所を判定せず tier を報告する。red の attribution は人間が行う。

## hook が届かない面の catalog (j#100410 item 3)

`sys.executable` 経由の child は hook を継承する。次は継承しない:

- **hardcode された `python` / `python3`** — `env={}` では PATH も無く `os.defpath` の
  system interpreter が選ばれる。**実測: corpus に 0 件。**
- **test が自前で作る venv の interpreter** — 実測 3 file
  (`test_scaffold.py` / `test_handoff_typed_outcome_cli_smoke.py` /
  `test_issue_13733_shard_env_hermetic.py`)。いずれも wheel install / console script の
  検証で、write 先は自分の temp venv であって mozyo-bridge home ではない。

両方を `InterpreterBypassCatalogTest` が機械的に検査する。**新しい bypass spawn が入ると
test が落ちる**ので、silent に穴が広がらない。これは syntactic な tripwire であり、
列挙された file が安全であることの証明ではない (その判断は各 test の著者が持つ)。

## 3 つの入口 (どれも同じ fence を使う)

| command | 何が isolated になるか |
| --- | --- |
| `mozyo-bridge tests run [-- <unittest args>]` | focused / full の正規入口。**task venv の interpreter**で **literal** `python -m unittest <args>` を走らせる (引数省略時 `discover -s tests`)。test 集合と verdict は serial 正本そのもの — 再実装ではない |
| `mozyo-bridge tests profile` | in-process discovery なので、handler が最初に自分自身を fenced child へ re-exec する。CI full lane の入口はこれなので、CI も同時に isolated になる |
| `mozyo-bridge tests parallel` | shard は既に isolated だったが **parent** の authoritative discovery が operator home 解決下で全 test module を import していた。parent も自分自身を re-exec し、shard はその fence を継承する |

re-exec は `argparse` namespace から引数を再構成しない。`cli.main` が parse した argv を
`args.invoked_argv` に記録し、handler はそれに hidden marker `--already-isolated` を足して
replay する。`sys.argv` を使わないのは、`main()` が test から programmatic に呼ばれたときに
host process の argv を replay してしまうためである。`invoked_argv` が無い呼び出し
(handler を直接呼ぶ in-process caller) は re-exec せず in-process で走る。CI rail がその
branch に落ちて隔離を **黙って失わない**ことは
`tests/integration/e_150_quality_architecture/f_150_ci_verification/test_test_home_isolation_runner.py`
の `IsolatedByDefaultThroughTheCliTest` が pin する。

child は `python -c <bootstrap>` で起動し、bootstrap が絶対 path の package dir を
`sys.path` へ挿入してから CLI `main` を呼ぶ。`PYTHONPATH` は **注入しない** — #13735
j#78390 F1 (`src/` が nested `pip install` へ漏れて install を skip させ、serial/parallel
verdict が割れた) と同じ穴を開けないため。

### `tests parallel` shard との差 (`HOME` の扱い)

shard は #13733 の acceptance どおり **per-shard `HOME` を pin し続ける**
(`test_issue_13733_shard_env_hermetic.py` が pin 済み)。本 rail は pin しない。2 rail は
`HOME` の 1 点だけで異なり、それ以外 (`MOZYO_BRIDGE_HOME` / temp / `XDG_*` / lane pin 除去 /
fence binding) は一致する。#13733 の documented acceptance を壊さないための意図的な
divergence であり、oversight ではない。

## `--no-isolate` (operator/debug escape hatch)

3 入口すべてが `--no-isolate` を持つ。fence も guard も張らずに走り、stderr に
「これは verification record ではない」と告知する。**通常の verification でこれを選ばない。**

**CI / pre-commit / release verification / documented standard command からは選ばない**
(j#100402 item 3)。pre-commit hook は `tests run` を持たない CLI を解決した場合、repo 同梱
source CLI へ切替を試み、それも不可なら **hook を fail させる** — R1 の「warning を出して
非隔離 `python -m unittest` へ fallback」は review j#100408 finding_3 で退けられた。warning は
shared-home mutation を防がない。

## 対象 inventory (acceptance 6)

base `f32c19c2` 時点の実測。**恒久に残るのは数値ではなく測る command である**
(数値は本件と無関係な test の増減でも動く。#14662 j#92449 の D2 policy correction と同型)。

```sh
# 引数なし store / fence / ledger 構築
grep -rnE '\b[A-Z][A-Za-z]*(Store|Fence|Ledger|Registry|Inventory)\(\)' tests/
# env clear
grep -rl 'clear=True' tests/
```

| 集合 | 実測 (base `f32c19c2`) | issue 本文の snapshot |
| --- | ---: | ---: |
| 引数なし構築を持つ module | 23 | 11 |
| 同 occurrence | 75 | — |
| `clear=True` を持つ file | 35 (本 issue の新規 2 file を除く) | 20 |

内訳 (occurrence 上位): `LaneLifecycleStore()` 46 / `RouteIdentityLedger()` 9 /
`LaneDeclarationStore()` 5 / `RecordingStore()` 3 / `InMemoryLedgerStore()` 3 /
`BuiltinProviderRegistry()` 3 / `DispatchOutboxFence()` 2 /
`AgentProviderProfileRegistry()` 2 / `LaneReconcileBindingStore()` 1 /
`BuiltinCliModuleRegistry()` 1。

subsystem の広がり (`clear=True` file の所在): `unit/e_110_execution_platform` 8 /
`unit/e_140_adapter_provider` 6 / `integration/e_110_execution_platform` 6 /
`regressions/**` 9 / `scenarios/**` 2 / `integration/e_150_quality_architecture` 2 /
`integration/e_130_governance_distribution` 1 / `unit/e_150_quality_architecture` 1 /
`support/**` 1。

**issue 本文の 11 / 20 は起票時 (#14757 作成 2026-07-30) の snapshot であり、現行値では
ない。** 本 rail は個々の call site を書き換えて数を減らす方式ではなく、**どれだけ増えても
process 境界で覆う**方式なので、この増加は rail の前提を壊さない。個別 call site の
`home=` 明示化は本 issue の scope 外である (#14757 non-goals: test 期待値を ambient
operator state へ合わせること)。

## 本 rail が緩めないもの

- **test 集合と verdict は serial `python -m unittest discover -s tests` が正本。** `tests run`
  はその literal command を fence 下で走らせるだけで、discovery を再実装しない。
- **green な suite は隔離の証拠ではない。** run の verdict は suite verdict **と** home guard
  の連言である。片方だけを報告しない。
- **guard は read-only。** operator 共有 state を修復・削除・downgrade しない。
- **拒否層の install 失敗は run の拒否である。** 検証できない run を「隔離済み」と報告しない。
- **injection channel は「拒否が起きたか」で検証する。** 「module が読まれたか」で判定すると
  棄却案 2 の failure mode (shadow されて無防備なのに成功報告) を再生産する。
- **`immutable=1` を mutable evidence に使わない。** 読めなければ `unreadable` として fail する。
- **`HOME` を隔離の道具にしない。** 隔離は canonical resolver が担う。
- **production は unfenced のまま。** fence env の無い process の home 解決は byte-invariant。

## 正本実装

- pure core: `src/mozyo_bridge/e_150_quality_architecture/f_150_ci_verification/domain/test_home_isolation.py`
  (pin の決定 / snapshot 値オブジェクト / tier 比較 / deny ledger / run outcome の合成)。
- 作用前拒否: `.../application/test_home_audit_hook.py` (task venv + `.pth` + audit hook
  生成 / install 検証)。
- resolver 側 fence: `src/mozyo_bridge/shared/paths.py` (`HomeFence` /
  `OperatorHomeFenceViolation` / `mozyo_bridge_home`)。
- I/O: `.../application/test_home_fence.py` (task root 生成 / consistent snapshot / re-exec)。
- handler: `.../application/commands_test_run.py` (`cmd_tests_run` / `guarded_isolated_run` /
  `isolate_self`)。
- registrar: `.../application/cli_test_run.py` (`tests run` と共有 isolation flag)。

## 検証

- `mozyo-bridge tests run -- <targets>` / `mozyo-bridge tests run` (full)
- `mozyo-bridge tests profile --slowest 20` (CI full lane と同一 command)
- `mozyo-bridge tests parallel`
- regression: `tests/regressions/test_issue_14757_test_process_home_isolation.py`
  (negative probe を含む)
- unit: `tests/unit/e_150_quality_architecture/f_150_ci_verification/test_test_home_isolation.py`
- integration: `tests/integration/e_150_quality_architecture/f_150_ci_verification/test_test_home_isolation_runner.py`
- `mozyo-bridge docs validate --repo .` / `--check-file-coverage` /
  `docs generate-file-conventions --repo . --check`
- `mozyo-bridge health check`
