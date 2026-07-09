# herdr 高忠実度 fake + 古典派 scenario テスト基盤の設計 (Redmine #13398)

親 Feature `120_シナリオ・受入テスト基盤` (#12531)。本 doc は **設計正本**であり
production/test code は含まない (実装は §5 の分割 US が所有する)。既存の
`120_シナリオ` 基盤 (`tests/scenarios/` + `tests/support/`, #12491) を置き換えず、
その **backend-routing 面への拡張**として位置づける。

## 0. 問題定義 (coordinator 診断、迎合なし)

herdr 関連バグが review / live smoke で繰り返し発見される真因は「実機必須」ではなく
**カバレッジの遅れ**である。backend-aware な新コードを足すたびに、テストの fake/stub が
実 backend の解決経路をモデル化しておらず、tmux/herdr の分岐が素通りする。今日の主な
バグはすべて**決定論的** (入力が決まれば結果が決まる) で、古典派テストで事前に潰せた:

- #13377 j#73640 `target_repo_mismatch` (explicit-lane 合成 cwd = sender repo)
- #13379 j#73711 config-only marker が root 推定に不在 (child cwd で fail)
- #13379 j#73722 / #13397 finding 1 — dispatch-worker inner-send が herdr pane を
  tmux target 誤認 (`target_unavailable`)

いずれも「実機でしか出ない」のではなく、**新コード経路にテストが追いついていなかった**
だけである。config marker バグ (#13379 j#73711) は auditor が pure python 再現スクリプトで
捕捉→回帰テスト化した = 古典テストで捕れた証拠。

### 0.1 なぜ既存 scenario 基盤では捕れないか (核心)

既存の `tests/scenarios/test_delegated_coordinator_route_plan.py` (#12491) は Detroit-school
scenario の良い先例だが、**side-effecting boundary を高い位置で fake する**:
subject-under-test は pure planner (`plan_delegated_coordinator_route`) で、fake は
「executor が *何をするか* を記録する」`support.delegation_route_fakes` である。

今日のバグはすべてこの fake boundary の **下** に住む — `repo_root_from_args` →
`load_repo_local_config` → `herdr_effective_backend_selected` → route authority → rail 選択
という **backend 解決の実経路**であり、planner の上位判断ではない。既存 scenario は
executor を fake するので、この実経路を一度も実行しない。よって finding 1 のような
「backend 予測が cwd と食い違う」バグは構造的に不可視である。

**本 US の設計目標**: fake boundary を **backend 解決経路の直下** (= 実 herdr CLI 面) まで
押し下げた高忠実度 herdr fake を用意し、scenario harness が実 backend-routing seam を
end-to-end で駆動し、各 hop で routing decision を assert する。live smoke を「バグ発見の
場」から「最終確認の場」へ格下げする。

---

## 1. Scope 1 — 既存 herdr test double の棚卸し (matrix)

### 1.1 実 herdr 0.7.1 API surface (モデル化対象の正本)

正本参照: `spec-herdr-native-identity` (§5 launch contract、§6 close-evidence)、
`vibes/docs/logics/herdr-poc-13175-experiment-log.md` (E1〜E14 実測、非 catalog doc)。
fake が忠実に再現すべき面:

| # | API surface | 実測仕様 (正本) | fake が保証すべき契約 |
|---|---|---|---|
| A | `agent start <NAME> [--cwd] [--env]... [--no-focus] [--workspace <id>] -- <argv>` | NAME は必須 positional、start 時直接適用 (`result.agent.name == NAME`); 出力は単一 JSON `type:agent_started`、locator は `result.agent.pane_id`; client env は spawn 先に**届かない** (`--env` 必須); `--permission-mode` は claude argv に #13360 policy で付与 | argv parse + JSON envelope 合成、locator 採番、`--workspace` prefix 一致検証 |
| B | `agent list [--json]` | live inventory の行集合。各行 `name`/`pane_id`(alias `pane`/`location`)。mzb1 名は `mzb1_<ws>_<role>_<lane>` scheme。**recognised-empty ≠ unrecognisable** の区別、malformed 行は skip (非 fatal) | in-memory state から行を render、mzb1 decode 契約に一致、malformed 注入面 |
| C | `agent get <target> [--json]` / `agent rename` | 付与名は `server stop`/restart を越えて永続 (E10); `terminal_id` は per-process 使い捨て | 名前永続、locator は再生成可能な transient として扱う |
| D | `workspace create --cwd --no-focus` | 応答 `type:workspace_created`、`workspace_id` + `root_pane.pane_id`、`pane_count:1` (空 base pane を必ず 1 個生成) | workspace 採番 + root pane 採番、cold-start 経路の base pane 残骸を再現 |
| E | `pane close <pane_id>` → **最終 pane close で workspace 自動消滅** | 実測 #13380: lane ゼロの host workspace は最終 pane close で herdr が自動 close (husk 構造的に生じない) | pane close→(workspace 内 pane 0 なら)workspace 消滅を状態機械で再現 |
| F | `wait agent-status <pane> --status <s> --timeout` | **change-semantics** (E9): 既にその状態でも返らず「その状態への*変化*」を待つ。check-then-wait 必須。started は event 返り ~0.36s (E12)、absent は pane get error (E9 c3)、blocked 中 `wait working` は timeout (E13/E14) | 状態遷移イベントの発火/timeout を決定論的に注入 (時間を実 sleep せず論理 tick で) |
| G | backend routing (config → rail) | `terminal_transport.backend: herdr` が唯一の selector; herdr は `BUILTIN_PROVIDER_REGISTRY` 非登録; explicit `%pane` target は herdr config 下でも tmux rail (#13320) | fake は herdr rail 側の IO のみ供給。routing 判定自体は実 code (seam) を通す |

### 1.2 現行 test double の配置と fidelity (file:line matrix)

現行の herdr test double は **各テストファイルに inline 定義された fake Runner**
(`Runner = Callable[..., subprocess.CompletedProcess[str]]`, `herdr_transport.py:85`) で
ある。**共有 fake infra は無い**: `tests/fakes/` 無し、`conftest.py` 無し、`tests/support/`
に herdr/tmux fake 無し (`delegation_route_fakes.py` は route-plan executor 用で backend
fake ではない)。

**構造的欠陥** (棚卸し実測):

- **共有 fake が無い / 単一 canonical が inline に埋没**。実 herdr の state machine を最も
  忠実にモデルするのは `_StatefulHerdr` (`tests/unit/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/test_sublane_actuator_herdr_ops.py:39`)
  だが、これは **1 テストモジュールの inline class** で、worker-dispatch テスト
  (`test_sublane_worker_dispatch_herdr_ops.py`, `_HerdrLaneFixture:61` が `:65` で wrap)
  ただ 1 つに import されるのみ。他の 50+ ファイルは各自 `RecordingRunner` / `FakePort` /
  `_FakeHerdr` / `_CloseHerdr` を再定義し、workspace↔pane lifecycle (§1.1 E の
  pane-close→workspace 自動消滅) を誰も共有的にモデルしない。
- **canned-response drift = false confidence**。各 fake の JSON 形が実 0.7.1 から乖離しても
  誰も気づかない。実バイナリに対する **contract テストが存在しない** (§2 で新設)。
- **routing seam を素通りする層で fake する (最重要、finding 1 の住処)**。
  dispatch-worker の inner send は `_drive_worker_send_argv`
  (`sublane_worker_dispatcher.py:235`) で実 `build_parser`→`orchestrate_handoff` に再入し、
  そこで `--target` (worker locator) を validate して rail を選ぶ。ところが herdr
  worker-dispatch テストはこの seam を **常に short-circuit** する:
  `DispatchContainmentTests` は `cli.build_parser` を `FakeParser`
  (`test_sublane_worker_dispatch_herdr_ops.py:159`) に差し替え `orchestrate_handoff` /
  `herdr_effective_backend_selected` / `require_tmux` を **一度も走らせず** argv だけを
  assert する; `HerdrUseCaseDriveTests` は `_drive_worker_send_argv` 自体を
  monkeypatch (`:286`); `BackendSelectorTests` (`:247-264`) は
  `_resolve_worker_dispatch_ops` (`sublane_worker_dispatcher.py:712`) が返す **class** の
  み assert する。**結果: `sublane dispatch-worker` (herdr) を実 inner send 経由で
  end-to-end 駆動するテストはゼロ**。tmux 側は `fake_run_tmux`
  (`test_same_lane_dispatch_submit_default.py:81`) で実 inner send を通すのに、herdr 側は
  通さない非対称。これが「cwd の repo-root 推定が config を読み損ね tmux rail に落ちる」
  経路 (§4.1) を構造的に不可視にしている。
- **唯一の実 herdr end-to-end send は別 entrypoint 経由**。`_FakeHerdr`
  (`tests/integration/e_110_execution_platform/f_130_handoff_routing/test_herdr_transport_wiring.py:63`)
  が `orchestrate_handoff` を直接駆動する (`PureHerdrEndToEndTest`) が、これは
  dispatch-worker choreography を経由しない。composition (dispatch-worker → inner send →
  predicate 群) を join するテストがどこにも無い。

### 1.3 モデル化 / 未モデル化の面

| 実 API 面 (§1.1) | 現行 fake が model するか | gap |
|---|---|---|
| A `agent start` argv+envelope | 部分 (argv assert する file はある) | 共有されず、`--workspace` prefix 検証 (#13330 review j#73231) を通す fake がない |
| B `agent list` decode | 部分 (canned rows) | malformed-row skip / recognised-empty 区別を注入する面がない |
| C rename/get 永続 | ほぼ無 | restart 永続 (E10) を再現する scenario がない |
| D `workspace create` + base pane | 部分 (#13330 test) | 共有 state machine でない |
| E **pane close→workspace 自動消滅** | **無** | retire choreography の核心 (#13380) を誰も再現しない |
| F `wait` change-semantics | 部分 (#13255 turn-start) | check-then-wait race を scenario で駆動する harness がない |
| G **backend routing (config→rail)** | **無 (routing 上で fake)** | **finding 1 の住処。最重要 gap** |

---

## 2. Scope 2 — fake の契約面設計 + ドリフト guard

### 2.1 単一 fake herdr の契約面 (API surface)

`tests/support/` に単一の **stateful fake herdr** を新設する (`herdr_fake.py`, 仮称)。
既存の分散 inline stub を段階的にこれへ収斂させる (§5-C)。契約:

- **注入形は実 code と同じ `Runner` port** (`Callable[argv,...] -> CompletedProcess`)。
  subject-under-test は実 `HerdrTransport` / `herdr_discovery` / route authority を通し、
  fake は **最外の subprocess 境界**のみを差し替える (Detroit-school: 実 collaborator を
  最大限通す)。
- **in-memory state machine** が §1.1 の A〜F を保持する:
  workspaces{id → {root_pane, panes[], cwd}} / agents{name → {pane_id, workspace, role, lane, cwd, status}}。
  `agent start` は state を変え locator を採番、`agent list` は state を render、
  `pane close` は pane を消し **workspace 内 pane が 0 になれば workspace を消す** (E)、
  `wait` は登録済みの論理遷移スクリプトに対して change-semantics で応答する (F)。
- **fail-closed 面を注入できる**: malformed row、duplicate assigned name、missing locator、
  mislocated launch (`--workspace` prefix 不一致)、`wait` timeout。これらは実 code の
  fail-closed 語彙 (§spec §3 の `no_match`/`multiple_matches`/`missing_locator`,
  route authority の `target_unavailable`/`target_ambiguous`/`route_locator_missing`) を
  **実 code 側に判定させる**ためのトリガであり、fake が語彙を持たない。
- **時間を持たない**。`wait` は実 sleep せず論理 tick / pre-armed event で決定論化する
  (E12 の「先に wait を arm→注入」順序を fake が再現)。macOS `timeout` 非依存、CI 高速。
- **public/private boundary 遵守**: fake が使う identity は抽象 placeholder のみ
  (home path / secret-shaped literal 禁止、既存 `delegation_route_fakes` と同姿勢)。

### 2.2 fake 自身を実バイナリに対して検証する contract テスト

fake ドリフト = false confidence を防ぐ **contract テスト** (小さな実機面) を設計する。
これは scenario harness とは別レイヤで、実 herdr 0.7.1 バイナリに対して fake の
「入力→出力形」が一致することだけを確認する:

- **対象面 (最小)**: A `agent start` の JSON envelope 形 (`type`/`agent.name`/`agent.pane_id`)、
  B `agent list --json` の行 schema、D `workspace create` の応答形、E `pane close` 後の
  `agent list` に workspace が消えること、F `wait` の change-semantics (既状態で返らない)。
- **契約の書き方**: 「同一 argv に対し実バイナリと fake が **構造的に一致**する JSON を返す」
  を assert する parametrized テスト。値 (locator 文字列など) は per-process 変動するので
  **shape/invariant を比較** (key 集合、type、prefix 規則)、literal 一致は取らない。
- **実行 cadence (重要、drift guard の本体)**:
  - contract テストは `@skipUnless(MOZYO_HERDR_BINARY set)` で **default suite からは skip**
    (CI は実バイナリを持たない / trusted-env のみ binary 解決 = #13245 姿勢)。
  - **coordinator の post-review 実機 acceptance** で必ず 1 回走らせる (live smoke と同じ
    実機 window)。これを acceptance protocol に組み込む (`logic-acceptance-rerun-protocol`
    への追記 pointer)。
  - **herdr version pin と結合**: `herdr --version` を記録し、version が上がったら contract
    テストを必ず再実行する gate。version 文字列単独を evidence にせず feature probe と
    併用 (runtime fingerprint 規律、`external-project-herdr-adoption.md` 既述)。
  - contract テストが赤 = fake が実仕様から乖離した signal。scenario の green は
    contract green を前提としてのみ意味を持つ (green の依存順序を doc で固定)。

### 2.3 ドリフトの二方向を明示

- **fake → 実バイナリ drift** (fake が古い): §2.2 contract テストが捕える。
- **実 code seam → fake の期待 drift** (production が新 API 面を使い始めたが fake が
  未対応): scenario テストが `NotImplementedError` 相当で **fail-closed に落ちる**よう
  fake を設計する (未モデル化 argv は canned 成功を返さず明示 error)。silent 成功 =
  最悪の false confidence なので、fake の default は「知らない argv は fail」。

---

## 3. Scope 3 — scenario harness 設計

### 3.1 配置 (tests-placement policy 準拠)

`logic-tests-placement-discovery-policy` (#12489) に従う:

- scenario 本体は **cross-cutting** (複数 bounded context を跨ぐ) ので
  `tests/scenarios/` に置き bounded context 細分しない (既存 route-plan scenario と同列)。
- 共有 fake は `tests/support/herdr_fake.py` (仮)。`test_*` でないので discover 非対象、
  scenario/regression から import (既存 `support/delegation_route_fakes.py` と同姿勢)。
- discovery authority は不変: `python -m unittest discover -s tests -v`。

### 3.2 choreography end-to-end

create→dispatch→ACK→callback→retire の全 leg を fake herdr に対して流し、**各 hop で
backend routing を assert** する。各 leg で「実 code が backend をどう解決し、どの rail に
乗せたか」を観測点にする:

| hop | 駆動する実 seam | assert する routing decision |
|---|---|---|
| create | `herdr session-start` / `sublane create --execute` の launch/adopt + `_launch_target_for_lane` (#13380 host join) | mzb1 名 mint、workspace join (project pin vs sublane host)、cold-start base pane reclaim (#13330) |
| dispatch | `dispatch-worker` → `_resolve_worker_dispatch_ops` → `HerdrWorkerDispatchOps.dispatch_to_worker` → `herdr_effective_backend_selected` | **worker_pane が herdr locator → herdr rail** (finding 1 の観測点)。config→rail の予測が cwd 由来 root と一致すること |
| ACK | route authority `resolve_herdr_route_target` (lane-in-match `derive_target_lane`) + queue-enter 送信 | 単一 live slot に解決 (`target_unavailable`/`ambiguous` に落ちない)、delivery ACK (exit 0) のみ promote (#12988) |
| callback | `--target coordinator` の workspace-scoped 解決 | coordinator lane の Codex slot に解決、cross-lane hop が lane-in-match を通ること |
| retire | `sublane retire` + `pane close`→workspace 自動消滅 (E) | lane slot 消滅、host workspace が最終 pane で自動 close、legacy token 経路と分離 |

### 3.3 parametrization (tmux/herdr × git/非git/外部)

harness は次の直交軸で parametrize する。**backend 分岐が全組合せで正しく解決すること**が
本 US の核心なので、外部 project 軸は必須:

- **backend 軸**: `tmux` / `herdr`。tmux は byte-invariant 確認 (routing が herdr に漏れない)、
  herdr は fake 経路。
- **workspace topology 軸**: `git main checkout` / `linked worktree (sublane)` /
  `非git (`.mozyo-bridge/scaffold.json` marker のみ)` / `外部 project (mozyo_bridge repo 外の
  合成 workspace、config-only marker)`。
- **root-inference 軸**: cwd = workspace root / cwd = child dir (子 cwd から root 推定を
  強制。#13379 j#73711 / finding 1 の再現条件)。

各 scenario は synthetic な workspace directory を tmp に構築し (real git init or scaffold
marker のみ)、cwd を設定し、fake herdr state を seed して実 seam を駆動する。実バイナリ・
実 tmux・実 Redmine には触れない。

### 3.4 観測点の作り方 (assert 手法)

routing decision を観測するために fake が **駆動された argv の列と、実 code が選んだ rail**
を記録する (route-plan scenario の executor recorder と同姿勢)。scenario は:

1. 実 predicate (`herdr_effective_backend_selected`) の戻り値を直接 assert (pure、最速)。
2. その上で end-to-end: fake に届いた argv 列が **herdr rail のもの**か **tmux target
   validation で reject された**かを assert。finding 1 は「herdr locator が tmux target
   validation に到達した」ことを **fail として**捕える。

---

## 4. Scope 4 — 今日のバグ → 回帰 scenario 写像

### 4.1 #13397 finding 1 (最初の scenario、段階指示 4)

**バグの正体 (code で特定済み)**: dispatch-worker の inner send は backend を再解決する。
`herdr_effective_backend_selected(args)` = `herdr_backend_selected(args) and not
explicit_tmux_pane_target(args)` (`herdr_send_entry.py:111-132`)。`herdr_backend_selected`
は `load_repo_local_config(repo_root_from_args(args)).terminal_transport` を読み
(`herdr_send_entry.py:81`)、`repo_root_from_args` = `resolve_repo_root(args.repo)`
(`commands_common.py:25-26`)。

inner send は `--target-repo auto` で `--repo` を明示しない (`herdr_auto_target_repo` =
`repo_root_from_args`, `herdr_send_entry.py:135-148`) ので、`resolve_repo_root(None)` が
**cwd から marker walk で root 推定**する。外部 project で config-only marker
(`.mozyo-bridge/config.yaml` のみ、`.git` 無し) が walk 側 root marker に不在だと
(#13379 j#73711 が walk 側 `REPO_ROOT_MARKERS` へ追加して修正した系統の残穴)、
child cwd から root 解決に失敗 → config load が herdr を読めず → `herdr_backend_selected`
= False → send が tmux rail に落ち → herdr locator `wS:p3` を invalid tmux target として
reject → `target_unavailable`。

**決定論性**: 入力 = (外部 project cwd に config-only marker + backend:herdr, worker_pane =
herdr locator, child cwd)。出力 = 予測 backend。実機は一切要らない。

**再現 scenario** (§3.3 の該当セル):
`backend=herdr × topology=外部 project(config-only marker) × root-inference=child cwd`。
assert: `herdr_effective_backend_selected(args)` が **True** を返し、dispatch が herdr rail に
乗ること。バグ存在時はこの scenario が **red** になり、#13397 の fix (backend 伝播/root 推定
修正) がそのまま green 化 = 「古典テストで先回り」の第一実証。

> **touch 面分離**: 本 US は scenario を **設計**するのみ。#13397 が production fix を所有。
> 本 doc の scenario 仕様は #13397 の fix が満たすべき acceptance を先に固定する意味を持つ。

### 4.2 #13377 j#73640 `target_repo_mismatch` (explicit-lane 合成 cwd)

explicit-lane dispatch の合成 cwd が sender repo になり target lane worktree と不一致で
`target_repo_mismatch`。→ scenario セル: `backend=herdr × topology=linked worktree ×
explicit `--target-lane``。assert: 合成 cwd が target-repo gate を通り、lane-in-match が
target lane slot に解決すること。

### 4.3 #13379 j#73711 config-only marker root 推定不在

config-only marker が root 推定 (walk 側) に不在で child cwd で fail。→ §4.1 と同じ
root-inference 軸の別 leg (session-start/adopt 側)。scenario セル: `topology=非git or 外部 ×
root-inference=child cwd`。assert: `resolve_repo_root(None)` が marker を root と認識。
(auditor の pure-python 再現が既に回帰化した先例。scenario harness はこれを恒久 grid に組込む。)

### 4.4 写像の一般原理

今日のバグは全て **(cwd → root 推定 → config → backend → rail) の予測が実配置と食い違う**
決定論バグである。§3.3 の parametrization grid が、この予測経路を全 topology × root-inference
セルで駆動するので、同型のバグは **将来分も** grid のセル追加で先回りできる。

---

## 5. Scope 5 — 実装 US 分割案

投資粒度が大きいため、fake / harness / 移行を分割する。design consultation (段階指示 3) で
auditor と粒度合意してから起票する。

| US | 粒度 | deliverable | 依存 |
|---|---|---|---|
| **A: 共有 fake herdr (stateful)** | `tests/support/herdr_fake.py` の in-memory state machine (§1.1 A〜F)、未知 argv は fail-closed、時間なし | fake + fake 自身の unit テスト (fake の state 遷移を直接検証) | なし (先行) |
| **B: fake→実バイナリ contract テスト** | §2.2。`@skipUnless(MOZYO_HERDR_BINARY)`、shape 比較、acceptance protocol 追記、version-pin gate | contract テスト + `logic-acceptance-rerun-protocol` への cadence 追記 | A |
| **C: scenario harness + routing grid** | §3。`tests/scenarios/test_herdr_lane_choreography.py` (仮)、parametrization grid (backend × topology × root-inference)、各 hop の routing assert | harness + **finding 1 再現 scenario (§4.1, red→#13397 で green)** | A |
| **D: 今日バグ回帰の grid 組込み** | §4.2/4.3 を C の grid のセルとして追加 | 回帰 scenario 群 | C |
| **E: 既存 inline fake の収斂移行** | 57 分散 stub を A の共有 fake へ段階移行。byte-invariant を保ちつつ重複削減。module-health / baseline 規律遵守 | 移行 PR 群 (機械的、粒度小) | A, C |

- **順序**: A → (B, C 並行) → D → E。C の finding 1 scenario は #13397 と情報共有し、
  #13397 の fix を acceptance にできる (段階指示 4)。
- **A/C を最小 MVP に**: 最初は finding 1 の 1 セルが red→green するところまでを A+C の
  acceptance にし、grid 全 topology は D で広げる (大投資を段階化)。
- **E は独立に価値**: 共有 fake 収斂は drift 源 (§1.2) を構造的に減らすので、A 完了後に
  背景で進められる。ただし byte-invariant 移行なので focused + full suite 規律必須。

---

## 6. 受け入れ条件との対応

| #13398 受け入れ条件 | 本 doc での充足 |
|---|---|
| (a) fake 契約面 + ドリフト guard cadence | §2.1 契約面 / §2.2 contract テスト cadence (post-review 実機 + version-pin gate) / §2.3 二方向 drift |
| (b) scenario harness 構造 | §3 (配置 / choreography / parametrization grid / 観測点) |
| (c) 実装分割案 | §5 (US A〜E、順序、MVP 段階化) |
| (d) 今日バグ ≥1 件の scenario 写像 | §4.1 finding 1 (完全な code 特定 + 再現セル) + §4.2/4.3 追加 2 件 |

## 7. design consultation 事項 (auditor へ、実装 US 起票前)

投資粒度が大きいため、以下を auditor 裁定にかける (段階指示 3):

1. **fake の boundary 位置**: 最外 subprocess `Runner` 境界で fake する案 (本 doc) で妥当か、
   もっと内側 (discovery port / route authority port) で fake すべき面があるか。
2. **共有 fake 収斂 (US E) の是非と順序**: 57 分散 stub の移行はコストが大きい。段階移行の
   投資対効果、byte-invariant 保証の粒度。
3. **contract テスト cadence の強制点**: post-review 実機 acceptance への組込み方、version
   pin gate の実装場所 (`acceptance-rerun-protocol` 追記で十分か)。
4. **grid の初期網羅範囲**: MVP を finding 1 の 1 セルに絞る案で妥当か、最初から全 topology を
   要求するか。

## 参照

- 正本: `spec-herdr-native-identity` / `vibes/docs/logics/herdr-poc-13175-experiment-log.md` (実 0.7.1 仕様)
- 既存基盤: `tests/scenarios/test_delegated_coordinator_route_plan.py` +
  `tests/support/delegation_route_fakes.py` (#12491, Detroit-school 先例)
- 配置規約: `logic-tests-placement-discovery-policy` (#12489)
- routing seam: `handoff_transport_wiring.py` / `herdr_send_entry.py` (`herdr_effective_backend_selected`) /
  `sublane_worker_dispatch_herdr_ops.py` / `sublane_herdr_projection.repo_backend_is_herdr`
- 関連: #13397 (finding 1 production fix、touch 面分離) / 親 #12531 `120_シナリオ・受入テスト基盤`

---

## Appendix A — 棚卸し file:line matrix (Scope 1 実測)

### A.1 herdr test double (現行、分散 inline)

| double | file:line | model する面 |
|---|---|---|
| `_StatefulHerdr` (canonical) | `tests/unit/e_110_.../f_140_.../test_sublane_actuator_herdr_ops.py:39` (`run`:57) | `agent list`/`start`/`read`/`workspace create`/`pane close` の lifecycle。**唯一の状態機械**だが inline |
| `_HerdrLaneFixture` | `test_sublane_worker_dispatch_herdr_ops.py:61` (wrap `:65`) | per-lane herdr workspace + `HerdrWorkerDispatchOps` |
| `FakeParser` (**seam bypass**) | `test_sublane_worker_dispatch_herdr_ops.py:159` | inner `handoff send` を実行させない = **finding 1 の盲点** |
| `FakePort` | `test_transport_binding.py:48` | herdr `TerminalTransportPort`、`%N`→locator translation guard |
| `FakeTerminalTransport` | `test_terminal_transport.py:44` | abstract transport port + target 語彙 |
| `RecordingRunner` | `test_herdr_transport.py:47` / `test_herdr_state.py:52` | send/read argv 構築 / `agent get` state decode |
| turn-start 群 | `test_herdr_turn_start.py:52,77` / `test_turn_start_rail.py:68,84,100,122` | wait / Enter-resend self-heal rail |
| `FakeLister` | `test_herdr_observability.py:44` | `agent list`→managed-slot projection (mzb1 decode) |
| `FakeHerdrPort` | `test_status_herdr_block.py:117` | status inventory block |
| `_CloseHerdr` | `test_sublane_herdr_retire.py:125` (`run`:130) | retire / `pane close` lifecycle |
| `_FakeHerdr` (**唯一 e2e**) | `test_herdr_transport_wiring.py:63` (`run`:102) | `orchestrate_handoff` 直接駆動 (dispatch-worker 非経由) |
| executor seams | `test_delegation_route_executor.py:110,122,143,156` | backend-neutral route executor の inventory/send/stamp/sink |

### A.2 tmux test double (parallel)

| double | file:line | model する面 |
|---|---|---|
| `fake_run_tmux` (47 files) | 代表 `test_same_lane_dispatch_submit_default.py:81` | send-keys/capture-pane rail。**herdr の twin だが herdr 側に対応 e2e 無し** |
| `FakeTmuxControlPort` | `test_tmux_control_port.py:31` | availability + config sourcing |
| `FakeTmuxPaneHealthReads` | `test_doctor_tmux.py:92` | pane-health resolution |
| `_sentinel_tmux()` | `test_transport_binding.py:~93` | tmux-passthrough identity path |

### A.3 production backend-routing seam (US-E/harness の駆動対象)

| seam | file:line | 分岐 |
|---|---|---|
| `herdr_effective_backend_selected` | `herdr_send_entry.py:111,132` | `herdr_backend_selected and not explicit_tmux_pane_target` (#13320) |
| `herdr_backend_selected` | `herdr_send_entry.py:94` (config load `:81`) | repo-local config → herdr |
| `is_explicit_pane_target` | `handoff.py:1189` | `target.startswith("%")` = pane-as-tmux-target |
| `repo_backend_is_herdr` | `sublane_herdr_projection.py:67` (pred `:86`) | repo config → herdr selector |
| `repo_root_from_args` | `commands_common.py:25` | `resolve_repo_root(args.repo)`、cwd walk (root 推定) |
| `_resolve_worker_dispatch_ops` | `sublane_worker_dispatcher.py:712` | `repo_backend_is_herdr`→`HerdrWorkerDispatchOps` else `LiveWorkerDispatchOps` |
| `_drive_worker_send_argv` (**核心 seam**) | `sublane_worker_dispatcher.py:235` | 実 `build_parser`→`orchestrate_handoff` 再入、`--target` validate→rail 選択 |
| `bind_runtime_transport` / binding | `handoff_transport_wiring.py:180,216,264` (backend check `:203,241,280`) | herdr binding + turn-start rail 設置 |
| `orchestrate_handoff` | `commands.py:1621` | herdr-native 解決 vs tmux `require_tmux`/`%pane` preflight |
| `require_tmux` | `tmux_client.py:20` | herdr path が skip すべき tmux-only preflight |
| `%N`→locator translation | `transport_binding.py:321` (`_well_formed_pane_target:400`) | pane/target validation |
| `resolve_route_neutral` | `backend_neutral_resolver.py:194,196,278,330` | tmux/herdr branch + herdr-only `route_locator_missing` |

**最優先 gap (棚卸し bottom line)**: 欠けているのは *composed* dispatch-worker herdr harness —
`_drive_worker_send_argv` に **実** `build_parser`/`orchestrate_handoff` を stateful herdr fake
(`_StatefulHerdr` 拡張) 相手に走らせ、inner `handoff send` が実際に worker locator を validate
して herdr rail を選ぶ経路。現状はこれを parser (`FakeParser`) か `_drive_worker_send_argv` で
stub しており、`sublane dispatch-worker` の実 inner send は **tmux 側のみ** (`fake_run_tmux`)。
共有再利用可能な herdr fake の新設 (§2.1 / US-A) が per-file 重複も解消する。
