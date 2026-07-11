# Session handoff bundle — Redmine #13529

> 一時 pointer bundle。Redmine issue / journal と cataloged docs が正本であり、本書は新しい authority ではない。次 session の受領 journal 後に削除する。

## Boundary

- Ticket: #13529「新規session移行packageでactive workflowと承認境界をdurableに復元できる」
- Role: session transition UserStory。user harness / docs / tickets / next-session prompt を整備する。#13518 の実装やrelease実行そのものではない。
- Parent Feature: #12521「Rules・DocsCatalog」（長期利用する機能カタログ）
- Version: #301「ワークフロー単一ステップ実行入口整備枠」
- Repo: `mozyo-giken-3800-mozyo-bridge`
- Target lane: `issue_13529_session_transition`（integration元は`int_13472_session_continuity`）
- Execution root: `.`
- Base integration head: `79727b8e9d82454ccf063622f8b0e0f77a27b1b0`
- Bundle commit: #13529 latest journalのexact hashを正本として読む。

## この session で完了した主な価値

- #13489「herdr環境でworkflow stepを単一agent commandとして再収束」: bounded dispatch、durable gate、runtime authority、idempotency fenceを実装・review・QA・統合・close。通知summaryよりsource journalを優先する運用を実証。
- #13497「bare mozyoを人間向け会話UIへ収束」: 人間の入口を一語へ寄せる次段の設計単位を整理しclose。
- #13518「coordinatorをblocking waitとraw Herdr操作から解放」: zero-wait / mozyo-only doctrineとcallback coreまでorigin issue branchへ到達。live wiringとQAを同laneで継続中。
- #13524「Version #301 integration headを次package versionへbumpしdogfood」: release unitとTask/Testを整備。TestPyPIまでのowner承認をdurable化。
- #13529「新規session移行package」: 今回のharness監査と一時bundle欠落をUS/Task/Test/Bugへ分解。

## Active / queued chain

1. **Active:** #13518「coordinatorをblocking waitとraw Herdr操作から解放しmozyo callback駆動へ統一する」
   - role: normal development US。Claude implementation lane → Codex US-level audit。
   - latest known anchor: #13518 j#75109。
   - origin commits: `3f98306`（doctrine / role profile）、`c87bf3752c62aff99f44b53d30b175f461101ed3`（callback core）。
   - current known action: live wiring + #13521 QAをsame laneで継続。新sessionは必ずlatest journalを再読する。
2. **Queued after #13518:** #13524「Version #301 integration headを次package versionへbumpしlocal environmentでdogfoodする」
   - role: version bump / build / local install / TestPyPI internal beta。
   - approval anchors: #13524 j#75091（TestPyPI承認）、j#75102（read-only preflight）。
   - known candidate: `0.11.0`系。#13518統合後に確定する。
3. **Queued after #13518 and #13524:** #13490「人間・coordinator・gateway・workerの単一入口E2E」
   - role: installed CLIでzero-wait / mozyo-onlyを受入再生するfinal E2E。
4. **Boundary work:** #13529 本US
   - children: #13531 docs実装、#13530 bundle欠落Bug、#13532 fresh-session QA。

## Owner authorization boundary

- authorized: 全close承認、greenなclosed laneの安全なdrain / retirement、Version #301 package bump、local wheel install、TestPyPI Trusted Publishing、TestPyPIからのexact-version fresh install。
- excluded: production PyPI、GitHub Release、credentialの記録、`origin/main`への未review直push、open/dirty/unknown laneの破壊的retire。
- efficiency policy: 最大10 laneを上限に効率を優先する。ただしdependency、edit overlap、review independence、destructive / release gateはhard constraint。現chainは直列dependencyのため、lane数を埋めること自体を目的にしない。

## Preservation / residual risks

- #13518 implementation laneは進行中。poll / blocking waitを行わず、latest Redmine journalでcallbackを回収する。
- #13518未統合のため、#13524を先行実行しない。
- primary checkoutの既存 `.claude/settings.local.json` 差分を変更しない。
- `origin/main` は本bundle作成時点で統合laneより古い。integration dispositionなしに更新しない。
- 本bundleのjournal番号やknown headは陳腐化し得る。必ずsource-of-truthのlatestを採用する。

## Next session acceptance

1. #13529 latest journalを読む。
2. #13518、#13524、#13490の順にlatest journalとrelationsを読む。
3. #13518がreview-readyならUS-level auditを行う。まだimplementingならpollせず、#13529 fresh-session QAとread-only release preflightだけを進める。
4. #13532にcontext restore結果を記録し、#13529へQA summaryを引き継ぐ。
5. 本bundleを削除可能と判断するのは受領journal記録後。

Boundary signals: `context_pressure`, `gate_transition`, `active_issue_change`。
