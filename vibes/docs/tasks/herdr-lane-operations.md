# herdr lane 運用手順書 (coordinator / operator 向け)

herdr backend (`terminal_transport.backend: herdr`) での sublane 運用の標準手順。2026-07-07〜08 の herdr 移行波 (#13331 / #13355〜#13360) の live 実測で確立した運用を replay 可能な形で固定する。設計正本は `vibes/docs/specs/herdr-native-identity.md`、role/gate の正本は central preset と `vibes/docs/rules/agent-workflow.md`。本書は手順のみを扱い、規約本文を複製しない。

## 前提

- 実行 CLI: installed CLI (pipx) が最新 origin/main と版ズレしている間は、**repo-local CLI** を使う (下記形式)。`python -m mozyo_bridge` は `__main__` guard 不在で silent no-op のため **不可**。

```sh
PYTHONPATH=src python3 -c 'import sys; sys.argv=["mozyo-bridge", <args...>]; from mozyo_bridge.application.cli import main; main()'
```

- `MOZYO_HERDR_BINARY`: launch 注入済み agent (lane worker / gateway) は不要。手動起動の coordinator 等で未設定なら `MOZYO_HERDR_BINARY=$(command -v herdr)` を inline 付与。
- lane の identity: linked worktree の lane は mzb1 workspace segment に path-hash token (`wt_<hash>`) を使う (#13331 Opt 1)。registry は main checkout identity を継承するため、`sublane create` 時の record (component `lane_metadata`) が label/issue の display join を担う。

## lane 作成 (標準形)

1. dispatch decision journal を issue に記録 (durable anchor)。
2. `sublane create --issue <id> --lane-label issue_<id>_<slug> --branch issue_<id>_<slug> --worktree <sibling path> --base-ref origin/main --journal <jid> --upstream-coordinator <coordinator herdr pane> --execute --no-dispatch --json`
   - **`--no-dispatch` が標準**: create 内蔵 dispatch は gateway TUI の boot に間に合わず空振りする (実測)。
3. boot 待ち (約 10 秒) 後、**明示送達**:
   `handoff send --to codex --source redmine --issue <id> --journal <jid> --kind implementation_request --target <gateway pane> --target-repo <lane worktree 絶対 path> --role-profile implementation_gateway --profile-field lane=<label> --profile-field upstream_coordinator=<coordinator pane>`
   - cross-workspace 送達のため `--target-repo` は **explicit** (auto は sender repo に解決される)。
4. 着弾確認: delivery record の `sent` + marker observed + turn-start。marker 未観測なら `herdr agent read <pane>` で実測してから再送判断。

## gateway → worker の駆動 (実測 ACK)

- gateway は lane worktree で `sublane dispatch-worker --issue <id> --lane-label <label> --journal <jid> --execute --json` を実行 (#13357)。
- `dispatch_result=worker_dispatched` / `worker_dispatch_confirmed=true` のみが送達成立。失敗は `gateway_notified` のまま fail-closed。結果は issue journal + #13296 ledger に残る。

## worker の relaunch (stall / 再起動時)

- **必ず両 agent 指定**: `herdr session-start --agent codex --agent claude --repo <lane worktree>` — codex slot の adopt が workspace pin になる。**claude 単独指定は adopt pin が効かず新 workspace に迷子になる** (#13360 j#73407 実測)。
- relaunch した worker は「⏵⏵ auto mode on」footer を確認 (permission parity #13360)。旧 pane は先に `herdr pane close`。
- relaunch 後、gateway に worker route の再駆動を指示 (worker の pane id は変わるが解決は assigned name 経由で自動追従)。

## lane retire (guarded close)

1. lane worktree の dirty を確認・復元: `git -C <worktree> checkout -- .claude/settings.local.json` (agent harness が触る唯一の常連 dirt)。**dirty のままだと retire は `dirty_worktree` で fail-closed する** (正常動作、#13331 j#73339 guard)。
2. `sublane retire --issue <id> --lane-label <label> --worktree <path> --branch <branch> --issue-closed --owner-approved --callbacks-drained --verified --durable-record --target-identity-known --execute --json` → managed slot のみ close、最終 pane close で workspace 自動消滅。
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

## 記録の衛生

- journal / commit message に host-local 絶対 path を書かない (worktree は sibling 名または lane label で参照)。`lane_metadata` の `worktree_path` は host-local private (正本: `vibes/docs/rules/public-private-boundary.md`)。
