# herdr lane 運用手順書 (coordinator / operator 向け)

herdr backend (`terminal_transport.backend: herdr`) での sublane 運用の標準手順。2026-07-07〜08 の herdr 移行波 (#13331 / #13355〜#13360) の live 実測で確立した運用を replay 可能な形で固定する。設計正本は `vibes/docs/specs/herdr-native-identity.md`、role/gate の正本は central preset と `vibes/docs/rules/agent-workflow.md`。本書は手順のみを扱い、規約本文を複製しない。

## 標準入口 vs primitive/debug 面 (#13446)

backend=herdr の workspace では、旧 tmux-era の semantic selection / pane 選択入口を **通常入口として選ばない**。これらは tmux server と tmux pane inventory を前提にしており、herdr session (tmux pane を持たない agent) では live agent が存在しても `no_candidate:repo` / `self_lane_unresolved` に落ちる (再発事例 #13435 j#74176 -> j#74177: repo config=herdr・`herdr agent list` に Codex/Claude が居るのに、coordinator が `agents targets` / `handoff send --select` を先に叩いて tmux 選択側で `no_candidate:repo`)。

- **lane 実装 dispatch の標準形**: `sublane create --execute` / `sublane start --execute` (coordinator 経由。詳細は下記「lane 作成 (標準形)」)。
- **primitive / debug / compat 面** (標準入口ではない): `handoff send` / `handoff send --select` / 明示 `%pane` target / `agents targets` / `message --select-role` / `workflow step` の tmux `%pane` self-lane 解決。これらは低レベル primitive・互換・debug 用途に限る。
- **preflight guard**: backend=herdr を検出した標準入口は、旧 tmux selection へ silent に落ちず `herdr backend active` を明示して上記標準形へ誘導する (fail-closed / guidance)。
  - `mozyo` (bare, herdr): session-start で workspace / 両 agent slot の存在を確認し、summary に `next:` (標準 dispatch) を出す。`--json` は `next_action` を持つ。
  - `workflow step`: backend=herdr では tmux `%pane` に触れる前に fail-closed し、reason=`herdr_self_lane_unresolved`・herdr-native lane env (`HERDR_PANE_ID` / `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID`) の観測 detail・`sublane` next_action を返す。
  - `handoff send --select` / `message --select-role`: selection fail 時、message に `herdr backend active` と `sublane create --execute` / `--target-lane` 代替を出す。
  - `agents targets`: backend=herdr で tmux-era primitive/debug 面である旨を stderr note で明示 (listing 自体は read-only で維持)。
- tmux backend の workspace では上記 guard は一切発火せず、出力は byte-invariant。

## routable lane state の確認と runtime fingerprint (次 action 前, #13543)

session 移行 bundle や journal の `target lane` label を、live で routable な herdr lane と同一視しない。lane に dispatch / handoff / retire の next-action を取る前に、次を **別 state** として確認する (契約正本: `spec-session-continuity-user-harness` `### Routable lane state の区別` / `### Runtime fingerprint gate (backend=herdr)`)。

1. **Git branch/worktree**: `git branch --list issue_<id>_<slug>` / worktree の存在。branch/worktree が在っても routable lane を意味しない。
2. **registered lane metadata**: `sublane list --lane <label> --json` の `sublanes`。非空なら registered、`sublanes: []` なら **lane-unregistered** (branch が在れば branch-only)。この空振りを「dispatch 送達失敗」と誤帰着しない。
3. **live routable runtime**: gateway/worker の live slot 実測 (`herdr agent read <pane>` / lane metadata の pane id)。metadata registered でも live slot が無ければ **runtime-unavailable**。
4. **runtime fingerprint**: `mozyo-bridge doctor runtime` を read-only 実行。installed CLI が source checkout の herdr preflight 等を欠く (`status: drifted` / probe mismatch) なら、skew を durable record に fail-closed 記録し、installed CLI surface の出力を next-action の根拠にしない。以降の lane discovery / dispatch は repo-local source CLI (`PYTHONPATH=src python3 -m mozyo_bridge <args>`) で行う (installed CLI upgrade は owner-gated)。

再発事例 (#13543 / #13535 j#75183): installed `mozyo-bridge 0.10.0` が #13446 herdr preflight を欠き、backend=herdr でも `agents targets` を通常面のように tmux 候補列挙した。coordinator が runtime fingerprint を照合せず、その空振りを handoff blocker 理由にした。正しくは `sublane list --lane issue_13535_session_transition --json` = `sublanes: []` (lane-unregistered / branch-only) であり、tmux candidate 空振りではない。

## 前提

- 実行 CLI: **installed CLI (pipx の `mozyo-bridge` / `mozyo`) が標準** (#13167 で herdr lane 世代へ追いつき済み、#13379 で installed CLI のみでの lane 運用完結を確認)。installed CLI が最新 origin/main と版ズレしている間に限り、fallback として **repo-local CLI** を使う。fallback の標準形は package 直の module 実行 (`src/mozyo_bridge/__main__.py` 経由):

```sh
PYTHONPATH=src python3 -m mozyo_bridge <args...>
```

  - **罠 (実測)**: submodule 指定の `python -m mozyo_bridge.application.cli` は `__main__` guard が無く **silent no-op** (出力なし・exit 0) — 「撃ったつもり」事故の既知トラップ。package 直 (`-m mozyo_bridge`) と混同しない。
  - `python3 -c 'import sys; sys.argv=["mozyo-bridge", ...]; from mozyo_bridge.application.cli import main; main()'` の直呼び形式も同等に有効 (旧手順互換)。

- `MOZYO_HERDR_BINARY`: launch 注入済み agent (lane worker / gateway) は不要。手動起動の coordinator 等で未設定なら `MOZYO_HERDR_BINARY=$(command -v herdr)` を inline 付与。
- lane の identity (**#13377 shared project workspace model**): sublane の slot は `mzb1_<project-ws>_<role>_<lane_label>` で、mzb1 の workspace segment は project identity のまま。linked worktree は main checkout の registry identity を継承する (#13152)。`sublane create` 時の record (component `lane_metadata`) が `(repo_workspace_id, lane_id)` unit と label/issue の display join を担う。
- lane の**配置** (**#13380 dedicated sublane host workspace**): lane slot は coordinator pair の project workspace ではなく、**専用 sublane host workspace** に着地する。herdr workspace 数は「project 1 + sublane host 1」の定数 (lane 数に比例しない)。host は最初の lane 作成時に on demand で mint され (operator 可読 label `<main-checkout名>_sublanes`、cosmetic のみ)、lane ゼロで herdr が自動 close する (残骸 husk は生じない)。#13380 以前に作成された coordinator workspace 同居 lane は heal では同居のまま (pair 不分裂優先)、retire で自然 drain する。
- lane の**細分化** (**#13411 lane=tab / gateway+worker=split**): sublane host workspace 内で、非 default lane ごとに専用 herdr tab を割り当て、gateway + worker を同 tab 内 split pair として並置する。7 lane = 14 loose pane ではなく 7 tab に整理される (owner intent #13377 j#73654 の密度懸念に対応)。tab join は live inventory の `tab_id` のみが authority (label は cosmetic、lane key 由来)。fresh lane は `herdr tab create` で tab を mint、heal / 混在 adopt+launch は生存 slot の `tab_id` を読んで**同一 tab へ復帰** (pair 不分裂)。tab root pane は base pane と同型で全 launch 成功後に reclaim、tab 内最終 pane close で herdr が tab を自動消滅させる。retire は assigned-name の `pane close` のままで tab 配置に非依存 (最終 pane close で tab / host が自動消滅)。identity / route / projection は不変。pre-#13411 に loose pane で起動された legacy lane は heal でも loose のまま、full relaunch で tab へ移行する。owner は display knob (#12391 範疇) としていつでも override 可。
- **legacy lane (pre-#13377)**: 旧 model の lane は独自 herdr workspace (`wt_<hash>` segment, default lane) を持つ。読み (list / status / dispatch-worker) と retire は互換対応済み。新規 create は常に shared model。legacy lane への coordinator dispatch は互換対象外 — 生かしたまま運用せず、順次 retire する。

## `sublane list` の metadata / runtime 読み分け

`sublane list --lane <label> --json` は lane metadata store と live Herdr inventory の read-only projectionであり、`sublanes` が非空という事実だけでは metadata record の存在を証明しない。shared-model lane は metadata record がなくても live assigned-name rowから表示される。

- row の `stale_hints` が `lane_record_missing` を含む: metadata record は absent。gateway / worker locatorがあれば live slotは別軸で presentになり得る。この組合せのprimary verdictは `lane-unregistered` とし、runtime stateを別途併記する。
- `lane_slots_missing` を含む: active metadata recordは presentだが、対応するlive managed slotは absent (`runtime-unavailable`)。
- `sublanes: []`: 対象laneのmetadata recordもlive rowもprojectionに現れていない。Git branch/worktreeの有無はGitから別に測る。
- store / projectionがunreadable、またはrecord-backedか判別不能: metadataは`unknown`としてfail-closedにする。

dispatch / handoff / retire可否は、Git branch/worktree、metadata record、gateway+workerのlive routabilityを独立して記録してから判断する。完全なstate / verdict語彙は [[spec-session-continuity-user-harness]] `### Routable lane state の区別` を正本とする。

## lane 作成 (標準形)

1. dispatch decision journal を issue に記録 (durable anchor)。
2. 単発 create+dispatch (#13378 以降の標準):
   `sublane create --issue <id> --lane-label issue_<id>_<slug> --branch issue_<id>_<slug> --worktree <sibling path> --base-ref origin/main --journal <jid> --upstream-coordinator <coordinator herdr pane> --execute --json`
   - gateway/worker は専用 sublane host workspace 内に lane slot (`--target-lane` = lane label) として起動する (#13380。#13377: per-lane workspace は作られない)。内蔵 dispatch も `--target-lane <label>` の explicit-lane 送達 (routing は mzb1 identity 基準で、herdr 配置に依存しない)。
   - 旧標準の `--no-dispatch` 二段運用は「create 内蔵 dispatch が gateway TUI の boot に間に合わず空振りする」実測が理由だった。#13378 で herdr の gateway readiness probe が liveness のみ → **live かつ rendered** (`agent read` で描画内容あり) に強化され、dispatch は boot 完了を bounded wait (`--gateway-ready-timeout`、既定 10 秒) してから送られる。
   - **self-heal**: dispatch が失敗し read-back で gateway slot の消滅を確認した場合に限り、lane column を 1 回だけ自動 relaunch (`append_lane_column` 再実行 = adopt-or-launch。#13380 lane-aware join: 生存 slot が pin (pair 不分裂)、両 slot 消滅でも他 lane slots の host に join するか host を再 mint する) して dispatch を再試行する。再失敗は fail-closed (`blocked`) で手動介入へ。
   - outcome の `reason` に `self-healed` が含まれる場合、記録すべき gateway pane id は relaunch 後のもの。
3. 着弾確認: `dispatch_result=gateway_notified` + delivery record の marker observed / turn-start。marker 未観測なら `herdr agent read <pane>` で実測してから再送判断。以降の worker 駆動は gateway の `sublane dispatch-worker` (#13357)。
4. fallback (旧二段運用): `--no-dispatch` で create し、boot 待ち後に明示送達も引き続き可:
   `handoff send --to codex --source redmine --issue <id> --journal <jid> --kind implementation_request --target <gateway pane> --target-repo <lane worktree 絶対 path> --target-lane <label> --role-profile implementation_gateway --profile-field lane=<label> --profile-field upstream_coordinator=<coordinator pane>`
   - **`--target-lane <label>` が lane slot の明示指定** (#13377)。同一 project workspace 内の送達なので workspace 越えではなく、`--target-repo` は repo/cwd gate として渡す (auto は sender repo に解決される)。
   - この経路では gateway 消滅時の自動復旧は働かない。送達失敗時は下記 relaunch 標準で復旧する。

## 初回 gateway pane 消滅の原因と運用注意 (#13378)

- 原因 (host log 実測、#13378 j#73606): lane 作成〜初回送達の間に host で agent CLI の global update (実例: `npm install --global @openai/codex`) が走ると、**idle かつ session 未確立** の codex TUI が exit 0 で自己終了し pane ごと消える。mozyo の launch 経路 (env / permission mode / adopt pin / root pane reclaim) の欠陥ではない。busy / session 確立後の agent は同じ update を生き延びる。
- 運用注意: **wave 進行中 (lane 作成〜初回送達の window) に agent CLI (codex / claude) の global update を実行しない**。update は wave 間の quiescent 時に行う。
- 復旧: 標準形の create+dispatch は self-heal で自動復旧する。self-heal 外 (稼働中 lane の途中死・fallback 経路) は下記 relaunch 標準で手動復旧する。

## gateway → worker の駆動 (実測 ACK)

- gateway は lane worktree で `sublane dispatch-worker --issue <id> --lane-label <label> --journal <jid> --execute --json` を実行 (#13357)。
- `dispatch_result=worker_dispatched` / `worker_dispatch_confirmed=true` のみが送達成立。失敗は `gateway_notified` のまま fail-closed。結果は issue journal + #13296 ledger に残る。

## worker の relaunch (stall / 再起動時)

- `herdr session-start --agent codex --agent claude --repo <lane worktree>` — lane segment は lane metadata record から自動復元される (record が無い場合は `--lane <label>` を明示。無指定 + record 無しは fail-closed)。
- launch 先 workspace は **lane-aware join** (#13380): 自 lane の生存 slot / adopted slot が最優先で pin (pair 不分裂)、無ければ他 lane slots の sublane host に join (coordinator workspace は除外)、それも無ければ host を再 mint する。旧「claude 単独指定は新 workspace に迷子」(#13360 j#73407) は live-agent join で構造的に解消したが、両 agent 指定の運用は維持してよい。
- relaunch した worker は「⏵⏵ auto mode on」footer を確認 (permission parity #13360)。旧 pane は先に `herdr pane close`。
- relaunch 後、gateway に worker route の再駆動を指示 (worker の pane id は変わるが解決は assigned name 経由で自動追従)。

## Host reboot recovery (#13518)

host (Mac 等) が再起動されると lane pane の Claude/Codex TUI は exit するが、`herdr agent list` の durable assigned-name row は残る (foreground は `-zsh` のみ、detected agent 無し)。**複数正本を照合する fail-closed recovery reconciler** を使い、DB 単独を authority にしない (設計正本: #13520 j#75276)。

- **state を混同しない (authority matrix, #13520 j#75276)**: Redmine issue/journal = workflow gate と durable anchor / Git worktree・ref・diff = code と dirty state / `registry.sqlite` + repo-local anchor = workspace identity / `state.sqlite` = lane metadata・callback outbox の復元材料 (workflow truth ではない) / herdr assigned-name + live inventory = runtime liveness / launch-time sender env = 再 attest する process-local input (永続 authority にしない)。
- **composite liveness で false-positive adopt を防ぐ (#13518 j#75329)**: `herdr session-start` の adopt 判定は assigned `name` 一致だけでは不十分。`agent list` row を `classify_named_slot` (`domain/herdr_slot_liveness.py`) で複合判定し、detected agent 不在 + `agent_status=unknown` の **shell residue** は `stale_named_slot` として outcome `stale` で surface する (blind adopt しない / 名前が残っているため launch も上書きしない)。detected agent が名指しされた live slot は従来どおり adopt、liveness signal を一切持たない minimal row も従来どおり adopt (self-heal 不変)。
- **dirty worktree を never-clobber**: recovery 中に lane worktree を reset / stash / delete / recreate しない。未 commit 成果は保全して同一 durable anchor から resume する (12-file dirty diff を SHA-256 で preflight/post-check して不変を確認した実例: #13518 j#75331/j#75334)。
- **stale pane の close + same-slot relaunch は destructive** ゆえ **owner-approved recovery gate** を要求する (replayable に journal 記録: #13518 j#75331)。承認後は old pane を `herdr pane close` → 同一 lane/worktree へ `herdr session-start` で relaunch (adopt でなく launched になる)。
- **projection cache を authority にしない**: `sublane status` の `panes=[]` は stale projection でありうる。live assigned-name inventory と矛盾する場合は同じ reconciler で fail-closed に扱い、runtime 不在と即断しない。
- **env 欠落に注意**: reboot 後に adopt された既存 process は launch-time `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID` を欠くことがある (session-start adopt は retroactive 注入しない)。正規 dispatch が `missing_sender_env` で fail-closed した場合、registry/anchor/live assigned-name から検証した値を **その 1 回の** high-level dispatch child process にだけ再注入する (env spoof / 別 role 偽装はしない)。Herdr backend では tmux 専用の `mozyo-bridge init` hint は無効 (`TMUX_PANE is not set`)。
- **fail-closed 条件**: workspace mismatch / missing・unreadable journal / ambiguous live slot / DB と Redmine・Git の矛盾は停止。implementation/close/integration/publish を自動承認しない。

## lane retire (guarded close)

1. lane worktree の dirty を確認・復元: `git -C <worktree> checkout -- .claude/settings.local.json` (agent harness が触る唯一の常連 dirt)。**dirty のままだと retire は `dirty_worktree` で fail-closed する** (正常動作、#13331 j#73339 guard)。
2. `sublane retire --issue <id> --lane-label <label> --worktree <path> --branch <branch> --issue-closed --callbacks-drained --verified --durable-record --target-identity-known --execute --json` → **対象 lane unit の managed slot のみ** close (#13602 Option A: routine green-preflight retirement は coordinator authority。`--owner-approved` flag は無い。`--issue-closed` は「対象 issue が種別ごとの close 契約を満たして closed」を表す — child Task/Test/Bug は `task_close`(owner_close_approval なし)、US / standalone issue は owner_close_approval-backed close (central preset `US-Level Audit Model`)。retire actuation はどの契約でも owner close approval を再収集しない。未解決の owner-approval-waiting は `--callbacks-drained` 側で block する) (#13377: project workspace・coordinator pair・他 lane は閉じない。最終 lane の close で sublane host workspace が herdr により自動消滅するのは無害な付随挙動で、retire の前提・完了条件ではない — #13380)。legacy lane (`wt_<hash>` workspace) は互換 plan で旧 slot も close される。
3. worktree / local branch の除去は **統合後** (`git worktree remove` + `git branch -d|-D`)。remote branch は削除しない。

## scratch pair retire (session-start の逆操作)

`herdr session-start` が作る scratch pair は **lane lifecycle record を持たない**。ゆえに上記 `sublane retire` の全契約が構造的に拒否し (`--execute` は `attest_retire_target` が `record is None` で `lane_owner_unverified`、`--retire-hibernated-bound` / `--reconcile-hibernated-live` / `--migrate-hibernated-legacy` は既存 `hibernated` row 前提で `lane_not_declared`、`recover-pair` は declared pins 前提)、public rail が無いまま capacity を専有し続ける (実証: #13882 j#80060 / j#80066 の保全 `dogfood13882` pair)。この隙間を埋める public rail が `herdr session-retire` (#13892)。

- `mozyo-bridge herdr session-retire --lane <label> --repo <root> [--json]` → **read-only preflight**。verdict のみで、close も write も行わず、**retirement authority の artifact (DB / seal / lock / temp) を 1 つも作らない** (review j#80523 R3-F4)。authority は strict read-only で観測する。
- `... --execute` → 明示の destructive intent。**対象 scratch pair の slot のみ** close する。
- identity は `session-start` と同じ durable な **assigned name** (`encode_assigned_name(workspace, role, lane)`) の exact 一致。pane / locator を引数で渡す口は無く、label-only / focus 依存の選択もできない。
- **本 rail の signature は「lifecycle record が無いこと」**。record を持つ lane は `lane_record_present` で zero-write 拒否し、既存 `sublane retire` 系へ route する (逆に、record を持たない pair は既存系が拒否する)。**retire を通すための lifecycle row 捏造は行わない** (#13882 j#80066 が却下した案)。capacity は `enumerate_active_lanes` が live pane を畳んで数えるため、**pane が消えること自体**が capacity 回収であり row は不要。
- fail-closed 軸: inventory unreadable / duplicate assigned name / foreign occupant / locator 欠落 / busy agent / pending composer / 同一 locator への衝突 → すべて **zero-close**。
- **durable obligation gate** (review j#80506 F4 / j#80523 R3-F1・R3-F3): idle / turn-ended は **receiver state** であって durable obligation の不在証明ではない (skill `references/workflow.md` `### ACK / delivery / completion の分離`)。
  - **ordering は `pending publish → obligation read → close`**。reserve を先に置くのが要点で、**publish して初めて dispatch 側が読める**。逆順 (先に読む) は必ず stale な答えになり、読んだ後に dispatch が reserve できてしまう。
  - **双方向**: 全 outbox reserve→send edge は send の**前**に retirement authority を確認し、`pending` / `completed` なら **zero-send** (`herdr_dispatch_execution`)。∴ 先に publish した側が勝ち、他方は**必ず何もしない** (retire 先行→`sent=0` / dispatch 先行→`closed=0`)。
  - `reserved` / `uncertain` は owed。**`delivered` は delivery ACK であって task completion ではない**ので単独では通さず、issue/journal identity を durable gate と相関できない限り block する。store 不読は `obligation_unreadable` で zero-close。
  - **durable obligation source matrix** (どの store が scratch pair の slot に owed な work を持ちうるか。正本は `tests/regressions/test_issue_13892_obligation_source_matrix.py` が pin):

| source | 判定 | 理由 |
|---|---|---|
| `DispatchOutboxFence` | **covered** | `target_assigned_name` が key 列。**唯一** slot 宛の owed work を持つ store。★`issue`/`journal` は `''` を許すので「scratch pair に issue が無い」論は**この store には効かない** — 読んで塞ぐ |
| `CallbackOutbox` | structurally-inapplicable | key に name 列が無い (journal 上の gate 遷移を指す) **かつ** 空 `issue`/`journal` を hard-reject する。scratch pair は anchor を持たず key を構築できない |
| `ForwardOutboxFence` | structurally-inapplicable | target の assigned name は **設計上 key から意図的に除外** (j#76528 point 1: rename が generation を進めないため)。key は *sender の route*。target を記録する field が無い |
| `CallbackPublicationFence` | structurally-inapplicable | key が **`lane_generation` を要求**し、これは lifecycle row のみが mint する。scratch pair は row を持たない。`issue`/`dispatch_anchor` も同様。行の意味も「Redmine record の書込」で pane への owed work ではない |
| `HerdrDeliveryLedger` | structurally-inapplicable | **evidence であって authority ではない** (`append_only_lossy` / rebuild path 無し / UNIQUE key 無し / state machine 無し)。loss が無害と宣言された store を許可 gate にすると lossiness が silent yes になる。`target` は transport locator で assigned name ではない |
| `HerdrIdentityAttestation` | structurally-inapplicable | assigned name keyed だが `rebuildable_cache` projection。startup 自己申告の verdict で owed work を持たず、docstring 自身が permission verdict への昇格を禁じる |
| `SessionInventory` / `CallbackSweepLease` | structurally-inapplicable | 前者は pane keyed の cache、後者は issue+anchor を要求する **attempt lease** (owed work ではない) |
- **post-close whole-unit re-measure** (review j#80506 F3、#13842 j#79320 R3 の踏襲): close の return code は「command が受理された」ことしか示さない。close 後に **fresh inventory** で unit 全体を再測定し、expected 全不在 + foreign / duplicate 不在を **positive に確認**できたときのみ retirement を記録・success。residue は `post_close_residue`、不読は `post_close_unreadable` で、**commit 済みの `closed` は保持したまま** non-success。
- **durable outcome は `ScratchRetirementFence` の row** (設計 j#80526 Option A-prime)。home-scoped の専用 authority で、workflow truth でも desired-state history でも lifecycle authority でもない **operational action-idempotency / side-effect transaction authority**。unit identity = `workspace_id` + `lane_id` + **canonical (順序非依存) assigned-name set**。`managed_events` は fence completed の **後**に best-effort narrative audit として append され、その失敗は proven retirement を無効化しない (load-bearing なのは fence)。**lifecycle row は作らない**。capacity 回収は従来どおり **live pane の positive absence**。
- **transaction ordering**: reserve pending → close → fresh whole-unit remeasure → fence completed → green。全体を **exclusive nonblocking OS advisory lock** で保持する (`BEGIN IMMEDIATE` は close を跨げない)。contention は `retirement_busy` で zero-close (待たない・奪わない)。
- **zero-slot は success ではない** (review j#80506 F1): absence は「pair がここに無い」ことの証明であって「**この command が retire した**」ことの証明ではなく、`--lane` typo と never-launched が区別不能。**exact な prior completion + action-time live-zero を証明できる時だけ** `already_retired` で **exit 0**、それ以外は `retire_evidence_absent` で non-success / zero-write。
- **zero-slot は決して store を bootstrap しない**。true first bootstrap は **live exact pair が positively present ∧ 全 authority artifact が absent** の時のみ (同一 lock 下で serialize)。失われた authority を無言で再作成しない。
- **operator 可視化**: `mozyo-bridge herdr retirement-store status [--json]` が authority の artifact shape (absent / present / damaged) と attempt を **read-only** で表示する (何も作らない)。damaged の間 `session-retire` は fail-closed で、**本 issue は generic な unsafe recover/reset を意図的に持たない** (失われた authority を再作成すると過去の retirement を忘れ、relaunch された pair を再 close しうるため)。recovery は command ではなく issue に記録する operator 判断。
- **store identity/recovery**: artifact inventory は `lexists` semantics で DB / `-wal` / `-shm` / `-journal` / seal / **temp** を見る。bootstrap は temp へ組んでから rename し seal を最後に書くので、**書込途中の crash は temp か seal 欠落として damaged 側に落ちる**。**absent / present / damaged の三分**で、damaged (片側欠落・orphan sidecar・seal 不一致・unknown schema・corrupt) は `retirement_authority_unavailable` で fail-closed。completion 後の store loss も fail-closed で、**prior success を捏造しない**。可視化は `fence.status()`。本 issue では generic な unsafe recover/reset は追加しない (残余は follow-up)。
- **#13842 の isolated-ledger anti-pattern との差**: あちらは ledger loss で **live pane を抱えたまま恒久 stuck**。本 fence の pending は **held / crash-released OS lock 下で resume** され、completed の loss は success を捏造せず withhold するだけ (pane は既に消えており capacity leak なし)。sibling authority と同じ **local total-loss indistinguishability residual** のみを持ち、それ以上は主張しない。
- **default lane は拒否** (coordinator pair は対象外)。他 lane は plan の unit scoping で構造的に対象外。
- **partial close は replay 可能**: pending attempt に **pinned locators** と closed progress が durable に残るため、re-run の close authority は **`attempt.pinned` − (positively absent | 既 closed)** の **exact locator のみ**。★**assigned name が一致しても locator が違えば `pin_drift` で non-success** — 同名で relaunch された別 pair を旧 attempt の権限で閉じないため (review j#80523 R3-F2)。crash 位置別の replay: `live + pending` → full preflight 再実行後に close resume / `absent + pending` → whole-unit re-measure 後に **completed へ repair** / `absent + completed` → **idempotent success (exit 0)** / `absent + proof 無し` → `retire_evidence_absent`。fence completion write 失敗は non-success (`completion_unproven`) だが **truthful な closed は保持**し、次回 run が pending から repair する。
- **relaunch 誤認の防止**: herdr assigned name は `(workspace, role, lane)` で決まるため同名 pair が再 launch され得る。`completed` の後に新しい live slots が現れたら **新しい attempt (revision+1) を開く**ので、古い completion が稼働中の pair の proof に流用されない。
- attestation は要求 **しない** (#13892 j#80483): scratch pair は generation / lifecycle row を持たず attestation は構造的に取得不能で、要求すると本 rail が対象とする唯一の shape (live-but-unattested) を恒久 retire 不能にする。identity は assigned-name 一致 + foreign 不在 + duplicate 不在 + locator 一意で証明する。
- worktree / branch 除去、process launch / resume、raw herdr / tmux、store 直接 mutation は伴わない。

## 統合 (integration disposition)

- 単一 lane が origin/main 直上 (ff 可) → operator の `git push origin <hash>:main` 一発。
- 並列波 → scratch worktree に integration branch を切り、approved commit を順に cherry-pick → conflict 解決 → **full suite (`unittest discover -s tests`、redirect + exit 判定、pipe 禁止)** → branch を origin へ push (anchor 到達性) → operator ff push → **re-anchor 対応表を Feature issue に記録** (旧 hash → 統合 hash)。
- 統合後: local main ff、lane worktree/branch 掃除、各 US に integration + re-anchor journal。

## 監視・callback の実際

- **coordinator 宛の handoff callback は coordinator が busy だと `precondition_not_idle` で不達になりがち。durable record (Redmine journal) の poll が正** — stall 判定は必ず journal 再取得 → 結果なし確認 → pane 実測 (`herdr agent read`) → 再送、の順。
- `blocked` 表示の agent_status は permission prompt / 一時状態の場合がある。pane read で実体確認してから介入する。

### recovery classification（再送・relaunch前）

| observed state | classification | next action | 禁止 |
| --- | --- | --- | --- |
| permission prompt / approval wait | `permission_wait` | durable gateと要求権限を照合し、正規approval経路へ | agent死としてrelaunchしない |
| logout / `authentication required` / process終了 | `agent_auth_unavailable` | credentialを記録せずre-authまたはfresh agent relaunch | routing bug扱い、blind resend |
| agentはliveだが実command shellに`MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID`が無い/不整合 | `sender_identity_missing_or_conflict` | dispatchを止め、runtime propagation/proxy gapをdurable化 | 手動env注入、raw Herdr send |
| assigned-name/lane slotが無い、または複数 | `route_runtime_unavailable` | lane metadata + live inventoryを再取得し、standard relaunch/preflightへ | tmux-era candidate空振りだけで断定 |

`sublane create --execute`がlaunch後にdispatchだけfailした場合は、起動済みslot、未配送anchor、失敗理由をjournalに残す。partial laneを成功扱いせず、同じcommandをblind replayしない (Redmine #13613)。

## live smoke の原則

- **本番機構で行う**: lane の smoke は必ず linked git worktree で。scratch 単独 repo は registry canonicalization の差を隠す (#13331 j#73348 の教訓)。
- 実 store / 実 workspace を汚さない工夫: 使い捨て stub slot (sleep process + `--no-focus`) や scratch `MOZYO_BRIDGE_HOME` を使い、smoke 後に必ず回収 (#13358 j#73456/j#73472 の実例)。
- 破壊系 (server 停止等) は並列 lane を巻き込むため、同一 fail path の代替実測 (例: `MOZYO_HERDR_BINARY=/usr/bin/false`) で置換可 (#13355 実例)。

## 非 Git workspace の lane (directory scaffold, #13392)

herdr backend は非 Git workspace (registry 採用済みの scratch / sync フォルダ等、git repo でない workspace root) の lane も動かせる。tmux 時代の directory-scaffold-lane 対応を herdr で復元したもの (設計正本: `vibes/docs/logics/sublane-lifecycle-map.md` の Git/非Git 差分、裁定 #13392 j#74067)。

- **runtime cwd = workspace root**: 非 Git lane は worktree を持たない (`git worktree add` は skip)。lane の cwd / `cockpit append --repo` / dispatch の `--target-repo` gate はすべて **workspace root 自身** に collapse する。lane agent は workspace root で走る。
- **create contract (#13432)**: 非 Git workspace では `sublane create` の `--branch` / `--worktree` は **optional** である。両方省略すると lane は worktree を持たず (skip_no_git)、省略された `--worktree` は **workspace root へ既定 collapse** する (runtime root と一致)。sibling worktree path を明示すると phantom path になり identity 解決に失敗しうるため、非 Git では省略が推奨形。`--branch` は非 Git では使われない。`--issue` / `--lane-label` は Git/非 Git を問わず必須 (省略時 `missing_field:issue` / `missing_field:lane_label` で fail-closed)。**Git workspace の contract は不変**: `--branch` / `--worktree` は必須で、省略は `missing_field:*` で fail-closed する (argparse ではなく create/actuate use case が probe 後に判定する)。
- **placement**: 非 Git lane も #13380 の dedicated sublane host workspace に着地する。lane の identity は `(project workspace_id, lane_label)` unit であり、`lane_id != default` なので coordinator の default-lane pair とは別 slot。host 分離は「distinct repo-root がある時だけ」ではなく `(workspace_id, lane_id)` + lane-aware placement で成立する (非 Git は repo-root を共有しても lane 分化する)。
- **並列 lane**: 同一非 Git workspace root 上で複数 lane を並走できる。lane_metadata は lane ごとに lane-scoped key (`dl_<hash(root, lane_id)>`) で記録され上書きしない (Git lane の `wt_<hash(worktree path)>` とは別体系)。
- **retire**: `sublane retire --worktree <workspace root>` で対象 `(workspace_id, lane_id)` の managed slot のみ close する。coordinator の default-lane pair は close しない。**branch / merge / worktree cleanup は非 Git では対象外** (worktree が無いため `git worktree remove` / branch 削除 / retire-time merge は発生しない。成果の取り込みは別経路)。
- **注意**: 非 Git の並列 lane は conversation / runtime lane の分離であって filesystem isolation ではない (branch / worktree による隔離は存在しない)。Google Drive / sync フォルダでは owner 方針どおり auto git-init はしない。
- 記録衛生は Git lane と同じ: workspace root の host-local 絶対 path を Redmine journal に書かない (workspace label / lane label で参照)。

## 記録の衛生

- journal / commit message に host-local 絶対 path を書かない (worktree は sibling 名または lane label で参照)。`lane_metadata` の `worktree_path` は host-local private (正本: `vibes/docs/rules/public-private-boundary.md`)。
