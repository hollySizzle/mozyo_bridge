# Session handoff bundle — Redmine #13535

> 一時 pointer bundle。Redmine issue / journal と cataloged docs が正本であり、本書は新しい authority ではない。次 session の受領 journal 後に削除する。
>
> Correction (#13543 / #13535 j#75183): 旧版は `Target lane` を live routable herdr lane と同一視していた。実際は branch-only / lane-unregistered。lane state は下記 Boundary で 3 state に区別する。

## Boundary

- Ticket: #13535「新規session移行時にrelease readinessとactive review chainをdurableに復元できる」
- Role: session transition UserStory。fresh-session QAとUS-level reviewを管理する。#13518の実装修正・close、統合、publishを実行するissueではない。
- Parent Feature: #12521「140_Rules・DocsCatalog」（長期利用する機能カタログ）
- Version: #301「ワークフロー単一ステップ実行入口整備枠」
- Repo: `mozyo-giken-3800-mozyo-bridge`
- Workspace identity: `e1487dcb1f2d4412b28e825fdeccf9e8`
- Lane state (`spec-session-continuity-user-harness` `### Routable lane state の区別`、#13543):
  - `issue_13535_session_transition` は **branch-only / lane-unregistered**。Git branch/worktree と commit `49a770f...` は存在するが、source runtime の `sublane list --lane issue_13535_session_transition --json` は `sublanes: []` で registered lane metadata / live routable herdr slot を持たない。
  - routable な herdr lane として dispatch / handoff の対象にしない。branch base は `origin/int_13472_session_continuity`。
  - runtime fingerprint 注意: installed `mozyo-bridge 0.10.0` は source の #13446 herdr preflight を欠く。next-action 前に `mozyo-bridge doctor runtime` で installed/source skew を確認し、mismatch なら repo-local source CLI を使う (`### Runtime fingerprint gate (backend=herdr)`)。
- Execution root: `.`
- Base integration head: `0b051eb162b11a055beda53b85438904e531b42a`
- Bundle commit: #13535 latest journalのexact hashを正本として読む。

## Active / queued chain

1. **Active review:** #13518「coordinatorをblocking waitとraw Herdr操作から解放しmozyo callback駆動へ統一する」
   - role: normal development US。Claude implementation lane → Codex US-level re-review。
   - latest known anchor: #13518 j#75152。
   - review target: `e776d5f2a4551e665a0f5301a43a87b9e6e4510b` on `origin/issue_13518_zero_wait_callback`。
   - previous findings: production callback runtime / sender wiring不足とconcurrent duplicate-send race。workerはruntime入口とclaim lease / fencingで修正済みと報告。独立re-reviewは未完。
2. **Queued after #13518:** #13524「Version #301 integration headを次package versionへbumpしlocal environmentでdogfoodする」
   - role: version decision / bump / build / artifact inspection / local install / TestPyPI internal beta。
   - children #13525–#13528は未着手。
   - approval: TestPyPIのみ承認。production PyPI / GitHub Releaseは除外。
3. **Queued after #13518 and #13524:** #13490「人間・coordinator・gateway・workerの単一入口E2E」
   - role: TestPyPIからfresh installしたCLIでzero-wait / mozyo-onlyを受入再生するfinal E2E。
   - child #13492は未着手。
4. **Boundary QA:** #13537「新規session promptから#13518再reviewとrelease残タスクを復元する」
   - role: acceptance Test。implementation / close / publishは行わない。

## Release-readiness snapshot

- known heads: `origin/main=32916f84d6af3c78cf9f03cb596e6c21d279b2eb`、`origin/int_13472_session_continuity=0b051eb162b11a055beda53b85438904e531b42a`、`origin/issue_13518_zero_wait_callback=e776d5f2a4551e665a0f5301a43a87b9e6e4510b`。
- latest known main CI: GitHub Actions Test run `29129232080` はPython 3.10–3.13のunit testでfailure。原因は未分類。
- package source version: `0.10.0`。次versionはrelease gateで未決定。latest tagは`v0.9.2`。
- release verdict: production release-readyではない。TestPyPI-readyでもない。#13518 review / integration、main CI原因解消、#13524のversion / build / artifact gate、TestPyPI fresh install、#13490 installed CLI E2Eが残る。

## Owner authorization boundary

- authorized: safeなread-only監査、#13518 re-review、review green後のgoverned integration disposition、Version #301 package bump、local artifact検証、TestPyPI Trusted Publishing、TestPyPI exact-version fresh install。
- excluded: production PyPI、GitHub Release、credential記録、unreviewedな`origin/main`直push、open / dirty / unknown laneの破壊的retire。
- efficiency policy: 最大10 lane。ただしdependency、edit overlap、review independence、release / destructive gateはhard constraint。直列release chainをlane数のために並列化しない。

## Preservation / residual risks

- #13518はre-review待ち。j#75147 / j#75150 / j#75152を照合し、active laneをpoll / retire / editしない。
- primary checkoutの既存 `.claude/settings.local.json` 差分を変更しない。
- #13524は#13518 review / integration後、#13490は#13518 + #13524後。
- branch head、CI、journalは陳腐化し得る。必ずsource-of-truthのlatestを採用する。

## Next session acceptance

1. #13537 latest anchorを読み、#13535と本bundleの役割を復元する。
2. #13518 j#75147 / j#75150 / j#75152とcommit `e776d5f`を独立再reviewする。
3. #13524、#13490のlatest journalを読み、dependencyと承認境界を再確認する。
4. main / integration / issue branch head、latest main CI、package versionをread-onlyで再取得する。
5. lane に next-action を取る前に routable lane state (Git branch/worktree・registered lane metadata・live routable runtime) と runtime fingerprint を確認する。`sublane list --lane <label> --json` と `mozyo-bridge doctor runtime` を read-only 実行し、`agents targets` の tmux 候補空振りを lane 不在の根拠にしない (#13543)。
6. #13537 QA Verificationと#13535 US-level reviewを記録する。integration / close / publishは別gateまで行わない。

Boundary signals: `context_pressure`, `gate_transition`, `active_issue_change`。
