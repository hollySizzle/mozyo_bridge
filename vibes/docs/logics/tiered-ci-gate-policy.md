# Tiered CI Gate Policy

Redmine #13734 (parent US #13732 `ローカル並列全件テストと段階的CIゲートで検証待ちを短縮する`,
Version `モジュール分割・テスト影響範囲整備枠`)。全 branch push で Python
3.10–3.13 の full matrix を反復していた CI を、**risk tier 別の gate** へ再編する
設計正本。`.github/workflows/test.yml` / `testpypi.yml` / `publish.yml` の trigger
routing と pre-publish 機械 gate を束ねる。

CI 内の test authority は引き続き `unittest discover` / `mozyo-bridge tests profile`
(authoritative serial runner)。本 issue は #13733 の local 並列 runner の未確定
interface には依存しない (`## 非目標`)。

## 出発点 (verified baseline, #13732)

- `test.yml` は既に **parallel** に 3.10–3.13 を回す。最適化対象は *並列化不足* では
  なく、**全 branch push での発火頻度**と **4-environment 重複 build** である
  (#13734 j#77169 invariant #1)。
- `publish.yml` は `release: published` で単一 Python の build+publish のみ。full
  matrix / artifact / fresh-install の機械 gate が無く、production の健全性が policy
  文書の手動前提に依存していた。
- `testpypi.yml` は #13601 の exact-SHA data gate (SHA / version mirror / lineage /
  prior Test success / uniqueness) を持つが、**publish 直前に suite を自ら回す機械
  gate** が無かった。

## Tier 構成

| tier | trigger | runner | gate 内容 |
| --- | --- | --- | --- |
| quick | `pull_request` / issue-branch `push` (branch ref のみ) / manual `quick` dispatch | single Python 3.12 | health + docs + **affected** tests (fail-closed → whole suite) |
| integration | `push` to `main` / `int_*` / `integration_*` | clean Linux single Python 3.12 | health + docs + **full** `unittest discover` + wheel/sdist build + fresh-install smoke、**exact SHA に 1 回** |
| testpypi | `workflow_dispatch` (exact candidate) / `workflow_run` (main Test success) | single Python 3.11 | #13601 data gate 群 + **inline clean single-Python full + artifact/install smoke** (両 event) |
| nightly | `schedule` (07:00 JST) | 3.10–3.13 matrix | full `unittest discover` + wheel build |
| production | `release: published` | 3.10–3.13 matrix (verify) + single (build) | exact release/tag SHA の **full matrix** + tag↔version mirror + wheel/sdist + **fresh-install smoke**、OIDC publish は別 job |

### issue-branch push を full matrix から外す (invariant #2)

`test.yml` の routing:

- `pull_request` → `quick` のみ。
- issue-branch `push` (= `main` でも `int_*` / `integration_*` でもない ref への push)
  → `quick` のみ。**issue-branch push は full Python matrix を auto 発火しない**。
- integration `push` (`main` / `int_*` / `integration_*`) → `integration` batch のみ。
- `schedule` → `full-matrix` (nightly)。
- `workflow_dispatch` → `lane` input で `full` (既定) / `quick` を選ぶ。明示 full が
  唯一の on-demand full matrix 経路。
- **tag `push` → どの job も発火しない** (下記 `### tag push は test.yml の tier ではない`)。

### tag push は test.yml の tier ではない (#13735 j#78390 F2 correction)

`test.yml` は **branch push だけ**を受ける。`on.push.branches: ["**"]` は branch
**allowlist** であり、`branches` filter を持つ push event は branch ref にしか match
しないため `refs/tags/*` は workflow に入らない。tag pattern の denylist ではなく
allowlist にしてあるので、将来 tag 命名が増えても構成上除外されたままになる。

二層目として `quick` の `if` と concurrency `cancel-in-progress` は
`startsWith(github.ref, 'refs/heads/')` を要求する。すなわち quick の push arm は
「branch であり、かつ integration branch でない」で定義され、「main でも `int_*` でも
ない」だけの否定条件で **tag が既定で quick に落ちる** ことはない。trigger filter が
将来緩んでも job 層が fail-closed で弾く (片側だけの guard にしない)。

release tag の authority は `publish.yml` (`release: published` + immutable
`github.sha` pin) にあり、`test.yml` は tag に対して何も主張しない。

> 修正前の実挙動: `on.push` に ref filter が無く、`quick` の `if` が branch ref のみを
> 除外していたため、`push refs/tags/v0.11.0` は `quick` を発火し (`cancel-in-progress`
> も true)、tier 表に無い run が release tag ごとに走っていた。under-tested な publish
> path ではなかったが、policy 表と実装の drift であり #13735 j#78390 F2 / j#78399 で
> correction 対象になった。regression は tag ref を trigger filter (YAML source) と
> job routing (if-式評価) の両層で固定する。

quick lane の affected 解決は #12752 の pure resolver を local と共有する。base は PR
なら merge target、push/manual quick なら `origin/main`。issue branch を `origin/main`
に対して diff すると integration base の差分も含めて **over-select** することがあるが、
これは安全側 (single Python で多めに回す)。base が解決不能なら resolver が fail-closed で
whole suite (single Python) を推奨する (silent な空集合 = fail-open を出さない)。

### integration batch (invariant #2)

integration ref への push で **exact integration SHA に 1 回** だけ、clean Linux
single-Python full + health/docs + wheel/sdist build + fresh-install smoke を回す。
これが自動 TestPyPI dev path (`testpypi.yml` の `workflow_run`) が key にする `main` の
`Test` success でもある。full matrix (4 環境) は integration tier では回さず、nightly /
production tier に寄せる。

### nightly (invariant #4)

`schedule` は 3.10–3.13 full matrix を維持する。日次で supported Python 全環境の
全件保証を担保する安全 lane。

## TestPyPI pre-publish 機械 gate (invariant #3)

`testpypi.yml` の build job は、**manual / auto 両 event** で publish 直前に inline で
clean single-Python full + health/docs + artifact/install smoke を回す。prior な
`Test` run を *信頼するだけ* にせず、**exact checked-out SHA で suite を自ら実行**する
ことで、manual dispatch の gate 抜け (green だが partial な上流 signal が publish へ
到達する経路) を塞ぐ。

保存する #13601 の性質:

- workflow 定義 / event ref は `main` 固定。artifact authority は exact `source_sha`。
- build job は `id-token` を持たず、OIDC (`id-token: write` + `environment: testpypi`)
  は publish job のみ。inline full/smoke を追加しても OIDC surface は build に触れない。
- exact SHA / version mirror / source_ref lineage / candidate `test.yml` == trusted
  main / 同 SHA `Test` success / version uniqueness の data gate 群は温存する。

4-version matrix は TestPyPI tier では **不要** (#13732 owner direction)。clean
single-Python full + artifact/install smoke で足りる。

## Production 機械 gate (invariant #4)

`publish.yml` は production publish を **publish workflow 自身で機械確認**する:

- `verify` job (matrix 3.10–3.13): **immutable な `github.sha`** (release-event の
  tagged commit、trigger 時に固定) を checkout し、`HEAD == github.sha` を
  fail-closed 照合。tag は **`v` prefix 必須** (release-flow.md `## Tag and Release`
  の canonical rule) で、非 `v` tag は fail-closed。`tag ↔ 2-file version mirror` の
  一致を検査してから full `unittest discover` + health gate を supported Python 全
  環境で回す。tag↔version mirror 一致が「artifact が tag に対応する」ことを機械保証
  し、policy 文書の手動前提を置き換える。
- `build` job: **同 immutable `github.sha`** を checkout し、build job 自身でも
  `HEAD == github.sha` を fail-closed 再照合してから wheel+sdist を build し、built
  artifact の fresh-install smoke (`mozyo-bridge` / `mozyo` entry point) を回して
  artifact を upload する。`verify` 成功後のみ。**artifact authority は mutable な
  tag 名でなく immutable SHA に固定**し、`verify` 完了後に tag が force-move されても
  未検証 commit が publish へ渡らない (Redmine #13734 j#77258 F1 TOCTOU 対策。
  `testpypi.yml` build job の exact-`source_sha` 照合と同じ doctrine)。
- `publish` job: `id-token: write` + `environment: pypi` を持つ**唯一の** job。
  verified artifact を download して upload するだけで、checkout/build/verify surface に
  OIDC credential が触れない (#13601 の OIDC 境界を production にも適用)。

trigger は `release: published` のまま。tag push だけでは発火しない。

## Disposable Ubuntu container smoke (#14100, parent #14098)

`testpypi.yml` / `publish.yml` の **build job** は、venv fresh-install smoke の
後・`Upload built distributions` の前に、同一 built wheel を **使い捨ての pinned
Ubuntu container** で black-box する。job 順序は **build → container smoke → upload**
のままで、`publish` job (OIDC 唯一) は従来どおり `needs: build`。container smoke は
build job 内で走り `id-token` を持たないため、#13601 の OIDC 境界を弱めない。

- **reusable 実装**: `scripts/disposable_ubuntu_smoke.py` (stdlib-only、依存なし)。
  container 内 program は **stdin 経由**で渡し、repo tree は bind-mount しない。
  artifact-only directory (`dist/`) だけを `/artifacts:ro` で read-only mount する。
- **blocking authority は image DIGEST**。default `blocking` mode は `--image` に
  digest pin (`ubuntu@sha256:<64-hex>`) を **必須**とし、floating tag を fail-closed で
  拒否する。よって「動く image」が gate になり得ない (mechanical)。
  - 現行 pin: `ubuntu@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90`
    (Ubuntu 24.04 LTS、`docker inspect` RepoDigests、取得 2026-07-20)。
    workflow env `DISPOSABLE_UBUNTU_IMAGE` と本 pin は **lockstep** で更新する。
- **非root / fresh HOME / source 不在 / install provenance は assertion**であり説明文
  ではない: container は root で OS provision (apt python3/venv) した後 **非root user
  へ drop** し、その user (`id -u != 0`)・fresh `HOME`・venv 配下の package path・
  両 console script の `--version == expected` を機械照合する。repo checkout は
  container に一切現れない。**source 不在は 2 層で検証する** (#14100 review j#82881
  F1 / j#82888): host 側で docker argv の mount を **全構文** (`-v` / `--volume` /
  `--mount` の separate・`=` 形、および `--tmpfs` / `--volumes-from`) 正規化して集合が
  `artifact_dir:/artifacts:ro` ちょうど 1 件である事と artifact-only directory 境界
  (distribution file のみ) を fail-closed 検証し (等価 mount 構文での迂回を封鎖)、
  container 側で `/proc/self/mountinfo` を **filesystem type で観測**して pseudo-fs
  (proc/sysfs/cgroup/tmpfs/devpts…) と allowed exact (`/`・`/artifacts`・
  `/etc/{resolv.conf,hostname,hosts}`) 以外の実 fs mount が無い事を `mount_isolation`
  surface として観測 (path prefix でなく fstype 判定ゆえ `/dev`・`/proc`・`/sys` 配下へ
  bind された host source も検出。自己申告の固定値でない)。両層が揃って初めて
  `source_mount_absent_verified` が真となり verdict を通す。
- **既存 venv fresh-install smoke との差分 (重複でなく追加価値)**: venv smoke は build
  runner 上で runner user として `--version` / `--help` を叩くだけ。container smoke は
  (1) OS 境界 (pinned Ubuntu LTS)、(2) 非root user + fresh HOME、(3) source checkout 不在、
  (4) 実 user harness (`rules install/status`、fresh target への `scaffold apply/status`、
  `docs validate/resolve`、read-only `doctor runtime`) を横断する。
- **machine-readable summary**: image ref/digest、wheel 名 + sha256、expected/observed
  version、runtime user/uid、fresh HOME、source-mount 不在、各 surface の pass/observed、
  duration を secret-safe な JSON で出力し job summary へ貼る。credential は image /
  container env / summary のいずれにも入れない。
- **quick lane 非常設**: `test.yml` (issue-branch push / PR quick lane) には接続しない。
  build→publish の release path (TestPyPI acceptance / production prepublish) のみ。
- **floating LTS canary は blocking verdict と分離**: script は advisory な `canary`
  mode (floating tag 許容) を持つが、release workflow には blocking gate としてのみ
  digest-pinned `blocking` mode を接続する。floating canary を常設する場合は必ず別 job
  かつ非 blocking (verdict が release blocker へ昇格しない) とし、blocking mode の image を
  floating tag で override できる曖昧な default を作らない。canary 常設の費用対効果は
  未決 (#14100 未確認事項) のため、現行は script mode + focused test として提供し、
  recurring な floating job は release path に足さない。

## Concurrency / provenance (invariant #5)

- `test.yml`: `test-<workflow>-<event>-<ref>` group。PR 更新と issue-branch push は
  `cancel-in-progress` で superseded run を止める。integration (`main` / `int_*` /
  `integration_*`) / nightly / manual dispatch は **cancel しない** (各 integration
  SHA / scheduled sweep / on-demand run が固有の verdict を保つ。自動 TestPyPI dev
  path が COMPLETED main `Test` run の head_sha を key にするため、main run を
  cancel すると gate が落ちる)。cancel 判定は `refs/heads/` prefix を要求するため、
  tag ref は (trigger filter で既に除外されているうえ) cancel 対象クラスにも入らない。
- `testpypi.yml`: `expected_version` (manual) / triggering `head_sha` (dev) 別に
  serialize、`cancel-in-progress: false`。
- `publish.yml`: `publish-pypi-<tag>` で release tag 別 serialize、
  `cancel-in-progress: false` (production publish は決して cancel しない)。
- run summary provenance: quick / integration / production の各 lane は
  `$GITHUB_STEP_SUMMARY` に event / ref / exact SHA / Python / gate 内容を出す。
  TestPyPI は #13601 由来の dev/exact summary を維持する。

## 現行 green 証跡の非無効化 (Acceptance)

本変更は変更 commit 以降に適用し、現行の active issue branch の CI 証跡を遡って
無効化しない。tier routing は event/ref で分岐するのみで、既存 SHA の過去 run を
再評価しない。

## 非目標

- test runner の差し替え (CI authority は `unittest discover` / `tests profile` の
  まま。#13733 の local 並列 runner interface には依存しない)。
- production publish / GitHub Release / tag / version bump の実行 (本 issue は gate
  設計のみ。publish 自体は行わない)。
- slow-test budget enforcement の CI default 有効化 (#12754 のまま opt-in)。
- 外部 environment 設定 (required reviewer / deployment branch policy) の変更。

## 関連

- `vibes/docs/logics/ci-quick-full-lane-policy.md` — quick/full lane split (#12753)
  の原設計。本 doc の quick / full 語彙と affected resolver 共有はここを継承する。
- `vibes/docs/logics/release-flow.md` — release / TestPyPI / production の運用手順と
  guardrail。`## Production Release Gate` はここを正本とし、本 doc は publish workflow が
  その gate を機械化する形を定義する。
- `vibes/docs/logics/release-helper-contract.md` — version mirror set の正本。
  `test.yml` / `testpypi.yml` / `publish.yml` の inline mirror check はこれと lockstep。
- `vibes/docs/logics/test-runtime-profiling-policy.md` — `tests profile` の runtime
  summary と suite verdict 正本性。
- `scripts/disposable_ubuntu_smoke.py` — 使い捨て Ubuntu container black-box smoke
  の reusable 実装 (#14100)。本 doc `## Disposable Ubuntu container smoke` が接続点と
  digest pin authority を定義し、script は blocking / canary mode を実装する。
