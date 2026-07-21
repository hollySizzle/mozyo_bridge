# Session handoff bundle — #14098 disposable Ubuntu pilot portability

> このfileは次session用の一時pointer bundleであり、authorityではない。最初にRedmineのlatest journalを再取得し、差分があればRedmineを採用する。受領journal記録後は削除対象にする。

## Boundary anchor

- Active US: #14098 公開パッケージを使い捨てUbuntuでblack-box検証しpilot可搬性を継続判定できる
- Issue role: Linux pilot portabilityを、source checkout非依存のinstalled black-box acceptanceとして成立させる親US
- Parent Feature: #12534 CI・検証方針（プロジェクト成長後も使う機能カタログ）
- Planning bucket: Redmine Version #271 production PyPI release readiness。package version番号の正本ではない
- Start journal: #14098 j#82853
- Current Task: #14099 session continuity user harnessとtransition bundleの更新
- Current Task journals: #14099 j#82854（Start）/ j#82855（限定codex_direct_edit）
- Repo: `mozyo_bridge` workspace registry / Git originで一意に解決する
- Execution root: `.`
- Target lane: none。branch/worktreeは存在するがmanaged lane metadata / live runtimeは作っていない
- Boundary signals: `active_issue_change`, `gate_transition`, `context_pressure`

## Session review result

- `AGENTS.md` / `CLAUDE.md`はthin routerを維持し、central preset / catalog / project-local docsへpointerしている。router本文の修正は不要。
- central presetはrepo-local pathから解決可能。role/provider bindingの運用値は`.mozyo-bridge/config.yaml`で`coordinator: codex`、terminal backendは`herdr`。
- user harnessのstable layer（router / governance / cataloged docs / Redmine / temporary bundle / boundary prompt）は成立している。
- 見つかったgapは、installed black-box環境を引き継ぐ際にartifact source、container image identity、runtime user、fresh home、source mount、実施surfaceを独立して復元するcontract不足。#14099の限定diffで補う。
- `vibes/docs/`配置は`logics`=業務ロジック、`rules`=規約、`specs`=仕様、`tasks`=手順、`temps`=一時pointerを維持する。
- primary checkoutの`.claude/settings.local.json`はowner-owned dirty fileとして変更しない。

## Completed release baseline

- Package: `mozyo-bridge 0.12.1`
- Production distribution: GitHub Release `v0.12.1` / PyPI `0.12.1`公開済み
- Exact release/main SHA: `3ec134ea56e469c99c19a5aa480273477543f99a`
- GitHub Test: run `29693901527` success
- Production publish: run `29694040244` success（Python 3.10–3.13、build、fresh install、Trusted Publishing）
- #14095 0.12.1のcallback・post-close recovery・nested rollback補正公開: closed
- #14096 installed 0.12.1のfresh callback・recovery・rollback総合検証: closed
- #14094 resumed correction laneのcurrent-generation review callback配送補正: closed
- #13948 nested startup rollback pointerとsame-binding replay補正: closed
- #13806 post-close stale-worker recoveryとauthority rejoin補正: closed
- Managed lane state: active 0。`sublane status`の97 rowは全件`detached`の履歴metadataで、live laneではない

## Active / queued issue chain

1. #14099 session continuity user harnessをcontainer pilot handoffへ更新する — current implementation/review input
2. #14102 公開後open Bugをcontainer blocker / broader pilot blocker / unrelated backlogへ分類する — triage、ID/statusだけで裁定しない
3. #14100 disposable Ubuntu black-box smokeを実装しCI/release gateへ接続する — implementation Task
4. #14101 exact installed packageのUbuntu black-box acceptanceを実行する — Test。failure再現後にだけBugを作る
5. #14098 親USのUS-level audit / owner close approval — child完了後

Related residuals:

- #14097 post-close recovery / nested rollbackを安全に再現するinstalled fault-path harness — 未着手。0.12.1の既知runtime defectではなくtestability改善
- #13811 project-gateway lifecycle adapterを共通lifecycle/generation APIへ接続する — 着手中だがmanaged processはhibernate済み。container docs smokeとは別scope

## Open Bug pointers for #14102 triage

以下はcandidate pointerであり、本bundleはblocker判定を行わない。latest issue/journalと現行main包含を再取得して分類する。

- #13683 callback supervisorが常駐せずdurable stateを継続供給できない
- #14063 `sublane list --json`がstale locatorをlive slotとして投影する
- #13952 review heading語彙差で`workflow glance`が最新review結論を認識しない
- #13910 callback recoveryのreceiver側exactly-once idempotency不足
- #13951 callback-sweep lease DB / sidecar不一致でpublic recoveryが停止する
- #13920 active adoptとrecover-pairのrole語彙差でhibernate後にpinsが欠落する
- #13897 hibernated legacy migrationがforeign-only inventoryをlive-zeroと誤認する
- #13884 explicit herdr targetがlane gatewayでなくcoordinatorへ誤解決する
- #14103 full unittest suiteがdogfood hostの実OTel receiverとLaunchAgentを拾い非hermeticに失敗する（変更前mainでも2件再現済み。container runtime defectではなくtest harness隔離欠陥）

## Authority and non-goals

- Owner authorized: Redmine hierarchy整備、session振り返り、user harness/temporary bundle更新、次sessionでのdisposable Ubuntu test推進。
- Owner authorization does not mean: Dev Container作成、new production publish、credential永続化、raw tmux/Herdr/SQLite操作、real TUI E2Eをcontainer smoke成功へ読み替えること。
- #14098のUbuntu container runtime defectは未観測。形式だけのBugは作らない。#14103は変更前mainでも再現したdogfood host test hermeticity defectとして観測根拠付きで起票済み。
- workflow / CI source変更は#14100のImplementation Done → task-level Review Gateを通す。

## Fresh-session receive order

1. #14098と#14099のlatest journalをRedmineから読む。本bundleのjournal番号より新しければ最新を採用する。
2. `AGENTS.md`、central preset、`.mozyo-bridge/config.yaml`、catalog resolverから本bundleに列挙したstable docsを読む。
3. #14099のorigin-reachable commitとReview Requestを確認し、stable-vs-temporary境界、portable pointer、secret/absolute-path absence、catalog reachabilityをreviewする。approved後にcoordinatorがintegration dispositionを記録する。
4. #14102でopen Bugをpilot blocker分類する。container smokeの開始を無関係Bugの一括完了待ちにしない。
5. #14100を開始し、pinned blocking image / floating canary / non-root / fresh home / no source mount / exact artifact provenanceを実装する。
6. #14101を実行し、rules / scaffold / docs / doctorのblack-box結果と未実施live surfaceを分けて記録する。
7. failureが再現した場合だけBugを起票し、#14098へrelationを戻す。

## Preservation and cleanup

- #14099 branch: `issue_14099_session_harness_container_pilot`
- #14099 worktree: present。portable promptへhost absolute pathを書かない
- managed lane metadata: absent
- live routable runtime: absent
- routable verdict: `branch-only`
- running process / pending delivery: none
- pending review: #14099 follow-up review required
- primary dirty preservation: `.claude/settings.local.json`を変更しない
- bundle cleanup: fresh sessionが受領結果をRedmineへ記録した後、次のreviewable transition commitで削除する
