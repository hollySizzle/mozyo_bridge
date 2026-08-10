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

## 作用前拒否 — OS 層 write 境界（正本層）

### R1 / R2 の主張を撤回する

| round | 私が境界と呼んだもの | 破られ方（実測） |
| --- | --- | --- |
| R1 | 変更を検出して run を red にする | acceptance 4「write attempt を拒否」/ 7「変更 0」を満たさない。row は着地したまま（j#100407 R1-F1） |
| R2 | task venv の `.pth` audit hook（in-process） | `os.open(..., dir_fd=<denied home fd>)` で **rc 0 / bytes 変化 / ledger 空**。`<fence python> -S -c` でも同じ（j#100417） |

**R2 の破れは hook の bug ではなく層の誤りである。** CPython の `open` audit event の引数は
`(path, mode, flags)` で **`dir_fd` を含まない**ため、相対 path がどこへ解決するかを hook は
知り得ない。`-S` は `site` を skip するので `.pth` 自体が読まれない。**どちらも hook 内では
閉じられない。**

**撤回する記述**: 旧版の「新しい bypass spawn が入ると test が落ちるので silent に穴が
広がらない」は **成立しない**（下記 `## interpreter bypass catalog`）。

### acceptance 3 の読み直し

acceptance 3「macOS `sandbox-exec` **だけに依存せず** Linux CI 相当境界を持つ」は、
**OS 層 enforcement を前提に「片 OS 依存にするな」と言っている**。R1 はこれを「OS sandbox を
使うな」と読んだ。**誤読だった**（j#100419）。

### 現行の境界

| platform | backend | 状態 |
| --- | --- | --- |
| macOS | `sandbox-exec -f <profile>`（`allow default` + 全 denied root へ `deny file-write*`） | **実測済み** |
| Linux | `bwrap --ro-bind / / --dev /dev --perms 01777 --tmpfs /tmp --bind <task-root> <task-root>` + canary / private-tmp denied mask | **修正後の exact-head CI で実証するまで未実測**（`OsFence.verified=False`） |

Linux は host root を再帰 read-only にしてから、namespace-private な `/dev` / `/tmp` と
task root だけを書込み可能にする。R3 の `--dev-bind / /` は host root をread-writeで見せた後、
不存在 denied root を通常の `--ro-bind` destination にした。bubblewrap 0.9.0 は bind 前に
destination を作るため、保護対象をhost上へ作り得た（review j#100507 finding 2）。現行は:

- base read-only領域の denied root は存在・不存在を問わずargvのdestinationへ出さない。
- private `/tmp` 配下のdenied rootだけをprivate tmpfs上でmaskする。不存在なら空tmpfsを
  `--remount-ro` し、host側pathは作らない。
- task rootと実denied rootが祖先・子孫として重なる設定は、write holeとdenyの矛盾なので作用前拒否する。
- task root内の既存canaryだけは最後にread-only bindし、controlはtask root内・canary外で書込み可能に保つ。
- bare Linux checkはbwrap実行中にchildをholdし、その時点と終了後の両方で不存在host rootが
  absentのまま、childの作成が`EROFS`で拒否されたことを確認する。argv検査を作用証拠にしない。

- **`unshare` 等の未実証 fallback へ自動退避しない**。backend が無い / self-check が通らない
  場合は `OsFenceUnavailable` で **test を 1 件も実行せず拒否**する。in-process guard への
  degrade は review j#100417 が退けた選択肢である。
- **毎 run self-check する**（`verify_os_fence`）。**4 probe** を 1 つの境界の下で走らせる:
  `dir_fd` / `no_site_sqlite` / `base_executable_sqlite` の 3 つが **その backend の
  exact errno**（Seatbelt=`EPERM` / bwrap=`EROFS`）で拒否され、**`allowed_control` の
  書き込みは成功する**こと。4 つ目が無いと「全部拒否する壊れた host」を正しい境界と
  読んでしまう。probe は **rc 0 + exact token** で答える契約であり、**非 0 exit は合格条件では
  ない**（境界が apply に失敗した場合も非 0 になる。macOS の nested sandbox-exec は 71）。
  さらに両 victim を read back して不変を確認する。通らなければ run を拒否する。
  R2 の失敗は「成功と報告しつつ無防備」だったので、**境界は「在るか」ではなく
  「今 enforce しているか」を毎回測る**。
- 出力は backend 名と **未実測かどうか**を表示する（`OS write boundary: bwrap (backend NOT
  measured in-repo)`）。**未実測 backend を実測済みとして報告できない形にした。**

実測（macOS、temp fixture。operator home は使わない）:

| bypass | in-process hook | `sandbox-exec` |
| --- | --- | --- |
| `os.open(..., dir_fd=…)` | 貫通（rc 0 / 変化） | **拒否** `PermissionError` / 内容不変 |
| `<python> -S -c` sqlite `UPDATE` | 貫通（rc 0 / 変化） | **拒否** `attempt to write a readonly database` / row 不変 |
| `sys._base_executable` | 貫通 | **拒否** / row 不変 |
| denied root **外**への write | 通る | **通る**（write 境界であって一般 sandbox ではないことの対照試験） |

### `.pth` audit hook は defence in depth / attribution へ格下げ

hook は残すが、**acceptance 3/4/7 の主根拠にしない**。役割は 2 つ:

- **attribution**: 拒否した試行を ledger に記録し「どの test が触ろうとしたか」を出す。
  OS 境界は拒否するが、誰が試みたかは教えてくれない。
- **defence in depth**: OS 境界の内側で、より早く・より説明的に失敗させる。

## snapshot guard — 粗い backstop

run の前後で **`ambient_homes()` の全 root** の論理 snapshot を照合する（R1 は deny 集合が
複数なのに 1 件しか監視しなかった = finding_2）。**acceptance 7 の一次証拠ではない**（OS 境界と
ledger が担う）。tier は `entries` / `schema` / `identity` / `backups` / `existence`。値は持たず
digest と count のみ。

読み取りは `file:<path>?mode=ro` + online backup API の read transaction 内複写で行い、
**`immutable=1` は使わない**（稼働中 cockpit では「変化し得ない」が偽で torn read を schema
変更と誤報する。acceptance 5）。読めない store は `unreadable` として guard を fail させる。

sidecar（`-journal` / `-wal` / `-shm`）除外と row 数比較の carve-out は残るが、**それが見逃す形は
いずれも OS 境界が作用前に拒否する**ので、acceptance の根拠が carve-out に依存しない。guarded
tier は operator 自身の cockpit も動かし得るため、red の attribution は人間が行う。

### 監視範囲を狭めない（j#100487）

`identity` tier は **全 user table の row count** を含む。高 churn な append table を
「authority table allowlist」へ絞る案は **撤回された**: 除外した table こそ test 由来 append が
隠れる場所であり、guard の検出面を弱める。**成功条件も監視範囲も緩めない。**

代わりに **報告の粒度**だけを上げる。identity delta は動いた `store/table` と row count を
`43->44` の形で述べる（`HomeDelta.detail`）。**値は出さない** — row 値・workspace id・秘密値は
detail に含めない。row count が同一で digest だけ動いた場合は
`registry workspace identity set changed` とだけ述べ、**id を列挙しない**。

**red の読み方**: snapshot は粗い backstop なので、**外部 writer（operator の cockpit、他 lane）
でも red になり得る**。一次証拠は **verified OS fence + clean ledger**（= この process tree が
何も試みなかったという attribution）であり、snapshot red は「誰かが動かした」までしか言わない。
反復測定の最終証跡は、**外部 writer を静止させた環境か clean CI** で取る。**単純な自動 retry で
流さない。**

## CI は OS 境界を install / check する（非隔離 fallback なし）

`tests run` / `tests profile` は OS 境界が無ければ **test を 1 件も実行しない**ので、境界は
CI の **hard prerequisite** である。したがって suite を走らせる **全 job** に 2 step を置く
(j#100430):

| workflow | job | install | check |
| --- | --- | --- | --- |
| `test.yml` | `quick` / `integration` / `full-matrix` | ✓ | ✓ |
| `publish.yml` | `verify` | ✓ | ✓ |
| `testpypi.yml` | `build` | ✓ | ✓ |

- **install**: runnerを`ubuntu-24.04`へ固定し、`apparmor` / `apparmor-profiles` / `bubblewrap`を
  packageからinstallする。Ubuntu同梱の
  `/usr/share/apparmor/extra-profiles/bwrap-userns-restrict` だけを`apparmor_parser`で明示loadし、
  `bwrap (enforce)`と最小live smokeを確認する。Ubuntu 24.04の
  `kernel.apparmor_restrict_unprivileged_userns=1` はload前後で維持し、global制限を無効化しない。
  初回run `31129109147` はprofile未loadのため4 probeすべてがuid map作成前に拒否された。
  fail-closedは正しいがLinux合格ではないため、修正headのgreenを必須とする。
- **check**: `OsBoundaryRefusesEveryKnownBypassTest` を suite の **前**に走らせる。境界が
  install 済みでも enforce していない場合に、suite の green が黙って意味を失うのを防ぐ。
  同じbare stepで不存在denied rootのlive probeも実行し、host pathを実行中・終了後に照合する。
  この step は境界そのものを検査するので、**意図的に fence の外**で `python -m unittest` を
  直接呼ぶ。suite を非隔離で走らせる fallback ではない（自前の temp fence だけを使い、
  operator home に触れない）。
- **非隔離 fallback は CI にも hook にも存在しない。** pre-commit は `tests run` を持つ CLI が
  解決できなければ **fail** する（j#100408 finding_3）。さらに **repo source を installed CLI より
  優先**する（j#100490 item 3）。PATH 上の古い build は `tests run --help` に答えられてしまい、
  **今 commit しようとしている契約とは別の（弱い）契約で「隔離した」と報告し得る**ためである。
  operator が明示した `MOZYO_BRIDGE_CMD` は引き続き優先される。resolve と run は同一 CLI を使う。
- **既定出力に絶対 path を出さない**（j#100490 item 4）。verdict は Redmine journal や CI log に
  貼られるため、`guarded-home[0] sha256:...` の **role / ordinal / digest** で識別する。
  絶対 path は **`--reveal-paths`（local debug 専用）** でのみ出る。
- step 名は `Verify` を避けて `Check` にしている。#13601 は `testpypi.yml` の `Verify` を含む
  step を release gate として集め `workflow_dispatch` 限定を要求するが、本 check は release
  gate ではなく全 event で走る必要がある。**改名すると `test_issue_13601_testpypi_exact_sha`
  が落ちる。**

## interpreter bypass catalog — 補助であって保証ではない

`InterpreterBypassCatalogTest` は corpus 上の bare `python` spawn（実測 0 件）と自前 venv 構築
（実測 3 file）を走査する。**これは syntactic な tripwire であり、bypass の不在証明ではない。**
`sys._base_executable` / `-S` / console script / 非 Python process は catalog では閉じられず、
**それらを閉じるのは OS 層境界である**。旧版がここに書いていた「新しい bypass が入れば必ず
test が落ちる」は **撤回した**（j#100419）。

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

bootstrap が挿入するのは **呼び出した CLI 自身の** package dir (`runtime_root()` =
`mozyo_bridge.__file__` の親の親) である。したがって re-exec する 2 入口 (`tests profile` /
`tests parallel`) では **どの CLI で起動したかが、どの runtime を検証したかを決める**。
installed binary から起動すると installed package が検証対象になり、`mozyo_bridge` は discovery
より前に import 済みになるので、test module 側の `sys.path.insert(0, ROOT / "src")` は既に
import 済み package の `__path__` を変えられず **inert** になる (#15229 実測: worktree より古い
installed から `tests parallel` を起動し、worktree 固有 symbol を参照する 13 module が collection
import error で fail-closed した)。`tests run` は fenced child が literal `python -m unittest` を
新しい interpreter で起動するため事前 import がなく、corpus 規約どおりに解決する。worktree の
`src/` を検証する invocation の正本は `local-parallel-test-runner-policy.md` の
`### runtime provenance` に置く (本 doc に複製しない)。

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
- **境界の不在・self-check 失敗は run の拒否である。** 検証できない run を「隔離済み」と報告しない。
- **未実測 backend を実測済みと報告しない。** `OsFence.verified` が False の backend は出力と
  journal の両方で未実測と明示する。
- **injection channel は「拒否が起きたか」で検証する。** 「module が読まれたか」で判定すると
  棄却案 2 の failure mode (shadow されて無防備なのに成功報告) を再生産する。
- **`immutable=1` を mutable evidence に使わない。** 読めなければ `unreadable` として fail する。
- **`HOME` を隔離の道具にしない。** 隔離は canonical resolver が担う。
- **production は unfenced のまま。** fence env の無い process の home 解決は byte-invariant。

## 正本実装

- pure core: `src/mozyo_bridge/e_150_quality_architecture/f_150_ci_verification/domain/test_home_isolation.py`
  (pin の決定 / snapshot 値オブジェクト / tier 比較 / deny ledger / run outcome の合成)。
- **作用前拒否（正本層）**: `.../application/test_home_os_fence.py` (macOS `sandbox-exec` /
  Linux `bwrap` の境界解決と毎 run self-check)。
- attribution / defence in depth: `.../application/test_home_audit_hook.py` (task venv +
  `.pth` + audit hook 生成 / install 検証)。
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
