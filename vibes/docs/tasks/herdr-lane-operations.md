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

## session-start が片role分だけ起動して失敗した場合 (#13948)

`herdr session-start` は #13948 以降、requested role すべてが **launch した locator に live / startup screen clear /
locator-matched self-attestation** を観測できるまで success を返さない。片方だけ落ちた run は **exit 非 0** で、role ごとに
原因を名指しする (`provider_exited` / `shell_residue` / `startup_interaction_required` / `receiver_unreadable` /
`attestation_timeout` / `attestation_mismatch` / `locator_drift` / `inventory_unreadable` / `unprofiled_provider` /
`attestation_unavailable`)。**この run 自身は何も close しない**。

1. 出力 (text の `action=` / `--json` の `action_id`) から **startup action id** を取る。rollback はこの id の下でしか動かない。
2. read-only preflight: `mozyo-bridge herdr session-rollback --action-id <id> --json`
   - 何が閉じられ、何が閉じられないかを role ごとに返す。ここでは **一切 close しない**。
3. 全 participant が `eligible` のときだけ `--execute` を足す。**この action が起動した participant だけ**が対象で、
   adopted slot・別 action の slot・durable name だけ一致する pane は決して閉じない。
4. refusal はそのまま原因である。緩めない:
   - `pending_input_present` — 誰かの未送信入力がある。**owner approval があっても本 rail では preserve** する。破棄が必要なら
     `herdr session-retire` の `--pending-composer-discard-approval` (exact `direct_owner` marker) という**別 authority**へ回す。
   - `work_obligation_present` / `obligation_unreadable` — durable ledger が work を owe している / 読めない。
   - `identity_drift` / `ambiguous` — その pane はもう我々の物ではない / 重複名。
   - `agent_busy` — turn 実行中。中断しない。
   - `composer_unreadable` / `inventory_unreadable` — 読めないものを空とみなさない。
5. `startup_interaction_required` (trust / login / theme) は **operator が provider の UI で承諾する**。mozyo は決して回答しない。
   承諾後に `session-start` を再実行する (新しい action になる)。
6. `attestation_unavailable` は launch env の PATH に `mozyo-bridge` が無く #13637 wrapper が乗らなかったことを意味する。
   agent の boot identity が検証できないため success にはならない。PATH を直して再実行する。
7. rollback が `rollback_incomplete` を返したら **debt は残る**。同じ `--action-id` で再実行してよい (resume する)。
   `already_rolled_back` は record から答えた replay で、再 close はしない。

## hibernated bound pair の pins/stale 循環解消

`sublane repair-pins` が `slot_stale` / `identity_unattested` を返し、同時に `sublane recover-pair` が
`hibernated_record_missing_pins` を返す場合は、一方の guard を緩めたり locator を手入力せず、専用の
`sublane converge-bound-pair` (#13933) を使う。

### pending composer がpairをpreserveしている場合

`converge-bound-pair` が `pair_contains_preserved_slot` / `preserve_pending_composer` を返したら、そのcommandへ
forceやpending overrideを加えない。pending generationが本当に破棄可能かを別のread-only railで測る。

1. `mozyo-bridge sublane prepare-bound-pair --issue <id> --journal <decision-journal> --lane <lane> --worktree <path> --branch <exact-branch> --repo <target-root> --json`
   - `state=actionable` の場合だけ、出力された `bound_pair_composer_discard_approval` markerをowner approval
     journalへそのまま記録する。markerはlifecycle revision/generation、worktree+branch、full slot snapshot、discard
     role setを束縛する。旧active-lane `sublane quarantine` のapprovalやproseは代用できない。
   - correlated markerは既存delivery railで処理する。busy/tool-child、unknown/ambiguous/foreign/newer、dirty worktree、
     branch mismatchはzero-closeのまま原因を解消する。
2. 同じcommandへ `--execute`を加える。credential-gated live Redmine readでexact markerが一致したときだけ、承認された
   uncorrelated pending roleをguarded closeしaction-bound relaunchする。pins write、resume、dispatch、sendは行わない。
   partial retryは同じimmutable transactionのclose proofだけを使い、別 locatorや任意のabsent slotを成功扱いしない。
3. `state=prepared` 後に `converge-bound-pair` のpreflightを**取り直す**。その新しいslot snapshotに対する別approval
   markerで通常convergenceを実行する。prepare用markerをconvergence authorityへ流用しない。

1. read-only preflight:
   `mozyo-bridge sublane converge-bound-pair --issue <id> --journal <decision-journal> --lane <lane> --worktree <path> --branch <exact-branch> --repo <target-root> --json`
   - `state=actionable` のときだけ、出力の `approval_marker` を owner approval journal に**そのまま**記録する。
     action-time slot locator / revision / generation / worktree / branch のいずれかが変われば marker は stale になる。
   - `inventory_unreadable` / pair duplicate・foreign・half / busy / pending composer / dirty worktree / branch mismatch は
     zero-close。先に原因を解消して preflight を取り直す。
2. execute:
   同じ command に `--execute` を加える。command は `--journal` を credential-gated live Redmine で fresh readし、
   structured marker が exact 一致する場合だけ、bad generation を guarded close → action-bound relaunch → fresh pair
   attestation → bounded pins CAS の順で進める。transaction plan直前と各close直前にlifecycle revision/generation、
   hibernated/released/bound signature、inventory、clean exact branchを再読するため、その間のraceはzero-writeまたは
   zero-closeで停止する。raw Herdr/tmux、DB/store直接編集、pins推測は代替にしない。
3. outcome:
   成功しても lane は `hibernated` のままで、work dispatch / resume は起きない。`sublane repair-pins` または
   lifecycle readで pins を再確認し、その lane の本来の next action（通常 recovery / hibernate release / retire）へ
   進む。partial stop は同じ marker/actionで replayする。transaction proof のない absent slot は replay対象にならない。

## scratch pair retire (session-start の逆操作)

`herdr session-start` が作る scratch pair は **lane lifecycle record を持たない**。ゆえに上記 `sublane retire` の全契約が構造的に拒否し (`--execute` は `attest_retire_target` が `record is None` で `lane_owner_unverified`、`--retire-hibernated-bound` / `--reconcile-hibernated-live` / `--migrate-hibernated-legacy` は既存 `hibernated` row 前提で `lane_not_declared`、`recover-pair` は declared pins 前提)、public rail が無いまま capacity を専有し続ける (実証: #13882 j#80060 / j#80066 の保全 `dogfood13882` pair)。この隙間を埋める public rail が `herdr session-retire` (#13892)。

- `mozyo-bridge herdr session-retire --lane <label> --repo <root> [--json]` → **read-only preflight**。verdict のみで、close も write も行わず、**retirement authority の artifact (DB / seal / lock / temp) を 1 つも作らない** (review j#80523 R3-F4)。authority は strict read-only で観測する。
- `... --execute` → 明示の destructive intent。**対象 scratch pair の slot のみ** close する。
- identity は `session-start` と同じ durable な **assigned name** (`encode_assigned_name(workspace, role, lane)`) の exact 一致。pane / locator を引数で渡す口は無く、label-only / focus 依存の選択もできない。
- **本 rail の signature は「lifecycle record が無いこと」**。record を持つ lane は `lane_record_present` で zero-write 拒否し、既存 `sublane retire` 系へ route する (逆に、record を持たない pair は既存系が拒否する)。**retire を通すための lifecycle row 捏造は行わない** (#13882 j#80066 が却下した案)。capacity は `enumerate_active_lanes` が live pane を畳んで数えるため、**pane が消えること自体**が capacity 回収であり row は不要。
- fail-closed 軸: inventory unreadable / duplicate assigned name / foreign occupant / locator 欠落 / busy agent / pending composer / 同一 locator への衝突 → すべて **zero-close**。
- **owner-approved historical convergence (#13918)**: pending composer だけが残る owner-unbound / unattested pair は、`--pending-composer-discard-approval <issue>:<journal>` で approval の **locator** を渡した場合に限り、その composer を破棄して retire できる。番号の書式だけでは authority にならない。command は credential-gated live Redmine source でその exact issue/journal を毎回 fresh read し、journal に次の単一 marker があることを要求する（値は action-time observation から計算し、prose は解釈しない）。
  - `[mozyo:workflow-event:gate=pending_composer_discard_approval:version=1:approval_source=direct_owner:decision=approved:effect=discard_pending_composer_and_retire:issue=<issue>:workspace=<workspace>:lane=<lane>:slot_digest=<assigned-name-set digest>:pin_digest=<role+locator digest>]`
  - missing / unreadable / wrong gate（`codex_direct_edit` や close approval は代用不可）/ foreign workspace・lane・slot / stale locator は **reserve 前に zero-close**。verified evidence は journal notes hash を含む canonical JSON として load-bearing retirement attempt に保存する。pending retry は fresh read した evidence が **byte-equal** の場合だけ進み、approval 無し・別 journal・編集後 journal では close/complete しない。completion 後も exact pointer は fence から復元でき、best-effort audit の失敗で失われない。
  - これは `pending_composer` 一軸だけの明示 override であり、idle / inventory / foreign / duplicate / locator / lifecycle signature / durable obligation / retirement fence は一切緩めない。`issue_<id>_...` lane は approval issue の一致に加え、action-time の Git worktree が readable・clean・branch==lane でなければ zero-close。default（flagなし）は従来どおり pending composer を拒否する。pane / locator / force 引数は追加しない。
- **durable obligation gate** (review j#80506 F4 / j#80523 R3-F1・R3-F3): idle / turn-ended は **receiver state** であって durable obligation の不在証明ではない (skill `references/workflow.md` `### ACK / delivery / completion の分離`)。
  - **ordering は `pending publish → obligation read → close`**。reserve を先に置くのが要点で、**publish して初めて dispatch 側が読める**。逆順 (先に読む) は必ず stale な答えになり、読んだ後に dispatch が reserve できてしまう。
  - **双方向**: **全 covered source の実 send edge** が send の**前**に retirement authority を確認し、`pending` / `completed` なら **zero-send**。対象 edge (**6 本**、単一 seam `target_is_retiring` を共有): `herdr_dispatch_execution.execute_dispatch` / `callback_sweep` / `operator_startup_resume` / **`CallbackOutboxProcessor.deliver`** / **`execute_herdr_forward`** / **`sublane_hibernated_pair_recovery_live.redispatch_to_gateway`**。★source を covered (=読む) にするだけでは不十分で、**その source の実 send edge を塞ぐ**必要がある (j#80620 R5-F2)。★★**edge の列挙は grep で数える。docstring や過去の journal の「全 edge」表記を信用しない** — j#80636 は「5 edge すべて」と報告したが、`target_is_retiring` 自身の docstring が名指ししていた hibernated redispatch が未結線だった (j#80644 R6-F3)。**retiring 時は reserve を `cancelled` にする** (reserved のまま放置すると「send の fate 未解決」として、その guard が譲ったはずの retirement 自身を block する deadlock になる)。∴ 先に publish した側が勝ち、他方は**必ず何もしない** (retire 先行→`sent=0` / dispatch 先行→`closed=0`)。
  - 読む source は **covered な 3 つ** (下表): dispatch outbox / callback outbox (owed TO) と forward fence (owed **FROM**)。いずれかが **不読なら `obligation_unreadable` で zero-close**。
  - **durable obligation source matrix** (どの store が scratch pair の slot に owed な work を持ちうるか。正本は `tests/regressions/test_issue_13892_obligation_source_matrix.py` が pin):

| source | 判定 | 理由 (実読で確認) |
|---|---|---|
| `DispatchOutboxFence` | **covered** (owed TO) | `target_assigned_name` が key 列。★`issue`/`journal` は `''` を許すので「scratch pair に issue が無い」論は**効かない** — 論でなく**読んで**塞ぐ |
| `CallbackOutbox` | **covered** (owed TO) | ★**key ではなく row が target を名指す**。row の `target_lane` / `target_receiver` から `BackendNeutralTargetResolver` が `encode_assigned_name(ws, target_receiver, target_lane)` で canonical pane_name を再構築する。「key に name 列が無い」は真だが**無関係**(review j#80594 R4-F3)。active (`pending`/`inflight`/`uncertain`) は owed |
| `ForwardOutboxFence` | **covered** (owed **FROM**) | ★Acceptance 2 は work dispatch **/ progress obligation** を要求する。ここでは pair が **sender** で、`from_lane_id`/`from_role` がその identity。generation は correlated callback が返るまで active なので、途中で close すると forward が stranded になる。「target 名が key に無い」は除外理由にならない |
| `CallbackPublicationFence` | structurally-inapplicable | key が **`lane_generation` を要求**し、これは lifecycle row のみが mint。scratch pair は row を持たない。`issue`/`dispatch_anchor` も同様。行の意味も「Redmine record の書込」で pane への owed work ではない |
| `HerdrDeliveryLedger` | structurally-inapplicable | **evidence であって authority でない** (`append_only_lossy` / UNIQUE key 無し / state machine 無し)。loss が無害と宣言された store を許可 gate にすると lossiness が silent yes になる |
| `HerdrIdentityAttestation` | structurally-inapplicable | assigned name keyed だが `rebuildable_cache` projection。docstring 自身が permission verdict への昇格を禁じる |
| `SessionInventory` | structurally-inapplicable | pane keyed だが「never the source of truth」な cache。obligation semantics を持たない |
| `CallbackSweepLease` | structurally-inapplicable | key が `issue` + `anchor` (Redmine anchor) を要求。かつ **attempt lease** であって owed work ではない |

- **`delivered` の相関** (R4-F1 / R5-F1、設計 j#80629 Option 1A): delivery ACK は task completion ではないので単独では通さず、**無条件 block もしない** (それは normal pair を恒久 retire 不能にする)。**source-of-truth の Redmine issue/journal** を読んで相関する (`RedmineJournalSource` / live は `LiveRedmineJournalSource.from_environment()`)。★**CallbackOutbox は相関 source にしない** — `delivered` は *callback の* delivery ACK、`dead_letter` は「unclassified / retry 枯渇」で、**どちらも元 dispatch が渡した work の completion を所有しない** (j#80620 裁定)。Redmine が読めないことは代替 authority 採用の根拠にならない → **不読は block**。

### `dispatch-disposition` marker (#13892 / j#80629)

`action_id` は元々 **AUTHORIZE marker の 1 箇所にしか書かれず、それを echo する terminal marker が無かった**ため、「どの dispatch **round** が終わったか」を Redmine から証明できなかった。その欠落を埋める専用 channel。

```text
[mozyo:dispatch-disposition:action_id=<opaque>:dispatch_journal=<AUTHORIZE の journal>:workspace_id=<ws>:lane_id=<lane>:target_assigned_name=<exact name>:terminal_gate=review_request:terminal_journal=<review_request の journal>:conclusion=discharged:recorded_by_role=implementation_gateway]
```

- **issue identity は marker 本文でなく owning entry から**取る (self-report の spoof 防止)。
- **`review_request` のみが positive terminal gate**。★**`implementation_done` は terminal ではない** — partial な implementation_done は正当な日常形であり (実例: #13892 j#80627 は「部分修正・未完」を明示した implementation_done)、terminal にすると **worker が work を負ったまま書いた journal が false discharge を生む**。`blocked` / progress / callback delivery / `dead_letter` も discharge しない。
- **writer は `implementation_gateway` 固定** (worker の自己申告完了は discharge にしない)。canonical writer は記録直前に **credential-gated live Redmine を fresh read** し、(1) `dispatch_journal` に valid な AUTHORIZE が **exactly one** 存在し identity が exact 一致、(2) `terminal_journal` が **dispatch より後**の canonical `review_request`、(3) 記録は terminal より後、(4) 同一 payload は **idempotent no-op** / conflict は **zero-write** を確認する。prose / pane / CallbackOutbox / delivery ACK / issue status からの自動 backfill は禁止。historical repair も同じ producer・同じ検査を通す。
- **reader (`session-retire`)**: **AUTHORIZE → later `review_request` → later exact disposition の三者一致のみ `discharged`**。zero match は `owed`、不読 / credential 失敗 / blank identity / foreign field / 順序逆転 / invalid fixed field / duplicate conflict は **block**。同一 payload の retry は dedupe、同じ `action_id` に異なる terminal/identity があれば ambiguous block。**issue closed だけ / 後続 gate の存在だけ / CallbackOutbox `delivered|dead_letter` だけでは discharge しない**。changes_requested 後の再 dispatch は **new journal + new `action_id`** なので、旧 disposition は新 action を discharge しない。
- **production producer は `mozyo-bridge workflow step` の gateway leg 唯一** (#13892 R6-F1 / j#80644 scope ruling)。同一 lane の implementation_gateway が step を踏み、その lane の verified anchor が **当該 round を終わらせた `review_request`** のとき、`gateway_disposition_intake` が (a) その round の dispatch AUTHORIZE を **exactly one** に解決し (直前 `review_request` より後・当該 terminal より前の候補が 1 本のときのみ。0 本 / 2 本以上は **zero-write**)、(b) canonical writer を fresh live read + credential-gated note append で実行する。★**writer を `implementation_gateway` 固定にした j#80629 は「gate flow へ挿さない理由」ではない** — worker gate writer / CallbackOutbox delivery / pane ACK へ誤配線しないための理由である。caller を持たない writer は rail ではなく、marker が live に一切存在せず delivered row が恒久 `owed` になる (R6-F1 の実害)。
  - `--dry-run` は **書かない** (durable marker を append する dry run は dry run ではない)。
  - step に対しては **fail-soft** (bookkeeping append が gateway の review action を止めない)、record に対しては **never fail-open** (refusal は zero-write)。
  - ★**applicability (= disposition を負う round か) は role + verified anchor **のみ**で決める。dry-run 判定より先**に決めること (#13892 R8-F3)。invocation の仕方 (dry-run か否か) は applicability を変えない。非 gateway / anchor 無しは **完全に silent** (envelope field も足さない = additive 契約)。**これが silent を許される唯一の 2 分岐**。
  - ★**applicability gate を通った後の refusal は、原因が何であれ全て surface する** (`dispatch_disposition` = `state`/`reason`/`detail`/`wrote`/`ok`、text/JSON 両方。#13892 R7-F1 / R8-F1)。★★`applicable` は **明示 field** にする — **`reason` の allowlist から推論しない**。R8-F1 は「sender identity 失敗を `no_verified_anchor` と誤ラベル → その reason が非 applicable 扱い → envelope から消滅」で、**R7-F1 の欠陥が別 shape で再発**した。`sender_identity_unresolved` と `no_verified_anchor` は別物 (前者は round が実在し anchor も検証済み)。
  - ★**writer の semantic state を潰さない** (#13892 R8-F2): `recorded` / `already_recorded` / `refused` を envelope に保持し `ok` を付ける。**same payload replay = 成功** (j#80629 idempotency contract) なので、text も `already recorded (idempotent replay)` とし `NOT recorded` と**区別**する。`wrote` bool だけに畳むと **契約上の成功が envelope 上で失敗に反転**する。**「zero-write だから安全」は誤り** — marker 未記録のまま review result を投稿すると **latest verified anchor が当該 round を通り越し、再試行の契機が永久に消える** → delivered row は恒久 `owed` → 本 issue が除去対象とする恒久 stuck そのものになる。**fail-closed が安全なのは誰かに伝わっている時だけ**。escaped exception も裸の `None` にせず `state=error` / `reason=leg_raised` にする (握り潰すと「記録すべきものが無かった step」と区別不能)。
  - **`dispatch_authorize_not_found` (0 本) と `dispatch_authorize_ambiguous` (2 本以上) を区別する**。両方 zero-write だが operator の取るべき行動が違う (後者と誤報すると存在しない重複を探しに行く)。
  - 本 gateway の実環境では **`MOZYO_REDMINE_DELIVERY_WRITE` が未設定**であり、標準 step は `write_opt_in_unset` で zero-write になる。これが envelope に出ることが live 運用の前提。
- **workflow gate ではない**: channel は watcher の recognized channels と `GATE_BEARING_KINDS` に **入れない**。correlation を説明する record であって workflow event ではない。
- **判定の正本は `tests/regressions/test_issue_13892_obligation_source_matrix.py`**。covered は「実際に scratch slot が現れ reader が返す」probe で、inapplicable は「**不可能にしている precondition**」を assert する (store の形が変われば落ちる)。prose は腐るので判定を prose に置かない。

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
