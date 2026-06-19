# Cockpit Web UI (段階3: 表示面 + アクション + 遷移フィード)

Redmine #11639 (#11679/#11680/#11681/#11682)。コックピット表示面の設計正本。owner 決定は #11639 journal #56164。実装は `src/mozyo_bridge/application/cockpit_ui.py` (UI / actions) + `application/otel_receiver.py` (同一 daemon での配信) + `domain/agent_activity.py` (`TransitionTracker`)。

## 設計境界

```yaml
配信:
  process: OTel receiver と同一 daemon (`mozyo-bridge otel serve`)。別 process は立てない
  bind: 127.0.0.1 のみ (receiver の loopback gate を継承)。外部公開・認証付き公開は本 US 外
  host非依存: 既定 host は iTerm2 Toolbelt webview。任意ブラウザで同一 UI (iTerm2 専用 code を UI に入れない)
endpoints:
  "GET /": 単一 self-contained HTML (外部 asset / CDN なし — 持ち出し経路を作らない)
  "GET /api/units": session inventory snapshot (五層: tmux=行の存在+stale / OTel=activity / Redmine=段階4 join / attention=#12007 派生 projection / observation=#12225 runtime observation freshness envelope)
  "GET /api/transitions": activity 遷移の ring buffer (memory のみ、daemon 再起動で消える = best-effort 整合)
  "POST /api/actions/reveal": repo_root を macOS `open` で開く
  "POST /api/actions/jump": attach client を `tmux switch-client -c <client> -t <session>:<window>` で移動
不変条件:
  - 自動前面化なし (US 制約5)。action は UI の明示 click (POST) でのみ実行。通知は UI 内表示に限定し OS 通知・focus 変更をしない
  - **action intent 検証 (review #56197)**: 「明示 click のみ」は security boundary。action endpoint は (1) `Content-Type: application/json` 以外を 415 で拒否、(2) per-process cockpit token (配信 page に埋込、custom header `X-Mozyo-Cockpit-Token` で送付。custom header は CORS preflight を強制し、server は CORS header を一切返さない) 不一致を 403 で拒否、(3) 非 loopback `Origin` を 403 で拒否する。cross-site simple request は action handler に到達しない (regression test で pin)
  - **HTML rendering 安全 (review #56197)**: workspace / session / path 名は local だが untrusted 入力。UI の DOM 構築は `textContent` / `createElement` のみで、`innerHTML` / `outerHTML` / `insertAdjacentHTML` / `document.write` を使わない (regression test で pin)
  - Redmine へ runtime heartbeat を書かない (US 制約3)
  - 構造化 command のみ (`subprocess` 引数リスト)。shell 文字列連結なし — 空白・日本語 path が inject できない
  - stale 安全: action 前に runtime inventory で pane を再解決。消滅 pane / session / tmux 不在は 409 JSON + refresh 誘導で安全失敗。stale snapshot 中は UI が action を無効化
  - jump v1 は attach client への switch-client。非 control-mode client の最新 activity を優先選択し、`-CC` (control mode) window の focus 移動は v1 scope 外 (UI 注記済み)
  - prompt / secrets / 個人情報を UI・log・Redmine に出さない (inventory が local に持つ情報のみ)
```

## Redmine gate join (段階4: #11686 / #11687)

- **Redmine は読み取りのみ** (US 制約3)。daemon は Redmine へ一切書かず、runtime heartbeat を journal に残さない。実装は `src/mozyo_bridge/redmine_context.py`。
- **credential boundary (review #56232)**: API key の送信先を repo-local file が選べてはならない。**信頼 base URL は daemon env `MOZYO_REDMINE_URL` のみ** が決め、request は常にその URL だけに発行する。workspace の `workspace-defaults.yaml` は project identifier (信頼 host へ送る query param) と宣言 URL を供給し、宣言 URL の scheme+host が信頼 base と**一致しない workspace は fetch ゼロで `unconfigured`** (hostile checkout が request を一切引き出せないことを regression test で pin)。API key は **daemon env `MOZYO_REDMINE_API_KEY` のみ** (repo file 不可、request header のみに乗り payload / log / journal に出さない — test で非漏出を pin)。
- 縮退状態 (unit payload の additive `redmine` field): `available` (open 件数 + 最新更新 open issue の id/status/updated_on — **subject は載せない**: 個人情報・機密要約を含み得て v1 UI が表示しないため、payload 不搭載を test で pin) / `unconfigured` (key・信頼 URL なし / workspace 未 mapping / host 不一致 — error ではない) / `unavailable` (fetch 失敗 or 未 fetch)。いずれも OTel / tmux 層と pane_id identity を変更しない。
- 攻撃面・負荷の抑制: fetch は TTL cache (成功 60s / 失敗 30s) + per-call budget (既定 2 project) で、single-thread daemon が Redmine 遅延で OTLP ingestion を停めない。timeout 2s。cold cache は poll をまたいで温まる。
- **cockpit 層限定の join**: `redmine` field は `/api/units` でのみ付与。`session list` CLI は network に依存しない (listing が Redmine 障害で遅延しない)。
- UI: redmine 列は phase 3 と同じ DOM API / class whitelist (`rm-available` / `rm-unconfigured` / `rm-unavailable`) で描画。

## Attention projection join (#12007)

- **additive な第四 join 層**: `/api/units` の各 pane に派生 `attention` field (#11951 `AttentionRecord`) を付与する。実装は `domain/attention.py` `conservative_attention` を `agents targets --json` (#11952) と共有し、二つの read-only attention projection が drift しない。tmux / OTel / Redmine 層と `pane_id` identity は変更しない (既存 field は不変、純粋に追加)。
- **projection only / source of truth にしない**: derived 値であり、color / DOM / pane title を正本にしない。routing / handoff target 選択には使わない (`cockpit-attention-state.md` / `runtime-observability-boundary.md` 準拠)。
- **durable source 未接続**: owner/review/blocked/stalled の durable source はまだ wire していないため、捏造しない。**live (runtime-readable) snapshot 前提**で、identity が清く解決した pane は `healthy` (`no_attention_source`)、role_source=unknown / confidence=none の pane は `unknown` に倒す。
- **stale fail-safe (#12007 review j#58888)**: snapshot が `stale` (tmux runtime 不読、cache 由来 row) の場合、per-pane liveness を honest に主張できないため、cached identity が strong でも payload 全体を `unknown` / `source_unreadable` に倒す (cached row を `healthy` 表示しない)。`cockpit-attention-state.md` の `unknown` state / verification note (source unreadable は healthy ではなく unknown) と `runtime-observability-boundary.md` (`healthy requires readable sources`) 準拠。top-level `stale` flag があっても consumer が attention field だけで runtime-unreadable pane を健康判定しないため。
- **public-safe**: `source_refs` は tmux pane id のみで、private path / secret を出さない。
- **cockpit 層限定 + 制限**: `redmine` join と同様 `/api/units` でのみ付与し、`session list` CLI payload は attention-free。inventory 層は `@mozyo_lane_id` を解決しないため projected `lane_id` は `default`、per-pane の role-ambiguity flag も持たない (`agents targets` は `ambiguous` を持つ)。`unit_id` は opaque provenance であり routing key ではない。
- UI: built-in HTML は本 join を必須描画しない (indicator surface であり app platform ではない)。private consumer / 外部 frontend が `attention` field を読んで独自 view を組む。

## Runtime observation reload / freshness 表示 (#12225)

cockpit / GUI で runtime observation が **timestamped snapshot** であることを明示し、operator が手動 reload できるようにする US。正本契約は `runtime-observability-boundary.md` の `## Runtime Observation Snapshot Contract` (#12223) と `### Contract handoff to follow-up issues` の `"#12225 cockpit UI"`、reload CLI は #12224。

- **additive な第五 join 層**: `/api/units` payload に top-level `observation` field を付与する。中身は #12224 の runtime observation snapshot envelope (`observed_at` / `source` / `method` / `freshness` / `readability` / `strength` / `stale_reason` / `display_state`)。実装は `cockpit_ui.py` `attach_observation` が、表示中の inventory snapshot **と同じ snapshot** から `commands_runtime_observation.snapshot_from_inventory` (= `observe reload` CLI が使う唯一の inventory→envelope 写像) で導出する。CLI と GUI が freshness 判定で drift しない。tmux / OTel / Redmine / attention 層と `pane_id` identity は変更しない (純粋に追加)。
- **UI: Reload button + freshness 行**: built-in HTML に手動 **Reload** ボタンと `observed_at / freshness / display_state (stale_reason)` を表示する 1 行を持つ。描画は phase 3 と同じ DOM API (`textContent` / `createElement`) のみで、display_state の class は whitelist (`obs-healthy` / `obs-reload_required` / `obs-unknown`) から選ぶ (untrusted payload が class を inject できない)。
- **fail-closed freshness (stale / unreadable / unknown を隠さない)**: stale / unreadable / contradictory snapshot は `display_state` を `reload_required` / `unknown` に倒し、決して `healthy` にしない (`runtime-observability-boundary.md` `### Freshness / fail-safe semantics`)。cache projection は `readability=partial` のため fresh-age でも `reload_required`。visible な「これは stale / cache 由来」ラベルは `freshness` field に残るので、snapshot は表示しつつ current とは読ませない。`completed` / `approved` / `current_status` / `delivered` / `accepted` 等の truth-like generic field は envelope に出さない (test で pin)。
- **diagnostic / display only**: `observation` 層も reload button も workflow truth / owner approval / review / routing / close / completion を更新しない。表示 snapshot は action 許可ではなく、side-effecting action (reveal / jump) は従来どおり実行時に live runtime で pane を再解決する action-time live preflight (`_resolve_record`、`action-time-live-preflight` 境界) を行う。表示 snapshot の鮮度を action 許可に昇格させない。
- **v1 freshness モデル = explicit reload + action-time preflight**: 本 US は polling / WebSocket / sidecar push を追加しない。既存の自動 poll (`setInterval`、#11679) は据え置きで、手動 Reload が explicit-reload affordance を担う。継続観測の formalization は #12227 future scope。
- **cockpit 層限定**: `redmine` / `attention` join と同様 `/api/units` でのみ付与し、`session list` CLI payload は observation-free。

## 遷移フィード (#11681)

- `TransitionTracker` が `/api/units` の runtime snapshot 観測ごとに pane 単位の state 変化 (active / idle / unknown) を記録。bounded ring buffer (100 件) を `/api/transitions` で返す。
- 控えめ通知の v1 = UI 内リスト表示のみ。idle への遷移は「入力待ちの可能性」であり死亡ではない (語彙は activity 層のまま)。stale snapshot では遷移を観測しない (古い pane 集合と新しい activity の組合せで遷移を捏造しない)。
- 段階4 拡張点: unit payload / transition は pane_id を鍵に持つため、Redmine gate 文脈は読み出し時に join できる。

## iTerm2 Toolbelt 登録 (#11682)

配布面は runbook (本節) とする。scaffold artifact にしない理由: iTerm2 の Scripts は user-global であり、workspace 単位で配布する scaffold の性質と合わない。

1. 事前: `mozyo-bridge otel serve` が稼働していること (`mozyo-bridge otel status` で確認)。
2. iTerm2 → Scripts → Manage → New Python Script → "Long-Running Daemon" を選び、以下を保存する (例: `mozyo_cockpit_toolbelt.py`):

   ```python
   import iterm2

   async def main(connection):
       await iterm2.tools.async_register_web_view_tool(
           connection,
           display_name="mozyo cockpit",
           identifier="biz.asile.mozyo.cockpit",
           reload_automatically=False,
           url="http://127.0.0.1:4318/",
       )

   iterm2.run_forever(main)
   ```

3. iTerm2 → Scripts から本 script を起動 (AutoLaunch に置けば常駐)。Toolbelt → Show Toolbelt → "mozyo cockpit" にチェック。
4. 任意ブラウザでも `http://127.0.0.1:4318/` で同一 UI を利用できる。
5. port を変えて serve している場合は script / ブラウザ側 URL を合わせる。

## 検証

- unit tests: `tests/test_cockpit_ui.py` (HTML / units / transitions endpoint、action の構造化引数・stale 安全・client 選択、TransitionTracker 遷移・ring buffer・stale 非観測)
- `python3 -m unittest discover -s tests` / docs validate 一式 / scaffold status
- 受け入れ検証は #11683。
