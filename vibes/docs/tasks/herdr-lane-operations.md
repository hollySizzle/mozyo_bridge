# herdr lane 運用手順書 (coordinator / operator 向け)

herdr backend (`terminal_transport.backend: herdr`) での sublane 運用の標準手順。2026-07-07〜08 の herdr 移行波 (#13331 / #13355〜#13360) の live 実測で確立した運用を replay 可能な形で固定する。設計正本は `vibes/docs/specs/herdr-native-identity.md`、role/gate の正本は central preset と `vibes/docs/rules/agent-workflow.md`。本書は手順のみを扱い、規約本文を複製しない。

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
- **legacy lane (pre-#13377)**: 旧 model の lane は独自 herdr workspace (`wt_<hash>` segment, default lane) を持つ。読み (list / status / dispatch-worker) と retire は互換対応済み。新規 create は常に shared model。legacy lane への coordinator dispatch は互換対象外 — 生かしたまま運用せず、順次 retire する。

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

## lane retire (guarded close)

1. lane worktree の dirty を確認・復元: `git -C <worktree> checkout -- .claude/settings.local.json` (agent harness が触る唯一の常連 dirt)。**dirty のままだと retire は `dirty_worktree` で fail-closed する** (正常動作、#13331 j#73339 guard)。
2. `sublane retire --issue <id> --lane-label <label> --worktree <path> --branch <branch> --issue-closed --owner-approved --callbacks-drained --verified --durable-record --target-identity-known --execute --json` → **対象 lane unit の managed slot のみ** close (#13377: project workspace・coordinator pair・他 lane は閉じない。最終 lane の close で sublane host workspace が herdr により自動消滅するのは無害な付随挙動で、retire の前提・完了条件ではない — #13380)。legacy lane (`wt_<hash>` workspace) は互換 plan で旧 slot も close される。
3. worktree / local branch の除去は **統合後** (`git worktree remove` + `git branch -d|-D`)。remote branch は削除しない。

## 統合 (integration disposition)

- 単一 lane が origin/main 直上 (ff 可) → operator の `git push origin <hash>:main` 一発。
- 並列波 → scratch worktree に integration branch を切り、approved commit を順に cherry-pick → conflict 解決 → **full suite (`unittest discover -s tests`、redirect + exit 判定、pipe 禁止)** → branch を origin へ push (anchor 到達性) → operator ff push → **re-anchor 対応表を Feature issue に記録** (旧 hash → 統合 hash)。
- 統合後: local main ff、lane worktree/branch 掃除、各 US に integration + re-anchor journal。

## 監視・callback の実際

- **coordinator 宛の handoff callback は coordinator が busy だと `precondition_not_idle` で不達になりがち。durable record (Redmine journal) の poll が正** — stall 判定は必ず journal 再取得 → 結果なし確認 → pane 実測 (`herdr agent read`) → 再送、の順。
- `blocked` 表示の agent_status は permission prompt / 一時状態の場合がある。pane read で実体確認してから介入する。

## live smoke の原則

- **本番機構で行う**: lane の smoke は必ず linked git worktree で。scratch 単独 repo は registry canonicalization の差を隠す (#13331 j#73348 の教訓)。
- 実 store / 実 workspace を汚さない工夫: 使い捨て stub slot (sleep process + `--no-focus`) や scratch `MOZYO_BRIDGE_HOME` を使い、smoke 後に必ず回収 (#13358 j#73456/j#73472 の実例)。
- 破壊系 (server 停止等) は並列 lane を巻き込むため、同一 fail path の代替実測 (例: `MOZYO_HERDR_BINARY=/usr/bin/false`) で置換可 (#13355 実例)。

## 非 Git workspace の lane (directory scaffold, #13392)

herdr backend は非 Git workspace (registry 採用済みの scratch / sync フォルダ等、git repo でない workspace root) の lane も動かせる。tmux 時代の directory-scaffold-lane 対応を herdr で復元したもの (設計正本: `vibes/docs/logics/sublane-lifecycle-map.md` の Git/非Git 差分、裁定 #13392 j#74067)。

- **runtime cwd = workspace root**: 非 Git lane は worktree を持たない (`git worktree add` は skip)。lane の cwd / `cockpit append --repo` / dispatch の `--target-repo` gate はすべて **workspace root 自身** に collapse する。lane agent は workspace root で走る。
- **明示形 (当面)**: `sublane create` は現状 `--branch` / `--worktree` を必須とするため、非 Git では **`--worktree <workspace root 絶対 path>`** を明示指定する (sibling worktree path を渡すと phantom path になり identity 解決に失敗する。code は skip_no_git 時に workspace root へ collapse するが、明示形が意図を最も明確にする)。`--branch` は使われないため任意の値でよい。contract の optionality 改善は follow-up #13432。
- **placement**: 非 Git lane も #13380 の dedicated sublane host workspace に着地する。lane の identity は `(project workspace_id, lane_label)` unit であり、`lane_id != default` なので coordinator の default-lane pair とは別 slot。host 分離は「distinct repo-root がある時だけ」ではなく `(workspace_id, lane_id)` + lane-aware placement で成立する (非 Git は repo-root を共有しても lane 分化する)。
- **並列 lane**: 同一非 Git workspace root 上で複数 lane を並走できる。lane_metadata は lane ごとに lane-scoped key (`dl_<hash(root, lane_id)>`) で記録され上書きしない (Git lane の `wt_<hash(worktree path)>` とは別体系)。
- **retire**: `sublane retire --worktree <workspace root>` で対象 `(workspace_id, lane_id)` の managed slot のみ close する。coordinator の default-lane pair は close しない。**branch / merge / worktree cleanup は非 Git では対象外** (worktree が無いため `git worktree remove` / branch 削除 / retire-time merge は発生しない。成果の取り込みは別経路)。
- **注意**: 非 Git の並列 lane は conversation / runtime lane の分離であって filesystem isolation ではない (branch / worktree による隔離は存在しない)。Google Drive / sync フォルダでは owner 方針どおり auto git-init はしない。
- 記録衛生は Git lane と同じ: workspace root の host-local 絶対 path を Redmine journal に書かない (workspace label / lane label で参照)。

## 記録の衛生

- journal / commit message に host-local 絶対 path を書かない (worktree は sibling 名または lane label で参照)。`lane_metadata` の `worktree_path` は host-local private (正本: `vibes/docs/rules/public-private-boundary.md`)。
