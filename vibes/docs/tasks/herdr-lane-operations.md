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

### 消滅したworkerのguarded recovery (`sublane recover-stale`, #13806 / #14663)

managed standard-sublane workerのprocessが消滅し、同じlane/worktreeへ安全に再joinできる場合は、raw close/relaunchではなく `sublane recover-stale` を使う。既定はread-only preflight。破壊的な `--execute` は、preflightが返す `required_approval_marker` を指定Redmine journalへ記録した後だけ許可される。

- approval journalはaction-timeにfresh readし、`stale_worker_recovery_owner_approval` markerが一意に実在すること、writerがgate固有rulingからanchored `coordinator` と解決されること、markerが `approval_source=direct_owner` / `decision=approved` を宣言することを連言で要求する。
- markerのdigestはaction id/generation、participant identityとworker inventory revision、lane revision/generation、元gateとredispatch actionを完全に束縛する。`--worker-revision` が空のpreflightは観測互換のため残るがapproval markerを生成せず、`--execute` はzero-closeで拒否する。
- journal不在・別issue所有・reader未配線/非fresh・markerの引用/重複/field不一致・unanchored/別role writerはすべてzero-close。post-closeで別 `--resume-journal` を使う場合は、元approvalと再approvalの両journalをfresh検証する。

## gateway の guarded refresh (`sublane recover-gateway`, #14203)

same-lane implementation_gateway が **callback delivery 確認済みの provider turn を即時終了し、期待 durable gate が着地しないまま live `turn_ended` を占有し続ける** 場合の標準回復。`recover-stale` は gateway を保護し (worker 専用)、`recover-pair` は hibernated lane 専用のため、この形は本 command だけが扱う。raw Herdr/tmux close・blind resend は使わない。

- **preflight (既定, read-only)**: `sublane recover-gateway --issue <owning issue> --lane <label> --role codex --provider codex --assigned-name <mzb1_…> --locator <live locator> --resume-anchor-journal <j> --resume-gate <gate> --json`。provider turn を closed 分類 (`turn_productive` / `turn_failed_no_durable_gate` / `turn_unconfirmed` / `turn_not_settled` / `turn_unobservable`) し、slot fence 10 軸 (identity / gateway-only / issue-lane / generation / **launch authority** / settled / composer / resume-anchor / worker 保全 / authority) を実状態から判定する。
  - ★**launch authority は close 前に判定する** (#14475、live blocker #14462 j#88463)。refresh の launch leg は destructive close の **後** に lane の ambient authority (lifecycle rev/gen + canonical `worktree_identity` token + expected branch) を再 join するため、この軸を preflight が持たないと「preflight actionable → old gateway close 済 → `launch_authority_moved` で relaunch 不能」という **不可逆な** 停止になる。preflight と action-time fence は **同一 evaluator** (`lane_authority_reason`) を参照し、boolean 射影が `decide_gateway_refresh` の軸になる。blocker は `launch_authority_unavailable` (**zero close**) で、detail に失敗軸の closed token (`worktree_identity_unbound` / `worktree_identity_mismatch` / `lane_revision_generation_moved` / `branch_drifted` / `lane_pins_unpinned` / `lifecycle_row_absent` / `lifecycle_unreadable` / `worktree_token_underivable` / `worktree_unreadable` / `unknown`) と回復 runbook を添える。token は軸名のみで path / token 値 / identity を出さない。
    - preflight で launch authority を評価するには `--lane-revision` / `--lane-generation` の pin が要る (未 pin は `lane_pins_unpinned` で block)。approval は preflight の出力から書くので、pin 無しの preflight を `actionable` の根拠にしない。
    - ordering: turn 分類の **後** に置く。`turn_productive` な lane は refresh 自体が不要なので、authority gap があっても `turn_not_classified_failed` のまま名指しする。identity / generation 等のより基礎的な fence は従来どおり先に勝つ。
    - この fence が守るのは **1 回目の close** のみ。close が既に commit 済みの post-close replay は従来どおり `identity_unknown` + committed-close transaction でのみ admit され、authority が壊れていれば `stopped` を維持する (追加 close 0 / launch 0)。
  - **durable journal が authority**: anchor より後に gate が着地していれば `turn_productive` — runtime がどう見えても refresh は拒否される。
  - **`delivered_not_started` 等の未確認 delivery/turn start は failure ではない** (`turn_unconfirmed`)。fresh durable read が構成されていない env (Redmine credential 無し) は `turn_unobservable` で fail-closed。
  - reason (`rate_limit`/`auth`/`session_stale`) は構造化 evidence token の注入のみ。不明は `unknown` (herdr は turn 終了理由を公開しない)。
- **execute (destructive, owner approval 必須)**: 上記に加え `--journal <owner approval j>` `--action-id refresh-gateway:<lane>:<role>:<provider>:<name>:<locator>` `--action-generation <n>` `--lane-revision <r>` `--lane-generation <g>` `--execute`。完全pin済みpreflightは `required_approval_marker` を返す。executeはjournalをaction-timeにfresh readし、一意な `gateway_recovery_owner_approval` marker、anchored `coordinator` writer、`approval_source=direct_owner`、action/generation/participant/lane/resume先のdigest完全一致を検証する。pointer形状だけ・散文・引用・別issue所有・reader不能はzero-close。検証後も `turn_failed_no_durable_gate` + 全 fence green のときだけ、**exact gateway generation のみ** close → same-slot fresh launch → action-bound attestation → **既存 anchor を governed handoff rail で exactly-once resume** (IR/RR は再生成しない)。worker / default coordinator / foreign slot は ordered fence が保護する。
- **partial failure**: replacement transaction が replay fence を保持し、re-run が resume する。close 後 crash の replay は `identity_unknown` + committed-close transaction のみ admit。
- **typed outcome** (#14475 review j#88477 F2): blocker の判定材料は `detail` の散文でなく **typed field** で出す。`--json` payload と text 出力の双方が、全 outcome (preflight / refused / stopped / completed) で `launch_authority_reason` (closed token) を、blocking 時は `launch_authority_runbook` (secret-safe な回復手順) を伴って出す。`ok` のときも reason は出る (automation が「field 不在＝正常」を推測しなくてよい)。
  - ★**reason は action-time の事実である** (review j#88485)。close は launch より前に commit されるため、preflight で `ok` だった lane が launch 直前の re-join で moved することがある (#14462 j#88463 はまさにこの遷移)。actuator 停止時および resume leg の authority refusal 時には canonical evaluator を **再読**し、その時点の axis を報告する。preflight 時の観測を持ち回さない。lane authority が維持されたまま別要因 (gateway assigned name occupied 等) で停止した場合は、再読結果どおり `ok` のままとする — この field は「何かが失敗した」ではなく **観測**を報告する。
- **launch leg の typed reason** (#14480): actuator は launch leg の失敗を一律 `detail="launch"` に潰すため、**なぜ launch できなかったか**は別の closed field で出す。`--json` payload は全 outcome で `launch_failure_reason` を持ち (fence が起きていなければ `null`)、text 出力は **fence が起きたときだけ** `launch_failure: <token>` 行を出す。token は fence 自身が raise した closed token (`replacement_binding_context_missing` / `pair_split` / `launch_target_absent` / `launcher_*` 等) を **verbatim** で運び、typed reason を持たない失敗だけを `launch_error` に落とす。compat として `detail` も `launch:<token>` に描画する。
  - ★**`launch_authority_reason` とは独立の軸である**。前者は「lane の ambient authority が今どうか」の**観測**、後者は「launch leg が何に fence されたか」。#14480 の live 事故 (#14479 j#88695) は `launch_authority=ok` のまま launch が 2 回失敗した形であり、authority 軸だけでは原因を名指しできない。
  - ★**空 (`null`) と `launch_error` は別の主張である**。空は「launch fence は起きていない」(成功した / launch leg に到達していない)、`launch_error` は「失敗したが typed reason が無い」。空を「正常」と読み替えて良いのは前者だけ。
  - token は **axis / fence 名のみ**で、path・locator・credential・例外散文を含めない。port が返した値が closed-token の形 (`^[a-z][a-z0-9_]{0,63}$`) を満たさない場合は publish せず `launch_error` に落とす (fail-closed)。
- ★**v1 attestation store 下では launch は exact participant context を要求する** (#14480)。selected store が v1 の間、action binding は participant を key とする side record なので、`launch_or_resume_v1_replacement` は target の `provider` / `assigned_name` / `old_locator` / workspace / lane を必須とし、欠けると `replacement_binding_context_missing` で **launch 自体が不可能**になる (「generic に launch される」ではない)。recovery port はこの context を **pin から** 取る — pin は close / verify が既に通している単一 authority であり、より狭い派生を別途組み立てない。`target_provider` は同時に same-tab postcondition を **その 1 participant に scope** する: actuator は 1 launch あたり 1 participant しか駆動しないため、この edge では pair は構造上 partial である (recover-gateway = gateway closed / worker live、recover-stale = worker closed / gateway live)。**scope しても live split は fail-closed のまま**で、緩むのは *absent* sibling の場合だけ (後続 leg が収束させる)。生存 sibling は `prepare_session` の adopt-or-launch 冪等性で **adopt** され、relaunch も close もされない。
- 実装正本: `domain/gateway_turn_recovery.py` / `domain/lane_launch_authority.py` (#14475) / `domain/replacement_launch_failure.py` (#14480) / `application/sublane_gateway_recovery*.py` (#14203)。launch context threading は `application/sublane_stale_worker_recovery_live.py` の `LiveRecoveryActuatorPort.launch_action_bound` (recover-gateway / recover-stale 共有 port)。

## live turn-ended worker の guarded refresh (`sublane refresh-worker`, #14661)

標準 sublane の **implementation worker** が、resume delivery 確認済みの provider turn を durable progress なしで終え、**live `turn_ended` を占有したまま in-scope の dirty worktree を抱えている** 場合の標準回復 (実測: #14658 lane j#92366)。既存 surface はどれもこの形を扱えなかった — `recover-stale` は `not_stale` で拒否 (process は本当に live)、`recover-gateway` は設計上 worker を保護、`callback-recovery` は `no_progress_after_handoff` を報告するだけで close しない、`sublane start --execute` は pair を adopt し再配送できるが同じ失敗後の再々送が exact-once でない。raw Herdr/tmux close・generic kill・blind resend は使わない。

- **既存 admission を緩めない**。`recover-stale` の「消滅した worker」admission と本 command の「live で unproductive な worker」admission は **別の事実・別の admission** である。`is_stale` fence はそのまま維持され、live worker を `recover-stale` に通す緩和は行わない (#14661 j#92369 design constraint。domain test が `decide_recovery` の `not_stale` を pin する)。
- **preflight (既定, read-only)**: `sublane refresh-worker --issue <owning issue> --lane <label> --role claude --provider claude --assigned-name <mzb1_…> --locator <live locator> --worker-revision <row revision> --lane-revision <r> --lane-generation <g> --resume-anchor-journal <j> --resume-gate <gate> --json`。
  - **turn 分類は #14203 の closed 語彙をそのまま使う** (`turn_productive` / `turn_failed_no_durable_gate` / `turn_unconfirmed` / `turn_not_settled` / `turn_unobservable`)。#14661 は class token を **足さない** — 消費側は 1 方言だけを読む。
  - ★**分類は 3 つの identity に束縛される** (#14661 acceptance): `anchor_bound` (durable anchor が exact に解決する = 非空 issue + 数値 journal id + closed resumable gate)、`lane_generation_bound` (pin した lane generation が live lifecycle と join できている)、`participant_revision_bound` (観測した row の revision が pin と exact 一致)。**いずれかが立たなければ `turn_unobservable`** — 破壊的判断の根拠になる観測が「どれについての観測か」を言えないなら、それは観測ではない。lane generation は **単一 evaluator** `lane_authority_reason` から射影する (lifecycle 系 token = unbound、worktree/branch 系 token は別軸なので unbind しない)。
  - **durable journal が authority**: anchor より後に worker progress gate が着地していれば `turn_productive` — runtime がどう見えても refresh は拒否される。worker progress の closed 語彙は `GATE_BEARING_KINDS` から **導出** し `review_result` だけを除く (reviewer の出力は worker へ *配送される* もの。progress と数えると「答えなかった worker」の回復を握り潰す)。導出なので upstream に gate 種別が増えれば自動的に progress 側へ入る — **refresh を減らす方向にしか動かない**。
  - ★**causal link は ordering + lane binding である**。gateway 側 (`review_request` anchor → `review_result` marker の `req=<anchor>`) と違い、worker の gate marker は答えた request への back-pointer を持たない (`render_workflow_event_marker` の `req` は `review_result` 専用)。そこで安全側に倒す: envelope (`lane` / `lane_generation`) を持つ marker は **両方一致** を要求し、envelope を持たない marker も **progress として数える**。出所不明を `turn_productive` 側へ倒せば最悪でも「close しない」で済み、逆に倒すと gate を出していた worker を close する。
  - **`delivered_not_started` 等の未確認 delivery/turn start は failure ではない** (`turn_unconfirmed`)。fresh durable read が構成されていない env (Redmine credential 無し) は `turn_unobservable` で fail-closed。
  - slot fence は 11 軸 + turn 分類の ordered 判定: identity / **worker-only (gateway・default coordinator・foreign を保護)** / issue-lane / generation / **turn 分類** / **launch authority** / settled / composer / resume-anchor / **worktree readable** / gateway 保全 / authority conflict。
  - ★**gateway 保全軸は canonical repo workspace + 一意な 1 slot に束縛する** (#14661 review j#92443 F3)。herdr inventory は **host-global** で lane label は workspace 内でしか一意でないため、workspace join が無いと**別 workspace の同名 lane** が「gateway は生きている」を単独で満たしてしまう (対象 workspace に gateway が 1 つも無くても preserved と読める)。同 lane の live gateway が 2 件ある曖昧な状態も、どの slot を保全しているか名指せないので不成立とする。
  - ★**turn-start の generation binding は pin revision を明示的に渡す** (#14661 review j#92443 F1)。共有 authority (`gateway_generation_authority`) は以前 `getattr(request, "gateway_revision", "")` で pin を読んでいたため、`worker_revision` を持つ worker request では**恒久的に空** = `turn_started` が常に `False` となり、live では `turn_failed_no_durable_gate` に到達できなかった (fake 差替えの test は全て緑のまま)。現在 `pin_revision` は **required keyword** であり、渡し忘れは silent な never-binds ではなく call site の `TypeError` になる。
  - ★**`worktree_readable` は cleanliness を主張しない**。dirty はそのまま保全対象であり、**unreadable だけが block** する (#13806 token `dirty_state_unreadable` を verbatim 共有)。close は process を 1 つ終えるだけなので working tree の byte は触られず、fresh worker は同じ checkout に再 join する。
  - ★**`--worker-revision` は空 pin では一致しない**。`recover-stale` のread-only観測は後方互換として空pinを扱えるが、#14663以後の破壊的executeはstructured approvalをparticipant revisionへ束縛するため空pinを拒否する。live workerの破壊的refreshも従来どおりunpinned generationに乗らない (#14203 j#87364 F5 と同じ規律)。
- **execute (destructive, owner approval 必須)**: 上記に加え `--journal <owner approval j>` `--action-id refresh-worker:<lane>:<role>:<provider>:<name>:<locator>:r<revision>` `--action-generation <n>` `--execute`。`turn_failed_no_durable_gate` + 全 fence green のときだけ、**exact worker generation のみ** close → same-slot fresh launch → action-bound attestation → **既存 anchor を governed handoff rail で exactly-once resume**。IR/RR は再生成しない。
  - ★**approval は canonical structured marker で要求する。散文の包含では成立しない** (#14661 review j#92443 F2 → j#92487 F1)。`--journal` の journal が anchor issue 上に **一意に実在**し、その本文が本 surface の approval gate marker を **ちょうど 1 個**持ち、**全 field が exact 一致**することを要求する。実装は既存の硬化済み `composer_discard_approval.verify_composer_discard_approval` と同型 (方言を作らない)。preflight の `--json` / text は **`required_approval_marker`** として「記録すべき marker そのもの」を出すので、operator はそれを承認 journal に貼る。
    - ★**substring 一致では 4 通りの未承認 close が通っていた** (j#92487 F1 実測): 否定文 (「承認しない: <token>」) / code fence 内の retry command 引用 / log 行 / **`:g30` の承認が `:g3` を prefix 包含**。marker は code fence 内では parse されず、generation は digest 入力として **field 等価**で比較されるため、いずれも構造的に不成立になる。
    - field は `gate` / `version` / `approval_source=direct_owner` (destructive operation は standing delegation の carve-out) / `decision=approved` (**明示的な肯定**) / `effect` / `issue` / `lane` / `action_digest`。action id と locator は `:` を含み marker 文法を壊すため、**digest で運ぶ** (`pin_digest` precedent)。
    - reader 未配線 / snapshot reader / 読取不能 / journal 不在 / journal 重複 / marker 0 個または 2 個以上 / 任意 field 不一致は**すべて zero-close**。
  - ★**issuer authority は「誰が記録したか」と「誰の判断か」の 2 軸を要求する** (#14661 Design Answer j#92641)。marker が `approval_source=direct_owner` と*名乗る*ことは何の証明にもならないので、承認 journal の **writer role** を durable に解決し、かつ marker が `direct_owner` を宣言していることを**連言**で要求する。
    - **writer role = `coordinator`**。governed preset は owner 判断の収集・記録を coordinator role へ集約するため、durable な approval journal を書く actor は coordinator である。これは「coordinator が owner である」という意味ではない — owner の判断であることは marker の `approval_source` が別軸で担う。
    - role は既存の **単一 gate→writer-role authority** (`contract_writer_role`) で解決する。解決が成立するには **authority anchor** が要り、anchor 無しの bare role token は「解決済み」として扱わない。
    - ★**authority anchor は 3 部の合成であり、各部が証明する範囲は別である** (#14661 review j#92767)。実形は `<gate固有 ruling> <committed config blob> evidence:redmine:j#<journal>:gate=<gate>` (space 区切り)。
      - (a) **gate 固有 ruling** = その gate の writer-role contract を**実際に決めた** durable record。本 gate `worker_refresh_owner_approval` は **`redmine:#14661:j#92641`** (Design Answer)。
      - (b) **committed config blob** (`git:.mozyo-bridge/config.yaml@<blob>`) = commit 済の role/provider binding。**この file は gate→role mapping を持たない** (`agents.profiles.*.provider` 等の provider binding だけ) ため、**単独では writer contract を証明しない**。blob は git の tracked object から読む (working tree の file ではない — 破壊操作を要求する actor が書き換えられてはならない)。
      - (c) **exact evidence (journal id + gate)** = この解決が「**どの record の、どの gate について**か」を束縛する。lane-scoped role ではさらに envelope (`workspace` / `lane` / `lane_generation`) が付く。
      - ★**(a) を repo 横断の 1 本にすると、その gate について沈黙している ruling へ全 gate が帰属する** (R6 の実欠陥。#14661 review j#92715)。anchor 文字列は非空なので `is_anchored` は通り、**空 anchor の拒否だけではこの誤帰属を検出できない**。**辿った先がその gate について沈黙している ruling なら、辿れたことにならない**。したがって ruling は role と同じ場所で **gate ごと**に持つ (`contract_ruling_pointer`)。
      - **既存 hibernate-evidence gate (`park_declared` / `review_result` / `required_ci_green` / `integration_disposition` / `dogfood_delegated`) は `redmine:#14219:j#85530:Q3` を維持する**。本 command の追加で既存 evidence の anchor 文字列は 1 文字も変わらない (**re-attribution しない**)。
      - **no-change Review waiver gate `no_change_review_waiver` の ruling は `redmine:#14695:j#93412`** (Design Consultation Answer)。writer role は `coordinator`、provenance 軸は marker の `approval_source=direct_owner` で、両者は連言であり代替関係ではない (#14695 j#93412)。**同一 consultation の先行 Answer j#93406 を pointer にしない** — 両者とも writer を `coordinator` と裁定しており binding 自体は同じだが、hard carve-out と live 測定境界を持つのは j#93412 だけなので、j#93406 を指すと `is_anchored` は通るのに**現行契約を述べていない record** へ読者を送ることになり、(a) が防ぐ誤帰属と同型になる。この gate も (b)(c) の合成規則と fail-closed 条件は既存 gate と同一で、既存 5 gate の anchor 文字列は 1 文字も変わらない。
      - **global offline rollout gate `herdr_offline_rollout_owner_approval` の ruling は `redmine:#14838:j#97993`**。writer role は `coordinator`、全workspace停止・3-store migration・runtime cutoverを承認した判断のprovenanceはmarkerの `approval_source=direct_owner` で、両者を連言する。full approval manifest + issueのdigestをmarkerへ束縛し、source-system author ID一致はauthorityに使わない。(b)(c) の合成とunanchored refusalは他gateと同じで、既存gateのruling pointerを変更しない。
      - **post-reboot pair recovery gate `restored_pair_recovery_owner_approval` の ruling は `redmine:#15227:j#102879`**。writer role は `coordinator`、owner 判断は marker の `approval_source=direct_owner` と連言する。pair 全体の lifecycle / participant / worktree pin と composer loss の許可を action digest に束縛し、既存 gate の ruling pointer を変更しない。ただしこのgateはconditional-close導入後のために予約されたdormant contractであり、現行read-only診断はmarkerを生成せず、gateを破壊操作へ使わない。
      - **ruling を持たない gate は unanchored** となり、本 surface は zero-close で refuse する (fail-closed)。
      - 本節の (a) と既存 gate の維持は **test が code から導出して照合する**ため、runbook と実装のどちらか一方だけを変えると赤化する (前 3 round 連続で docs drift を出したため綴り検査ではなく導出照合にした)。
    - ★**旧実装の「approval journal の author == issue の author」は撤去した** (#14661 review j#92601 F1)。実測で worker / gateway / coordinator の全 role が同一 source-system user id で書いており、この述語は **issue 上の全 journal が満たす**。何も証明していなかった。
    - `coordinator` が **standing delegation** の承認を中継した記録は不成立 (destructive operation は carve-out で `direct_owner` 必須)。非 coordinator が `direct_owner` を名乗った記録も不成立。
  - ★**承認は operation 全体を拘束する** (review j#92533 F2)。`action_digest` の入力は action id と action generation **だけではない** — lane lifecycle revision / generation、anchor issue、resume anchor journal、resume gate も含む。これらが digest 外だった間は、1 つの marker で **別の resume anchor・別の gate・別の lane generation** を承認できてしまった (4/4 実測)。「この worker を close してよい」は「そして別の anchor へ resume してよい」ではない。
  - ★**相反・malformed field は肯定に化けない** (review j#92533 F2)。共有 marker parser は last-write-wins で `decision=declined:decision=approved` を `approved` に潰すため、approval は**専用の厳格 parser**で読む: duplicate key / 空 key・値 / 未知 extra field / canonical field set 不一致をすべて fail-closed にする。governed field の exactly-one 規則を、この surface が実際に gate する field へ適用したもの。
  - #14663で残存差分を解消した。`recover-gateway` / `recover-stale` は共通 `recovery_owner_approval` contractとstrict marker scanを使い、既存 `worker_refresh_owner_approval` は公開marker byteを維持しつつ同じstrict scanへ委譲する。gate tokenとeffect/digestはsurface別なので相互流用できない。
    - `gateway_recovery_owner_approval` のwriter-role ruling: `redmine:#14663:j#99195`。
    - `stale_worker_recovery_owner_approval` のwriter-role ruling: `redmine:#14663:j#99195`。
  - action id prefix は `refresh-worker:` で、`recover:` (#13806) / `refresh-gateway:` (#14203) と **disjoint**。同じ slot の 2 つの回復は別 admission であり replay fence を共有してはならない。sibling の action id を渡しても alias として受理されず refuse する。
  - resume の transport は #14203 と同じ `--kind reply` pointer (j#84223 shape)。`recover-stale` の `redispatch_gate` は **再利用しない** — あの rail は `dispatch_to_worker` が `kind="implementation_request"` を hardcode する一方で確認 marker を `expected_gate` から組むため IR anchor 専用であり、live worker refresh は lane が実際に詰まっている任意の resumable gate (#14658 の形は `review_result` round) を resume する必要がある。
- **zero-close / zero-send の matrix**: durable progress 着地済 / busy・working / delivery 不確実 / identity・generation・branch・worktree drift は **すべて close 0・send 0**。regression test が 1 行 1 規則で pin する。
- ★**durable progress を inner close の呼び出し直前で再 join する** (#14661 review j#92533 F3 → j#92656 F2)。run 冒頭の turn 分類を使い回すと、**worker が preflight と close の間に自分の gate を land させても close する** (実測: `observe_turn_calls=1` / `closed_count=1`)。これは本 surface が守るべきもの — dirty worktree を抱えた worker の作業 — を破壊する経路そのものである。**再読の位置は「効果の呼び出し直前」でなければならない**。actuator は preservation 判定と close の間で lease を再認証する (`_reauth_before_effect`: store get / renew / get) ため、preservation 段の guard は**最後の外部観測ではない** — その窓で gate が着地しても close していた (j#92656 F2 実測: `turn_reads=3` / `closed_count=1`)。現在は close-boundary wrapper の `close_exact_generation` (= inner close の直前) で再 join し、landed / ambiguous / unreadable のいずれでも `close_refused_durable_progress_moved` で zero-close する。post-close replay は guard を渡さない (既に close 済で、replay は transaction を完了させるために存在する)。
  - 「無関係な gate の着地で承認済み action を撤回すべきでない」という理由づけは**誤り**だった。本 surface の progress 検出器は lane-bound か、provenance 不明なら progress として数える設計であり、**「無関係」と断定できる経路を持っていない**。自分の検出器が判定できないことを上位で断定してはいけない。
- ★**close 直前に target も再観測する** (#14661 review j#92487 F2)。preflight と実際の close の間には durable approval 読取・rail probe・transaction plan が挟まり、その窓で worker は turn を始め・permission prompt に入り・composer に未送入力を得られる。したがって `drive_worker_recovery` の**直前**に target を再観測して verdict を再導出し、drift していれば自分の typed blocker (`worker_not_settled` / `pending_composer_input` / `stale_generation` / `gateway_not_distinguished` …) で **zero-close** 拒否する。turn 分類は再導出**しない** (承認が下りた durable history の判断であり、無関係な gate の着地で承認済み action が途中で撤回されるべきではない)。**post-close replay では skip する** — そこでは pinned worker の不在が期待状態であり、再導出すると完了させるべき replay を全部拒否してしまう。
  - ★さらに **close boundary 自体**を settled 要求で塞ぐ。共有 #13806 boundary は runtime を `running_process = (state == busy)` の 1 boolean に潰すため、実測で **`blocked` (permission prompt 待ちの生きた agent) / status 欠落 / 未知 token / `unknown` がすべて `may_close=True`** になっていた。本 surface は共有 port を薄く wrap し、**positively settled (`turn_ended` / `awaiting_input`) を観測できない限り close を塞ぐ** (composer も同時に再読)。identity / lifecycle / row-revision の判定は共有実装のまま (再実装しない)。同じ fail-open は sibling 2 surface にもあるが changed-path 境界外のため触らず報告する。
- ★**resume は action-bound rail を既定にする** (#14661 review j#92487 F3 → j#92533 F4)。従来 `target_action_id` を渡していたのは one-shot rail だけで、governed rail は logical slot を再解決して送り、confirmation も秒精度の `observed_at` だけを見ていた (#14203 j#87445 が「同一秒の 2 launch は timestamp を共有する」として既に否定した identity)。現在は **send 直前**と **confirmation** の双方で `LiveRecoveryAnchorDeliveryService.preflight` を通す。
  - ★さらに **public `preflight` は advisory であり authority ではない** (delivery service 自身の実装コメント)。`deliver()` は irreversible edge で完全な preflight を再実行し、target revision と replacement action を **transport まで**運ぶ。一方 governed CLI の argv は locator + lane しか持たないため、binding 確認と injection の間に slot が recycle しても検出できない (実測: `action_binding_checks=1` で recycle 後も `sent`)。したがって **delivery service が使えるときは常にそちらを既定**とし、governed CLI は service 利用不能時の fallback に限定する (rail を削除すると governed rail しか解決しない環境を壊すため残す)。fallback では `_drive_cli` 直前に **locator 再解決 + action binding 再 join** を置き、locator が動いていれば zero-send。
- **partial failure**: replacement transaction が replay fence を保持し、re-run が resume する。close 後 crash の replay は `identity_unknown` + committed-close transaction のみ admit。**pinned worker がまだ解決する間に lane authority が壊れていれば verdict は `launch_authority_unavailable` で outright refuse** (close 済 worker を relaunch 不能にしないため)。committed close 後の replay は admit されるが、authority が壊れていれば追加 close 0・launch 0・send 0 で `stopped` を維持する。
- **同じ失敗が 2 度起きた lane も回復できる**: anchor delivery record は **anchor 自身の gate kind と `reply` の両方** を受理する (前回 refresh の resume pointer も同一 anchor の同一 worker への配送である)。片方しか見ないと 2 回目が恒久的に `turn_unconfirmed` になり、#14661 が解こうとした exact-once gap が 1 round 後に再発する。
- **typed outcome**: `--json` payload と text 出力の双方が、全 outcome (preflight / refused / stopped / completed) で `launch_authority_reason` (closed token) を出し、blocking 時は `launch_authority_runbook` を伴う。`launch_failure_reason` は fence が起きたときだけ text 行に出る (`null` = 「launch fence は起きていない」)。#14475 j#88485 のとおり、actuator 停止時・resume authority refusal 時は canonical evaluator を **再読** して action-time の axis を報告する。
- 実装正本: `domain/worker_turn_recovery.py` / `application/sublane_worker_refresh*.py` (#14661)。turn 分類器・launch authority・launch failure・replacement transaction / actuator / continuation drain は #14203 / #14475 / #14480 / #13806 の既存 module を **共有**する (新方言を作らない)。

### `recover-pair` の worktree binding fence (#14475 review j#88477 F1)

`sublane recover-pair` の preflight も **同じ closed `LAUNCH_AUTHORITY_*` 語彙**で lane の canonical worktree binding を判定する (surface ごとの方言を作らない)。軸は **token だけではない** (review j#88505 F1): token は worktree の *path* から導出されるため、checkout を別 branch へ切り替えても・解決しなくなっても一致し続ける。したがって `worktree_identity` 非空 + token 一致に加えて、**checkout が解決すること** と **current branch == lane id** を要求する（branch は `_norm_lane` する前に **raw 値の非空**を先に確認する。`_norm_lane("")` は `default` を返すため、正規化を先にすると読み取り失敗が lane `default` で green になる — review j#88513 F1） (`lane_authority_reason` が owed launch 前に再 join するのと同じ 3 軸)。いずれかが不成立なら `may_recover=false` で、**close / relaunch / resume / redispatch いずれも 0**。
さらに **各 destructive effect の直前ごとに**同じ軸を action-time で再読する (review j#88526 F1 / j#88532 F1 / j#88538 F1): 各 close の直前、relaunch の直前、resume の直前、redispatch の直前。**さらに「呼び出しの直前」では足りない**軸が 2 つある — resume は自身の preflight（live pair / attestation / pins 観測）を挟んでから disposition CAS へ進み、delivery は自身の preflight（target 解決 / rail 準備）を挟んでから transport へ進む。したがって **`transition_disposition` CAS の直前**（`commit_authority` seam）と **`drive_turn_start` の直前**（`pre_transport_authority` seam）にも再 join を置き、そこが最後の外部観測の後になるようにする。resume は「fresh pair が same lane / worktree に立っている」前提で `active` へ CAS し自身は branch を再読しないため、send は wrong branch 上の pair へ work anchor を届けることになるため、いずれも checkout 非依存ではない。live 側は **2 段**で再 join する (review j#88547 F3): (a) `pre_send_authority` = **reserve-edge の早期 rejoin**（outbox reserve 獲得直後、target 解決と delivery preflight の**前**）。moved なら reserve を cancel して typed zero-send にし、reserve を宙吊りにしない。(b) `pre_transport_authority` = **delivery-edge の最終 rejoin**（delivery 自身の preflight の後、`drive_turn_start` の直前）。(a) だけでは delivery preflight 中の drift を覆えないため、両方が要る。preflight は operator 判断の前の観測であり、その後に branch が切り替わり得る。**「最初の close の前に 1 回」では不十分**で、close と close の間・最後の close と relaunch の間の切替を素通りする。drift 時は以後の close / relaunch / resume / send を 0 にして停止し、既に committed 済みの close は partial-close replay state として報告する。outcome は 3 つの独立した事実を分けて述べる (review j#88538 F2 / j#88554 / j#88563)。

- **`effects`** = この実行が **適用したと判明している** effect のみ。closed vocabulary は `closed_bad_generation` / `relaunched_pair` / `resume_disposition_committed` / `implementation_request_redelivered`。`executed` はその非空性。
- **`unresolved`** = 試行したが durable な fate を確定できなかったもの（`redispatch_fate_unresolved`）。**effect ではない**。
- ★これらは **status 文字列から再推測しない** (review j#88571 F1)。`uncertain` は「reserve 前の zero-write」と「transport 開始後に ledger write が失敗」の**両方**に付くため、同じ token が zero-send と既知の redelivery を指す。redispatch edge は typed な観測（`status` / `delivered` / `zero_send` / `unknown_fate`）を返し、application はそれを**無損失で**運ぶ。したがって「transport 開始済み + ledger write 失敗」は `implementation_request_redelivered` を **effect として保持しつつ** fate 未確定も報告し、「cancel 成功の zero-send」は unresolved を**空**にする。
- **`attempted`** = `--execute` の admission を越えて actuation に入ったか。`is_blocked` はこれを見る。`executed`（何か変わったか）を「実行したか」の代理に使うと、first-close failure（適用 0）が blocked と読めなくなる。

**どの分岐でも固定値にしない**。全 return path が単一 composer を通り、`RecoverPairOutcome` / `RecoverPairDeliveryRetryOutcome` は `__post_init__` で「effects ⊆ 語彙」「`executed == bool(effects)`」「**`(effects or unresolved) ⇒ attempted`**」を**構造的に検証**する（矛盾した outcome を構築不能にする。review j#88571 F2）。close failure / relaunch failure も例外ではない（first-close failure は適用 0、second-close failure は先行 close を報告する）。

redispatch の terminal 分類は**全域**とする: fresh delivery = effect / already-redispatched = no-op（unblocked）/ **`target_retiring` = reserve cancel の blocked zero-send**（「already delivered」と誤報しない）/ failed・uncertain = unresolved fate（blocked）。terminal success 判定は **main / retry が同一の closed policy**（`REDISPATCH_TERMINAL_SUCCESS` = delivered / already のみ）を共有し、未知 token は既定で blocked とする (review j#88571 F2)。retry surface（`recover-pair-delivery`）も同じ effect / fate / attempted 契約に従う。`detail` は実 disposition と一致させ、**JSON も text も全 path で** applied と unresolved を出す — 空のときも `applied: nothing` / `unresolved: none` と明示し、preflight-blocked でも省略しない（field の不在から operator に推測させない。review j#88571 F3）。
なお **`is_blocked` は `executed` に依存させない** (review j#88554 F1): nested resume blocker を先に評価する。順序を誤ると「effect を truthful に直したら blocked が消える」という結合が生まれる (re-run はその slot を `slot_absent` と見て relaunch する)。outcome は preflight 時の値でなく **action-time の軸**を報告する。blocker は `lane_worktree_binding_unverified:<axis token>` の形で `blocked_reasons` に載り、preflight payload は `worktree_binding_current` / `worktree_binding_reason` / `worktree_binding_runbook` を出す。probe を持たない古い ops adapter や例外を投げる probe は `unknown` = fail-closed（green default に乗らない）。

- 回復手順は **row の disposition で分岐する** (review j#88490)。使える bounded write surface が違うため。
  - **active** row: **lane 自身の declaration surface を再実行**する (`sublane create` の self-heal 再実行)。create path の bounded backfill は row の現在の pins を exact に保持したまま空の worktree field だけを CAS で埋めるため、**pins を持つ row でも成立する** (#14462 の形)。
  - **hibernated** row: `sublane repair-worktree-binding`（#14475、下記）。#13809 backfill は active 専用なので hibernated row には構造的に届かない。
  - いずれも非空で異なる binding は上書きしない。

### `sublane repair-worktree-binding` (#14475 review j#88490)

hibernated / released で `declared_slots` は在るが `worktree_identity` が空、という **どの既存 surface も収束させられない**形の public な metadata-only 回復。#13879 `repair-pins`（worktree 非空・pins 空）の**鏡像**であり、両者の signature は構成上排他。既定は read-only preflight、`--execute` で **lifecycle payload field は `worktree_identity` の 1 つだけ**を exact revision+generation CAS で書く（同 UPDATE は本 component の全 CAS と同様に decision anchor / revision / `updated_at` の audit metadata も更新する。review j#88496）。**process launch / close / resume / send は 0**、worktree / branch も触らない。lane は hibernated のまま残り、以後は `recover-pair` が担当する。

- row 側に比較対象の token が無い（それが欠陥そのもの）ため、`--worktree` の自己申告は信用せず **positive evidence** を要求する: (1) 実在 git checkout の **root 自身**であること（subdir は同じ branch を答えるが別 token を導出するため不可）、(2) canonical resolver で **lane record と同一 workspace** に解決すること（別 repository の同名 branch を排除。branch 名は identity ではない — review j#88493）、(3) 現在 branch が lane id と一致すること。
- token は **canonical (symlink 解決済み) root** から導出する。`derive_lane_workspace_token` の契約が resolve 済み path を要求しており、未解決だと symlink alias 経由の repair が「後続 live probe が再導出できない token」を記録してしまう (review j#88494)。同じ理由で live 側の binding probe も resolve 済み root から導出する。
- 既に**同一 token** で bound なら idempotent no-op、**別 worktree** に bound なら zero-write 拒否（この surface は隙間を埋めるだけで lane を移動させない）。
- read-only preflight と store CAS は **同一の pure classifier** (`core/state/lane_worktree_binding_signature.classify_repair_signature`) で row signature を判定する (review j#88526 F2)。「preflight 側にも全軸を書く」方式は 3 round 連続で取りこぼした (軸なし → 一部投影 → 正規化差) ため、**drift 不能な構造**にした。軸は disposition / `binding_kind == issue`（project scope とは独立。**この軸だけは CAS 自身が normalize するため raw 比較の例外**）/ issue / project scope / release / replacement / pins（非空・validator 通過・**canonical encoding 一致**）。
- classifier の **外に残る residual predicate**（already-bound の worktree_identity）も同じ raw semantics に揃える (review j#88532 F2): normalized token 一致は idempotent replay、**raw** 非空は conflict、raw 空のみ repairable。ここを normalize すると whitespace-only binding が「unbound」に潰れ、classifier で閉じたはずの false green が classifier の外で再発する。
- 比較は **raw** で行う。CAS が persisted bytes を比較するため、padding を含む row (`' 14475 '` / `'   '` / `' released '` / 前後空白付き canonical JSON) は **preflight と execute の双方が同じ typed blocker で拒否**する。preflight 側だけ normalize すると「dry-run green → execute `repair_cas_refused`」の false green になる。一部しか投影しないと「dry-run は green、`--execute` は `repair_cas_refused`」という **false green** が生じ、それが owner approval の根拠になり得る（preflight が自分の effect を予測できていない = 本 issue の主題そのものの再発）。
- repair の `--journal` は row の **decision anchor を更新する**（sibling と同じ。lifecycle row は常に「現在の状態にした durable record」を指す、`transition_disposition` R1-F5 規則）。**issue binding** / pins / disposition / generation は不変 (review j#88493 / j#88495)。worktree binding は空→canonical token へ更新される — それが本 command の目的そのものであり、ここで「不変」なのは issue binding の方である。

## Host reboot recovery (#13518)

host (Mac 等) が再起動されると lane pane の Claude/Codex TUI は exit するが、`herdr agent list` の durable assigned-name row は残る (foreground は `-zsh` のみ、detected agent 無し)。**複数正本を照合する fail-closed recovery reconciler** を使い、DB 単独を authority にしない (設計正本: #13520 j#75276)。

- **state を混同しない (authority matrix, #13520 j#75276)**: Redmine issue/journal = workflow gate と durable anchor / Git worktree・ref・diff = code と dirty state / `registry.sqlite` + repo-local anchor = workspace identity / `state.sqlite` = lane metadata・callback outbox の復元材料 (workflow truth ではない) / herdr assigned-name + live inventory = runtime liveness / launch-time sender env = 再 attest する process-local input (永続 authority にしない)。
- **composite liveness で false-positive adopt を防ぐ (#13518 j#75329)**: `herdr session-start` の adopt 判定は assigned `name` 一致だけでは不十分。`agent list` row を `classify_named_slot` (`domain/herdr_slot_liveness.py`) で複合判定し、detected agent 不在 + `agent_status=unknown` の **shell residue** は `stale_named_slot` として outcome `stale` で surface する (blind adopt しない / 名前が残っているため launch も上書きしない)。detected agent が名指しされた live slot は従来どおり adopt、liveness signal を一切持たない minimal row も従来どおり adopt (self-heal 不変)。
- **dirty worktree を never-clobber**: recovery 中に lane worktree を reset / stash / delete / recreate しない。未 commit 成果は保全して同一 durable anchor から resume する (12-file dirty diff を SHA-256 で preflight/post-check して不変を確認した実例: #13518 j#75331/j#75334)。
- **stale shell-residue pane の close + same-slot relaunch は destructive** ゆえ **owner-approved recovery gate** を要求する (replayable に journal 記録: #13518 j#75331)。この履歴例は、agent processが無いlogical pane自体のcloseをownerが承認したscopeである。exact terminal generationだけを閉じる #15227 active-sublane recoveryには適用せず、raw `herdr pane close`で代用しない。
- **projection cache を authority にしない**: `sublane status` の `panes=[]` は stale projection でありうる。live assigned-name inventory と矛盾する場合は同じ reconciler で fail-closed に扱い、runtime 不在と即断しない。
- **env 欠落に注意**: reboot 後に adopt された既存 process は launch-time `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` / `MOZYO_LANE_ID` を欠くことがある (session-start adopt は retroactive 注入しない)。正規 dispatch が `missing_sender_env` で fail-closed した場合、registry/anchor/live assigned-name から検証した値を **その 1 回の** high-level dispatch child process にだけ再注入する (env spoof / 別 role 偽装はしない)。Herdr backend では tmux 専用の `mozyo-bridge init` hint は無効 (`TMUX_PANE is not set`)。
- **fail-closed 条件**: workspace mismatch / missing・unreadable journal / ambiguous live slot / DB と Redmine・Git の矛盾は停止。implementation/close/integration/publish を自動承認しない。

## lane retire (guarded close)

1. lane worktree の dirty を確認・復元: `git -C <worktree> checkout -- .claude/settings.local.json` (agent harness が触る唯一の常連 dirt)。**dirty のままだと retire は `dirty_worktree` で fail-closed する** (正常動作、#13331 j#73339 guard)。
2. `sublane retire --issue <id> --lane-label <label> --worktree <path> --branch <branch> --issue-closed --callbacks-drained --verified --durable-record --target-identity-known --execute --json` → **対象 lane unit の managed slot のみ** close (#13602 Option A: routine green-preflight retirement は coordinator authority。`--owner-approved` flag は無い。`--issue-closed` は「対象 issue が種別ごとの close 契約を満たして closed」を表す — child Task/Test/Bug は `task_close`(owner_close_approval なし)、US / standalone issue は owner_close_approval-backed close (central preset `US-Level Audit Model`)。retire actuation はどの契約でも owner close approval を再収集しない。未解決の owner-approval-waiting は `--callbacks-drained` 側で block する) (#13377: project workspace・coordinator pair・他 lane は閉じない。最終 lane の close で sublane host workspace が herdr により自動消滅するのは無害な付随挙動で、retire の前提・完了条件ではない — #13380)。legacy lane (`wt_<hash>` workspace) は互換 plan で旧 slot も close される。
3. `workflow supervisor --run-once` / `--watch` は callback/backlog delivery後・hibernate前に、同じleaseとpass-wide mutation budget下で一意な終了済み候補を自動的に上記と同じtyped退役処理へ渡す。全evidenceの2回完全一致、exactly-one、close/review/integration/CI/callback/clean/origin条件を満たさない場合はzero-mutation。
4. worktree / local branch の除去は **統合後のoperator runbook** (`git worktree remove` + `git branch -d`)。自動退役の結果は常にphysical cleanupを`cleanup_blocked`として残す。forceとremote branch削除は禁止する。

## session-start が片role分だけ起動して失敗した場合 (#13948)

`herdr session-start` は #13948 以降、requested role すべてが **launch した locator に live / startup screen clear /
locator-matched self-attestation** を観測できるまで success を返さない。片方だけ落ちた run は **exit 非 0** で、role ごとに
原因を名指しする (`provider_exited` / `shell_residue` / `startup_interaction_required` / `receiver_unreadable` /
`attestation_timeout` / `attestation_mismatch` / `locator_drift` / `inventory_unreadable` / `unprofiled_provider` /
`attestation_unavailable`)。**この run 自身は何も close しない**。

1. 出力 (text の `action=` / `--json` の `action_id`) から **startup action id** を取る。rollback はこの id の下でしか動かない。
2. read-only preflight: `mozyo-bridge herdr session-rollback --action-id <id> --json`
   - 何が閉じられ、何が閉じられないかを role ごとに返す。ここでは **一切 close しない**。
3. preflight が `state=actionable` なら `--execute` を足す。`state=actionable` は「**settled でない (=解消すべき) refusal participant が
   残っていない**」の意味であって「全 participant が `eligible`」ではない。settled = `eligible`(close-target) ∪ `absent`・`already_closed`
   (no-target)。**close されるのは `eligible` の participant だけ**で、
   `absent`(既に居ない) と `already_closed`(前回の実行が閉じ済み) は **no-target だが実行を block しない** — 途中まで閉じた
   rollback を再実行すると残りが `eligible`・前回閉じた role が `absent|already_closed` になるのが正規の resume 形であり、
   ここで operator が止めてはならない (それが `--execute` を再度足す意味)。`eligible` が対象になるのは **この action が起動した
   participant** だけで、adopted slot・別 action の slot・durable name だけ一致する pane は決して閉じない。preflight が
   1 つでも close-target でない live refusal (下記) を残す間は `state=blocked` で、`--execute` も何も閉じない。
4. refusal はそのまま原因である。緩めない:
   - `rollback_authority_unavailable` — startup transaction store が読めない/壊れている/別 store に置換された。
     raw error にはならず structured に refuse し、**close は 0**。store を直さない限り再実行しても閉じない。
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

### active-lane `sublane quarantine` のapprovalを組み立てる (#14234)

`sublane quarantine --execute` は `--assigned-name` / `--locator` / `--action-generation` /
`--approved-revision` / `--approval-observed-at` をすべて要求する。これらは**推測してはならない** exact
generation tokenであり、`sublane list` は返さない。raw Herdr・内部Python API・pane bodyを使わずに組み立てるには、
公開read-only surfaceを使う。

```
mozyo-bridge sublane quarantine-inspect --issue <id> --lane <lane> --role <role> [--repo <root>] [--json]
```

- read-onlyである。store write、close、launch、Herdr mutationを行わない。
- managed inventoryをidentity decodeで一度だけ読み、既存の #13763 quarantine inspectorへ**同じsnapshot**を渡して
  classifyする。したがってdiscoveryとclassificationが別のinventory readでdriftしない。`sublane list` とは責務が異なり、
  こちらは「approvalがどのexact generationを束縛するか」を答える。
- `approval_ready=true` のときだけ、貼付可能なowner approval記録と exact `--execute` command lineを出力する。
  approval journal idは**placeholder**であり、owner が実際に記録した後に実idへ置換する (id を予測しない)。
- composer本文・hash・length・raw ANSI・path・credentialは出力しない。出るのはidentity / revision / generation
  tokenとclassificationだけである。
- 組み立て不能な場合は typed reason でfail-closedし、exit非ゼロになる: `inventory_unreadable` /
  `composer_unreadable` / `receiver_absent` / `duplicate_receiver` / `revision_unreadable` /
  `attestation_unreadable` / `known_marker_requires_q_enter` / `not_quarantine_candidate` /
  `workspace_unresolved`。**refusal時はtemplateを出さない** (execute側fenceが拒否するapprovalを貼らせないため)。
- `known_marker_requires_q_enter` はreceiver replacementではなく、既存delivery railのq-enterで処理する。
- receiverのrevision / attested generation / locatorが変化したらapprovalは無効である。`--execute` は実状態と
  再照合してfail-closedするので、driftしたapprovalは適用されずに拒否される。inspectを取り直してapprovalを出し直す。

### pending composer がpairをpreserveしている場合

`converge-bound-pair` が `pair_contains_preserved_slot` / `preserve_pending_composer` を返したら、そのcommandへ
forceやpending overrideを加えない。pending generationが本当に破棄可能かを別のread-only railで測る。

1. `mozyo-bridge sublane prepare-bound-pair --issue <id> --journal <decision-journal> --lane <lane> --worktree <path> --branch <exact-branch> --repo <target-root> --json`
   - `state=actionable` の場合だけ、出力された `bound_pair_composer_discard_approval` markerをowner approval
     journalへそのまま記録する。markerはlifecycle revision/generation、worktree+branch、full slot snapshot、discard
     role setを束縛する。旧active-lane `sublane quarantine` のapprovalやproseは代用できない。
   - correlated markerは既存delivery railで処理する。busy/tool-child、unknown/ambiguous/foreign/newer、dirty worktree、
     branch mismatchはzero-closeのまま原因を解消する。
   - `--repo` (execution root) は lane identity を**変えない** (#13933 R7、design answer j#81046)。lane identity は
     `--worktree` の root の KIND (git worktree か否か) だけで決まる。lane worktree 自身を `--repo` にしても、別の root を
     `--repo` にしても、同じ lane は同じ identity を derive する。以前は `resolved == repo_root` の偶然で `wt_`/`dl_` が
     切替わり、lane worktree から実行すると row と食い違って `not_hibernated_released_bound_pins_empty` で block した (#13846 j#81024)。
   - block した場合、detail が破れた axis 名 (`worktree_identity_mismatch` 等) を列挙する。`resuming=false` のときは
     `resume_diagnostic` が理由 (`approval_source_unreadable` = credential 未設定 / `no_matching_approval_marker` = approval 不在 /
     `no_action_owned_progress` = 所有 action 無し / `projected_still_blocked:<reason>` = 別 axis で block) を示す。credential 欠落と
     真の block を混同せず、示された axis を解消する。
2. 同じcommandへ `--execute`を加える。credential-gated live Redmine readでexact markerが一致したときだけ、承認された
   uncorrelated pending roleをguarded closeしaction-bound relaunchする。pins write、resume、dispatch、sendは行わない。
   partial retryは同じimmutable transactionのclose proofだけを使い、別 locatorや任意のabsent slotを成功扱いしない。
3. `--execute` が `replacement_stopped` / `effect_failed` で止まり、pairが片側だけ閉じた状態 (例: gateway missing +
   old worker remains) になった場合は、**同じcommandをそのまま再実行する** (#13933 R6、live evidence #13846 j#80934)。
   - preflightは `state=actionable` かつ `resuming=true` を返し、**新しいapproval markerを出さない**。既に記録済みの
     approval journalが引き続きauthorityであり、追加のowner approvalは不要。`action_id` は初回と同一である。
   - `--execute` は同じimmutable transactionをreplayし、閉じ済みroleはrelaunch/attestationから、残りroleはguarded
     closeから再開する。**新しいpairをdiscardする権限は増えない**: この間に到着した productive / correlated /
     ambiguous composer、復帰したforeign sibling、revision/generation/worktree/branch driftはzero-close/write/send。
   - `resuming` が出ない block は本当のblockである。preflight結果を無視して`--execute`を繰り返さず、原因を解消する。
4. `state=prepared` 後に `converge-bound-pair` のpreflightを**取り直す**。その新しいslot snapshotに対する別approval
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
- fail-closed 軸: inventory unreadable / duplicate assigned name / foreign occupant / locator 欠落 / busy agent / pending composer / 同一 locator への衝突 → すべて **zero-close**。pending composer の判定は #14239 以降 hibernate rail と同じ #14065 provider ghost gate を通した ghost-refined observation であり、provider が宣言する `dim` ghost placeholder だけの composer は settled として扱う (discard approval 不要)。`normal` 描画の実入力・unreadable/未解決 render・read error は従来どおり preserve され、実入力の discard には引き続き下記の exact direct-owner approval が必須。
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

### 再起動復元後の active sublane pair を診断する (#15227)

`sublane recover-restored-pair` は、active な issue-owned sublane の gateway/worker が
両方 live だが、再起動復元後の command-shell CWD が lifecycle row の canonical worktree
と一致しない、または live locator に結び付いた startup self-attestation が non-green な
場合の公開read-only診断である。default lane と片側だけ存在する pair は本診断の対象外
である。default coordinator を安全に self-close/relaunch する公開 rail は現時点では未実装
であり、本コマンドで代用してはならない。片側だけ存在する pair は vanished-slot 用 rail
を使う。

- 現行で提供する動作は read-only preflight だけである。`--allow-pending-composer-loss` は、
  eventual replacementで旧 pane の未送信composer textが失われ得るかを診断結果へ含めるための
  入力であり、close、file破棄、またはowner approvalを実行するflagではない。
- preflight は lifecycle の issue/lane/revision/generation、canonical worktree token、branch/HEAD、
  gateway/worker の assigned name/locator/inventory revision、runtime state、CWD、startup
  attestation を結合する。runtime は両 slot とも明示的な `awaiting_input` または
  `turn_ended` の場合だけ settled とする。status 欠落、`unknown`、未知値、非文字列、
  `blocked`、`busy` はいずれも非actionableである。両 slot が healthy、inventory/
  attestation が読めない、pair が一意でない、worktree/branchが動いた場合も同様である。
- **現行は診断専用で、破壊操作を持たない**。Herdr 0.8 / protocol 19 の `pane.close` は
  `pane_id` だけを受け取り、preflightで観測したterminal generationやinventory revisionを
  close mutationと同じ原子的操作で照合できない。server restartでは同じlogical pane idを
  復元しつつterminal generationが変わり得るため、inventoryのfresh read直後に
  `pane close <pane_id>`を呼ぶ二段階処理もexact-generation closeにはならない。
  `generation_conditional_close_unavailable` は全planに付く技術的blockerであり、
  `required_approval_marker` は生成しない。public CLIに `--execute` はなく、programmatic CLI
  呼出しも常にread-onlyである。live replacement portを直接呼んでもreplacement transaction、
  close、launch、sendを一切行わずtyped refusalを返す。raw Herdr closeや二重readで迂回しては
  ならない。
- **先行要件**: Herdr server内の同じclose操作で、少なくとも `pane_id` と期待するterminal
  generation（必要に応じてassigned name / inventory revisionも）を照合し、
  `closed | absent | precondition_failed` を区別して返すconditional-close primitiveが必要である。
  このprimitiveとadapterが実装・検証されるまで、本railはapproval-readyにもdogfood-readyにも
  ならない。
- **conditional close導入後に再審査する契約**: 既存のowner approval digest、approval-time
  health pin、replacement transaction、zero-effect transactionのgeneration再承認CASは将来の
  候補として残る。ただしread-sideのfresh再確認だけで現行locator-only closeを有効化しては
  ならない。Herdrのclose mutation自体が承認されたterminal generationを消費し、その返却値を
  transaction effect receiptへ結合できて初めて、marker生成とclose → same-slot launch →
  action-bound attestationを有効化する。その時点でpartial replay、healthy化、generation ABA、
  close成功後CAS前停止を改めて独立reviewする。
- provider の会話セッション再開は保証しない。保証対象は worktree/branch/file と durable record
  であり、古い pane の画面内容は必要なら read-only capture して Redmine の状態判断へ反映する。

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
