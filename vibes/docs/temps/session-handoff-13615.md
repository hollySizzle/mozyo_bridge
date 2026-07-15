# Session handoff bundle — #13615 fresh-session durable handoff

このfileは一時的なpointer bundleでありauthorityではない。新規sessionは最初にRedmine #13615 j#76088をsource-of-truth systemから読み、以下のstateをaction-timeに再取得する。

## Boundary identity

- Ticket: #13615 セッション終了時に実行状態・ハーネス・Redmineを整合しfresh sessionへdurableに引き継げる
- Role: session continuity USのfresh-session QA / US-level audit準備
- Parent Feature: #12521 Rules・DocsCatalog
- Redmine Version: #301 ワークフロー単一ステップ実行入口整備枠
- Repo: `mozyo-giken-3800-mozyo-bridge`
- Workspace identity: `e1487dcb1f2d4412b28e825fdeccf9e8`
- Execution root: `.`
- Implementation lane: `issue_13615_session_harness`
- Boundary anchor: #13615 j#76088
- Boundary signals: `context_pressure`, `gate_transition`, `active_issue_change`

## Harness work

- #13617 session retrospective / harness bundle整合: Start j#76081、direct-edit gate j#76082、QA correction request j#76105
- #13616 durable-anchor-only fresh-session QA: j#76103 changes_requested（metadata recordとlive rowのcollapse）、j#76104 portable-path correction
- #13618 snapshot-free correction未昇格: current mainのintegration regression
- Stable docs:
  - `vibes/docs/specs/session-continuity-user-harness.md`
  - `vibes/docs/tasks/new-session-onboarding.md`
  - `vibes/docs/logics/session-boundary.md`
  - `vibes/docs/tasks/herdr-lane-operations.md`
  - `vibes/docs/specs/herdr-native-identity.md`

## Action-time facts to re-fetch

- `origin/main`: boundary時点 `1cc019e4618b5ffd43f00ad1590df69b5c2a8f6d`
- main Test: run `29186580007` success（Python 3.10–3.13）
- integration: boundary時点 `origin/int_13472_session_continuity@84d1a3effd3fe4faddf5f6df40addbc4f15399e8`
- package version: main/stagingとも`0.10.0`
- TestPyPI auto dev run `29186628420`: build success / publish waiting。candidate internal betaではない
- primary checkoutには既存 `.claude/settings.local.json` dirtyがある。変更しない

## Active blocker chain

1. #13614 Codex tool-exec shellへのMOZYO sender identity非伝播（最上流）
2. #13518 callback統一 — j#76017 changes_requested、exact `0f548b7`、j#76071 replacement launch済み/dispatch未配送
3. #13583 default-role authority — j#75976 changes_requested、exact `011d7af`、j#76072 replacement launch済み/dispatch未配送
4. #13595 session-start dry-run副作用 — j#75942 `stop_overlap`、#13518統合後に着手
5. #13524 package dogfood / TestPyPI-only — j#76074のdependency順を復元

Process bugs:

- #13613 `sublane create --execute`がsender attestation前にagentsを起動しpartial laneを残す
- #13614 live coordinatorと実command shellのsender identityが乖離する

## Source journals to re-read

1. #13615 j#76088
2. #13524 j#76074 / j#76063 / j#75919
3. #13606 j#76060 / j#76061 / j#76064
4. #13518 j#76017 / j#76068 / j#76071
5. #13583 j#75976 / j#76030 / j#76069 / j#76072
6. #13595 j#75794 / j#75942
7. #13613 / #13614 issue descriptions

## Fresh-session acceptance

1. workspace registryからcheckoutを解決し、workspace id、checkout existence、Redmine project、Git originを照合する。
2. catalogから`spec-session-continuity-user-harness`をexact idでresolveして本文を読む。
3. Redmineのlatest journalsを再取得し、bundleとの差分はRedmineを採用する。
4. Git branch/worktree、registered lane metadata、live runtime、agent auth、sender command-shell identity、delivery stateをcollapseせず復元する。Herdr `sublane list`の非空rowだけでmetadata presentとせず、`lane_record_missing`はmetadata absent、live gateway/workerは別stateとして判定する。
5. main/integration/active issue heads、latest main CI、package version、TestPyPI waiting stateをread-only再取得する。
6. #13524のrelease chainとowner approvalの対象/除外を復元する。local greenだけでrelease-readyとしない。
7. #13617 j#76105 correctionをorigin-reachable commitで再検証し、QA結果を#13616へ記録する。green後にだけ#13615のUS-level review_requestを作る。bundle受領後のcleanup dispositionも記録する。

## Holds / non-goals

- manual MOZYO env injection、raw Herdr send、cross-lane Claude direct sendを行わない。
- auto dev TestPyPI waiting runをcandidate releaseと扱わず、承認しない。
- version bump、tag、production PyPI、GitHub Releaseを行わない。
- #13518 / #13583のreview finding verdictなしに実装を進めない。
- primary `.claude/settings.local.json`を変更しない。
- bundleをstable policyの正本に昇格させない。
