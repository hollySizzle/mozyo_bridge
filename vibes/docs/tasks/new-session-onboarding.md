# 新規セッション onboarding 導線 (fresh agent 向け)

新規 session の agent (coordinator / worker いずれも) が chat context を持たずに立ち上がっても、context を失わず現在地に到達するための **安定した導線** (Redmine #13406)。本書は **state snapshot を焼かない** — 「今どこか / 次に何をするか」の live な正本は Redmine journal であり、本書はその読む順序と在り処だけを固定する pointer である。

## 読む順序 (最初の 5 分)

1. **`CLAUDE.md` (Codex は `AGENTS.md`)** — tool-specific router。session 開始手順と常時適用規則ダイジェストを読む。
2. **central preset** `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` — gate / 役割 / 編集権限 / completion の governance 契約の正本。role/provider binding の運用値は `.mozyo-bridge/config.yaml` の `provider_binding` を確認する (自役割を自認する前に)。
3. **現在の作業の Redmine issue / journal** — durable record が作業状態の正本。着手前に active issue を読む。どの issue が active かは §「現在地の見つけ方」参照。
4. **質問ドメインの cataloged docs** — 設計・仕様・現状挙動を回答/断定する前に、`mozyo-bridge docs resolve <path>` で正本を解決して読む (always 規則 `answer-time-doc-resolution`)。

## Redmine 構造 (機能カタログ)

Epic / Feature は「プロジェクトが成長しても使える機能カタログ」として設計されている (release grouping ではない)。現在地は **Feature 単位** で探す。

- `110_実行基盤・Routing` (#12501) — workspace/session identity, agent discovery, handoff routing, delegated coordinator, runtime 観測, state store。**`170_herdr backend swap` (#13242)** は terminal/transport backend swap の機能カタログ (この Feature 配下に herdr 関連 US が並ぶ)。
- `120_運用Cockpit・表示` (#12502) — cockpit read model / web UI / preflight / grouping / attention。
- `130_統治・Scaffold配布` (#12503) — Redmine 統治 workflow, scaffold preset, canonical renderer, **`140_Rules・DocsCatalog` (#12521)** ← 本 onboarding doc / 規約系の親。
- `140_Adapter・Provider基盤` (#12504) — ticket adapter, redmine/asana adapter, presentation provider, plugin/marketplace, provider registry。
- `150_品質・アーキテクチャ統治` (#12505) — テスト構造, **`120_シナリオ・受入テスト基盤` (#12531)** ← 古典 scenario テスト基盤の親, モジュール健全性, source 配置, CI。
- `160_外部AgentUI連携` (#12506) — VS Code Agent Pane 系 (遠期 backlog)。

新規 US は該当 Feature 配下に置く。Version は release grouping ではなく実行 bucket / conflict bucket (`feedback_coordinator_fill_ritual_and_redmine_scope` / `vibes/docs/logics/release-flow.md`)。

## 現在地の見つけ方 (live state)

1. `mozyo-bridge docs resolve` は catalog 経由で「触る path に紐づく正本」を返す。
2. active US は Redmine で `list_user_stories(status=open)` / `list_recently_updated_issues` で引く。どの Feature に open backlog が寄っているかは query 結果 (open US の parent Feature 分布) で判断する — 本 doc に「主戦場」を固定記載しない (live state は Redmine が正本)。特定 Feature 配下を見るときは §「Redmine 構造」の Feature 一覧から parent を選んで `list_user_stories` を絞る。
3. `mozyo-bridge status` / `herdr agent list` / `herdr workspace list` は runtime の観測 (durable record ではない — 判断の正本は Redmine)。

## session boundary-prompt の必須 anchor

fresh session が chat 履歴なしで受け取る最初の入力は、区切りで提示される **next-session boundary prompt** である。その必須 field / portable pointer / 推奨 field の contract 正本は `logic-session-boundary` の `## 2. Next-session boundary prompt` と実装 `mozyo-bridge session boundary-prompt` (formatter `session_boundary.build_boundary_prompt`) であり、本 doc は table を複製せず要点のみ pointer する (入口の薄さ)。prompt は現在地の snapshot ではなく durable record への pointer 束で、先頭は「anchor journal を source-of-truth system から読んでから着手せよ。以下は pointer であって新しい authority ではない」で始まる。

canonical contract の要点 (詳細・完全な field 表は上記正本を読む):

- **必須**: `issue` (ticket id) + `issue_subject` (短い subject) + `issue_role` (この issue が何をするものか: 本番 / 前提確認 / cleanup / operator decision 等) + `journal` (latest anchor journal id)。ID 単独で渡さない — 少なくとも `#<issue> <short subject> + issue_role + parent_issue` を隣接行に置き、誤実行が高コストな issue では `non_goals` / `not_this_issue` を明示する。
- **portable pointer**: `repo` (canonical session name + 利用可能なら workspace id。絶対 path にしない) + `execution_root` (#12098。portable repo-relative pointer で、pane cwd / workspace root とは別物。絶対 path は `--json` 構造化出力にのみ載せ、pasteable text には載せない)。pane 位置ではなく durable record / workspace registry から解決する。canonical session 名が重複した場合は workspace id、checkoutの存在、Redmine project、Git originで照合し、一意に決まらなければ停止する。
- **推奨**: `parent_issue` / `commit` / `target_lane` / `gate_state` / `verification_state` / `residual_risks` / `pending_action + next_actor` (next_actor: owner|claude|codex) / `signals`。

`boundary-prompt --help` が `--issue-subject` / `--issue-role` をまだ公開していないruntimeでは、helperの出力をそのまま渡さず、両fieldを手動で補う。これは旧runtimeとの互換手順であり、field省略を許可する例外ではない。runtime promotion gapはRedmineに残す。

handoff notification marker (`kind` / `receiver` / role profile pointer 等) は配送 edge の別 contract であり、boundary-prompt の必須 field ではない (混ぜない)。role profile pointer の展開仕様は `logic-role-profile-handoff-expansion` を読む。

boundary-prompt から現在地を再構成する順序は §「読む順序」に従う: prompt の anchor → 該当 Redmine journal → parent US / active issue → 質問ドメインの cataloged docs。prompt に固定された state はどれも durable record で照合する。

## Session transition package の受領

prompt が `vibes/docs/temps/session-handoff-<issue>.md` を指す場合、その file は複数 issue の current chain を束ねた一時 pointer であり、authority ではない。[[spec-session-continuity-user-harness]] と [[logic-session-boundary]] に従い、次の順で受領する。

1. prompt の boundary issue / journal を Redmine から読む。
2. temporary bundle に列挙された active / queued issue の latest journal を読む。bundle と相違すれば Redmine を採用する。
3. approval の対象と除外、preservation signal、pending action / next actor を復元する。
4. lane に対して next-action (dispatch / handoff / retire) を取る前に、bundle の `target lane` label を live routable lane と同一視せず、Git branch/worktree・registered lane metadata・live routable runtime を区別する。`sublane list --lane <label> --json` は live row だけでも非空になり得るため、配列の有無だけで metadata present と判定しない。返却 row の `stale_hints` が `lane_record_missing` を含めば metadata absent、`lane_slots_missing` を含めば metadata present + runtime absent と読む。live runtime は gateway / worker slotを別に測る。判別不能は unknown で fail-closed にする。backend=herdr では `agents targets` (tmux-era primitive/debug 面) の empty candidate を lane 不在の根拠にせず、`mozyo-bridge doctor runtime` で installed/source fingerprint skew を read-only 検出し、mismatch を fail-closed 記録してから repo-local source CLI を使う (正本 [[spec-session-continuity-user-harness]] `### Routable lane state の区別` / `### Runtime fingerprint gate (backend=herdr)`、[[task-herdr-lane-operations]])。
5. agent auth stateと、handoffを実行するcommand shellのsender identity stateを別々に測る。認証切れ/agent死はre-auth/relaunch、sender identity missing/conflictはstandard dispatch blockedとして扱い、routing failureと混同しない。手動env注入/raw sendは行わない。
6. fresh-session QA の結果を boundary Test / parent US に記録する。
7. 受領記録後、bundle は stale cleanup 対象とする。恒久 doc から参照しない。

### Release readiness を復元する場合

release / TestPyPI / installed CLI E2Eを含むtransitionでは、次を独立したgateとして順に復元する。

1. active issueのlatest review journal、未解決finding、対象commitのorigin到達性。
2. `origin/main`、integration lane、active issue laneのheadとlatest main CI conclusion。
3. package version decision / bump、build、artifact inspectionの完了状態。
4. TestPyPI publish承認、exact-version fresh install、installed CLI E2Eの完了状態。
5. production PyPI / GitHub Releaseが明示承認の対象か、除外か。

focused / full local testsがgreenでも、main CI失敗、未完review、未決version、未実施artifact / installed E2Eが残る場合は「release可能」と報告しない。原因未分類のCI失敗はそれ自体をrelease blockerとして扱う。

LLM turn 内で worker completion を待つための poll は行わない。callback が未着なら current durable state を記録して turn を終了し、次の callback / fresh turn で再開する。

## 主要 doc の在り処 (vibes/docs レイアウト)

| dir | 用途 | 主な doc |
| --- | --- | --- |
| `logics/` | 業務ロジック | `coordinator-sublane-development-flow.md` / `delegated-coordinator-cockpit-display.md` / `sublane-lifecycle-map.md` / `herdr-scenario-test-foundation.md` |
| `rules/` | プロジェクト規約 | `agent-workflow.md` / `workflow-docs-boundary.md` / `public-private-boundary.md` / `codex-autonomous-guardrail-lane.md` |
| `specs/` | 仕様 | `session-continuity-user-harness.md` / `herdr-native-identity.md` / `route-identity-ledger.md` / `delegation-policy-project-config.md` |
| `tasks/` | 手順書 | `herdr-lane-operations.md` (lane 運用) / `external-project-herdr-adoption.md` (他 project 導入) / 本書 |
| `temps/` | 一時ドキュメント | 恒久参照にしない。stale は掃除対象。 |

## herdr lane 運用の要点 (pointer)

以下は spec/task doc に正本があり、ここでは pointer のみ (時点依存の snapshot ではなく、herdr lane を扱うとき読む正本への安定 pointer):

- lane は **project workspace とは別の専用 sublane host workspace** に着地する (coordinator window + sublane window の分離、正本 `logic-delegated-coordinator-cockpit-display` / #13380)。
- lane identity = `mzb1_<project-ws>_<role>_<lane>`、新 lane 標準形 = 単発 `sublane create --execute`、explicit lane 送達 = `--target-lane` (正本 `spec-herdr-native-identity` / `task-herdr-lane-operations`)。
- lane 内 nested handoff send は outer repo root を `--repo` で pin する (外部 project で backend 乖離を防ぐ、#13397)。
- 未採用 directory の bare `mozyo` は fail-closed (採用 = scaffold/config marker、Git 非条件、home 常時拒否、正本 `task-external-project-herdr-adoption`)。
- CLI 版ズレ時 (installed CLI が最新 main 未反映) の lane 運用回避は `task-herdr-lane-operations` の前提節を読む。

## 禁止・注意

- agent-private memory は補助。**harness の正本は本 repo の vibes/docs + Redmine + central preset**。memory の「解消済み」記録は scope を疑い、正本で照合する (`answer-time-doc-resolution`)。
- 記録に host-local 絶対 path / secret を書かない (`rule-public-private-boundary`)。
