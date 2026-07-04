# herdr 採用 PoC 実験ログ (#13175)

terminal/transport/state 検知層の移行先候補 **herdr** に対する kill criteria 4 点の実測記録。各実験を「目的 / 実行 / 結果 / 学び」の固定フォーマットで残す。**失敗・誤読・観測方法の欠陥も削除せず記録する** — 判定の再現と、将来の再評価 (herdr バージョン更新時など) のため。

- 判定の durable record: Redmine #13175 j#72230 (session 1) / j#72240 (session 2 + verdict) / j#72241 (訂正)。戦略決定は #13126 j#72060、実行拘束は #13175 j#72065。
- 対象: herdr v0.7.1 (Mach-O arm64)、sha256 `16f4653f0491ea1e7d2b46b5b02542f18e1b82e88daaf9e2900572e5bb634df8`
- sandbox: `/private/tmp/mozyo-herdr-poc-13175-20260704/` — 隔離 `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME`。session 1 は HOME も隔離、session 2 (live) は agent auth のため HOME=実 HOME で herdr の設定/状態のみ隔離。
- pane 内 agent (session 2): Claude Code v2.1.201 (banner は Opus 4.8 表示 → 後半 status line は Sonnet 5。operator が途中でモデル変更/再起動した可能性があり、両モデルで検知が機能したことを意味する)。

## kill criteria (事前固定、確証バイアス対策)

| # | 条件 | 最終判定 |
|---|---|---|
| (a) | Claude/Codex の Blocked/Working/Done 検知精度 | PASS (Claude/Codex とも live 全状態) |
| (b) | 恒久 identity 付与手段 | PASS |
| (c) | socket wait で submit→turn-start 確認が #13166 同等以上 | PASS (4-case harness 実測、E12〜E14) |
| (d) | remote manifest/skill update の pin・無効化可否 | PASS (kill criterion) |

総合 verdict: **conditional-go** (詳細条件は j#72240、review findings は j#72247→補完 j#72258、owner 決裁待ち)。

---

## Session 1 — 隔離 sandbox (headless)

### E1. sandbox 構築と binary pin

- 目的: `curl | sh` なし・global install なしで検証環境を作る (j#72065 拘束)。
- 実行: pinned GitHub release asset を配置、`shasum -a 256` で照合。隔離 env で `herdr --version` / `herdr --help`。
- 結果: sha256 一致、`herdr 0.7.1`。help の Config/Logs パスが sandbox 内を指し、XDG 隔離が機能。
- 学び: herdr は `HERDR_CONFIG_PATH` でも config を差し替え可能。隔離は XDG 変数だけで完結する。

### E2. supply-chain 静的調査

- 目的: (d) の remote surface を全列挙する。
- 実行: `herdr --default-config`、`herdr integration` / `plugin` / `channel` サブコマンド調査、`strings` による binary 検査、`integration install claude|codex` の生成物検査。
- 結果: remote surface は 3 系統のみ —
  1. `[update] version_check` / `manifest_check` (herdr.dev への version / agent-detection manifest チェック。`HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL` で catalog URL を env override 可能 = 自前 pinned mirror を立てられる。fetch は `curl --retry 2 --connect-timeout 5 --max-time 15 --max-filesize` の bounded GET、TOML データのみで code 実行なし)
  2. `herdr update` / `channel set` (明示コマンドのみの self-update)
  3. `herdr plugin install <owner>/<repo> [--ref REF]` (github 由来 remote-code。ただし既定 install ゼロの opt-in、`--ref` commit pin 可、`plugin link <path>` で local-only 可)
- claude/codex integration hook の生成物は完全 local: 自己完結 sh + inline python3、通信は Unix socket (`$HERDR_SOCKET_PATH`) への `pane.report_agent_session` のみ。URL/curl/http の埋め込みなし。`HERDR_ENV=1` + socket + pane_id が揃う herdr 管理 pane 内でのみ発火。
- 学び (失敗込み): `integration install` は対象 agent の config dir (`~/.claude` 等) が存在しないと拒否する。最初の install は素の隔離 HOME で失敗した — dir を seed してから成功。

### E3. egress A/B 実測 — 観測方法の欠陥と訂正

- 目的: `manifest_check=false` / `version_check=false` が実際に egress を止めることの実証。
- 実行: (i) 無効化 config で headless `herdr server` 起動 → `lsof -i` 監視 + log 検査。(ii) 有効 config で同様。(iii) cache 消去 → 無効化 config で clean 起動。
- 結果:
  - 無効化: log に update/manifest 系ゼロ、network socket ゼロ、remote cache 生成ゼロ、`agent-manifests --json` は全 agent `source_kind: bundled`。**完全オフライン動作**。
  - 有効: `update.check.start` 発火、herdr.dev から 18 agent manifest を実ダウンロード・cache (`state/herdr/agent-detection/remote/*.toml`)。
- **学び (観測の失敗)**: 有効 run の最初の `lsof -p <server pid> -i` は空で「有効でも egress なし」と誤読しかけた。実際は fetch が **curl 子プロセス**で走るため server pid には現れない。`agent-manifests --json` の `source_kind: remote` と cache ファイルの出現で訂正。**プロセス単位の network 観測は子プロセス fetch を見逃す** — 判定は成果物 (cache/status) 側で行うこと。

### E4. bundled 検知 rule の抽出

- 目的: (a) の検知機構を理解する。
- 実行: `strings` で binary 内蔵 manifest を抽出、有効 run の cache から `claude.toml` を取得。
- 結果: claude/codex の state 検知は **hook ではなく pane-content heuristics** (公式 hook が wire するのは SessionStart = identity 報告のみ)。rule は region-scoped:
  - working: OSC terminal-title の braille spinner regex `^[\x{2800}-\x{28FF}] ` (region `osc_title`, priority 1100)
  - idle: prompt box の `❯` (region `prompt_box_body`) + OSC title `^✳ ` (`osc_title_idle`)
  - blocked: 承認 form 文言の複合 match (`live_blocked_form` / `generic_permission_prompt` 等、冗長 rule)
- 学び: 検知の頑健性は「OSC title >> 本文テキスト」の優先順位で設計されている。manifest は versioned (`2026.06.10.3`) で bundled→remote→local override の 3 層解決。

### E5. offline 検知テスト (`agent explain --file`) — 部分成功

- 目的: live agent なしで rule の分類精度を検証する。
- 実行: 合成 snapshot (idle / blocked / working 文言) を file にし `herdr agent explain --file <path> --agent claude --json`。
- 結果: **blocked = 正** (`live_blocked_form` match)、**idle = 正** (安全側 fallback)。**working = 判定不能**: OSC escape 列を file に埋め込んでも `explain --file` は OSC を parse しない (osc_title region_bytes=0) ため、working rule が構造的に発火しない。
  - codex の offline 合成テストも fallback (`default_known_agent_idle_fallback`) で inconclusive — 実行時点で codex rule cache が不在だったため rule 照合自体が行えていない (review finding 1 で記録漏れが指摘され追記)。
- 学び (失敗): **working 検知は offline で検証できない**。`explain --file` は本文 region 専用。OSC-title 依存 rule の検証には実 PTY + TUI が必須 — これが session 2 (live) を要求した根本理由。

### E6. headless server の限界

- 目的: 自動化 harness だけで live 検証を完結できるか確認。
- 実行: headless `herdr server` + `agent start` で script pane を起動し、OSC を出力させ `agent get` / `agent read` を確認。
- 結果 (失敗): pane は作れるが `agent read` は空文字、`revision: 0`、`agent_status: unknown` のまま。**TUI client が attach していないと PTY 描画も検知も走らない**。
- 学び: herdr の検知は「描画された画面」に対して動く。headless では (a)(b)(c) の live 実測は不可能。自動化のみで完結させようとせず、operator の実端末とのハイブリッドに切り替えた。

## Session 2 — 実機 live (operator の VS Code + 実 Claude)

### E7. working 検知 — 誤読と確定

- 目的: (a) の核心「working 中に false idle/done を出さないか」。
- 実行: operator が herdr TUI 内で `claude` を起動しタスク投入。coordinator 側から `agent explain w1:p1 --json` を polling (probe.py)、のち 30 秒 sampler。
- 結果:
  - 初回: `STATE=idle`、OSC title `✳ ファイルの内容を確認` 固定 → 「作業中なのに idle」に見えた。
  - **実際はタスクが約 5 秒で完了しており、観測時点では本物の idle だった** (operator 申告で判明)。
  - 重いタスクで再測: **30 秒間 5355 サンプル全て `working`**。OSC title が braille spinner (`⠂`/`⠐`) で回転し `osc_title_working` が安定 match。**false idle/done ゼロ**。完了後は `✳` title → idle、ターン直後は `done` 状態も区別される。
  - pinned 旧 manifest (2026.06.10.3) が最新 Claude Code v2.1.201 を正しく判定 — pin による drift 懸念は現時点で顕在化せず。
- 学び (誤読の教訓): 「作業中に idle 表示」を見たら、まず**タスクがもう終わっていないか**を疑う。state snapshot の解釈には対象の実時間コンテキストが要る。検証は単発 probe でなく連続 sampler で行うこと。

### E8. socket injection round-trip

- 目的: coordinator (別プロセス) から pane 内 agent へタスクを投入できるか — mozyo-bridge handoff の等価操作。
- 実行: `pane send-text w1:p1 "<メッセージ>"` → `pane send-keys w1:p1 enter` → explain polling → `agent read`。
- 結果: **完全な往復が成立**。注入直後に working 遷移 (turn-start 検知)、pane 内 Claude が応答 (`herdr注入OK`)、`agent read` で応答本文を取得、idle 遷移で turn-end 検知。現行 tmux の `send-keys` + `capture-pane` heuristics が socket API に置換可能であることの実証。
- 学び (問題): 注入文の前に残キー `qq` が前置されて届いた (composer に残存入力があった模様)。**adapter 実装では注入前の composer clear 手順が必須** — C-u clear の信頼性問題という既知教訓と同種。

### E9. `wait agent-status` の semantics — 予測と違った

- 目的: (c) の wait primitive の判別能力 (未開始/相手なし/開始)。
- 実行: idle 中に `wait agent-status w1:p1 --status working --timeout 2500` (c1) / `--status idle` (c2)、存在しない pane に wait (c3)。
- 結果:
  - c1: timeout (exit 1) — 「未開始」を正しく区別。
  - **c2: timeout — 予測 (「既に idle なので即マッチ」) に反した**。エラー文言 `timed out waiting for agent status change` の通り、wait は「**その状態への変化**」を待ち、既にその状態でも返らない。
  - c3: pane get error — 「相手なし」を区別。
- 学び (設計拘束): **check-then-wait 必須** — wait 発行前に現状態 snapshot を読まないと「発行前に遷移済み」race で hang する。また turn-start の陽性確認は polling で観測したもので「wait が遷移時に返る」ことは未実証 (訂正 j#72241)。formal 4-case harness (delivered-not-started / absent / blocked / started) は adapter フェーズで実装する。

### E10. identity の restart 永続

- 目的: (b) ephemeral pane id に依存しない durable handle の存在確認。
- 実行: `agent rename w1:p1 poc_claude` → `server stop` (完全停止を確認) → server 再起動 → `agent list` / `pane list`。
- 結果:
  - **付与名 `poc_claude` は restart を越えて永続** ✅。pane 位置 `w1:p1` / tab / workspace レイアウトも復元。
  - `terminal_id` は per-process 再生成 (使い捨て) — durable handle には使えない。
  - 会話 session は自動復活せず (`agent=None`)。`resume_agents_on_restore` (default true) は**公式 integration hook が session ref を報告していることが前提**で、今回は operator の `~/.claude` 非侵襲を優先し hook 未導入のため復活しないのは想定どおり。
- 学び: mozyo-bridge の lane/Redmine 紐付けに使う handle は「**herdr 付与名**」一択。session 復活まで求めるなら integration hook の opt-in 導入が条件。

### E11. pane content 取得 (付随収穫)

- 目的: 現行 `capture-pane` heuristics の代替確認。
- 実行: `agent read poc_claude --source visible --lines 30` (他に `recent` / `recent-unwrapped`)。
- 結果: 実画面 3965 bytes を JSON で取得 (truncated flag 付き)。折返し解除版 (`recent-unwrapped`) もある。
- 学び: transport 層だけでなく観測層 (delivery record の pane-body 証跡) も API 化できる。

## Session 3 — review findings 補完 (実機 live、exec ラッパー版 helper 使用)

Codex evidence review (#13175 j#72247) の findings 2 件 (live blocked/Codex 不足、4-case harness 未実施) を実測で補完した。pane 内 agent は `agent-clean` (XDG redirect 除去) で起動しており、session 2 の env 汚染 caveat を解消した条件での測定。

### E12. started case — wait が遷移で返ることの実証

- 目的: E9 で発覚した change-semantics の下で、check-then-wait パターンが実際に成立するか (review finding 2 の核心)。
- 実行: `wait agent-status <claude pane> --status working --timeout 45000` を background で先に arm → `pane send-text` + `send-keys enter` で注入 → wait の返りと時刻を計測。
- 結果: wait は event JSON (`pane.agent_status_changed` → working) を出力して exit 0。**注入 Enter 完了から wait 返りまで約 0.36 秒**。event-driven であり polling 不要。
- 学び: 「先に wait を arm してから注入する」順序で change-semantics の race は回避できる。adapter の送信 rail はこの順序を契約にする。

### E13. Claude blocked live + blocked 判別

- 目的: 承認待ち (blocked) の live 検知と、blocked 中の not-started 判別 (review finding 1 + 2)。
- 実行: auto mode OFF の Claude pane に `wait blocked` を arm → 承認要タスク (`touch /tmp/poc-e13-test.txt` の実行依頼) を注入 → blocked snapshot と `wait working` timeout を計測。操作は operator が拒否で終了 (副作用なし)。
- 結果: wait blocked が event で返り exit 0 (注入から約 6.7 秒 — Claude の tool 判断+プロンプト描画時間込み)。snapshot は `STATE=blocked` / rule `generic_permission_prompt` / visible_blocker=True。**blocked 中の `wait working` は 8 秒 timeout (exit 1)** = delivered-but-not-started の fail-closed 判別。blocked 継続中の false done/idle/working ゼロ。
- 学び (失敗込み): 初回試行は operator の model 切替と重なり wait が timeout した (測定中の環境変更は再実行で潰す)。

### E14. Codex live 検知 (working / done / blocked)

- 目的: Codex 側の live 検知 evidence (review finding 1)。
- 実行: codex-cli 0.142.5 を herdr pane 内で起動。working/done は通常起動、blocked は `-a untrusted` で再起動して承認プロンプトを発生させた。
- 結果:
  - herdr は pane を `agent=codex` と自動識別。
  - working: wait arm → 注入 → **event 返り 0.36 秒** (Claude と同水準)。
  - done: turn 終了を `wait done` event で捕捉、応答本文も `agent read` で取得。
  - blocked: `STATE=blocked` / rule `osc_title_blocked` (Codex は blocked も OSC title 検知) / visible_blocker=True。blocked 中の `wait working` timeout (exit 1)。承認プロンプトの実画面を API で照合。
- 学び (重要):
  - **初回注入で text は composer に着弾したが Enter が不着** — tmux 時代から既知の Codex TUI Enter 癖が herdr transport でも再現した。Enter 再送で submit 成功。transport 置換は agent TUI 側の submit 癖を解消しない。adapter の Codex 送信 rail には Enter 再送/確認 logic (現行 #13166 相当) の移植が必要。
  - この version (0.142.5) に TUI 内 `/approvals` コマンドは無い。承認モードは起動フラグ `-a, --ask-for-approval` で与える (推測でコマンド名を案内せず `--help` で実物を確認してから operator に渡す、という手順教訓も込み)。
  - `wait done` を arm した際、直前に done 遷移済みのケースで即 event 返り (11ms) を観測した例があり、event 配送の購読時挙動は adapter フェーズで仕様確認する (fail-safe 側)。

### 4-case harness 充足まとめ

| case | evidence |
|---|---|
| started | E12: wait 返り 0.36s (event) |
| blocked (delivered-not-started) | E13/E14: blocked 中 wait working timeout + check-then-wait 規約 (注入前 snapshot 非 idle は fail-closed) |
| absent | E9 c3: 存在しない pane への wait は pane get error |
| turn-end | E14: wait done event 捕捉 |

## 環境衛生の設計ミスと訂正 (operator 指摘)

- 失敗: live helper 初版 (`env.sh`) は「source して `XDG_CONFIG_HOME` 等を export する」設計だった。XDG 変数は herdr 専用ではないため、operator のシェル全体と全子プロセスに漏れる — `git` は `$XDG_CONFIG_HOME/git/config` を実設定より優先するし、herdr server 経由で **テスト対象の claude 自身も redirect された XDG を継承**していた。加えて session 1 の restart テストでは server を `env -i PATH=/usr/bin:/bin` の最小環境で再起動したため、pane shell が `claude` を PATH 解決できない事象も起きた (env 伝播の扱いの雑さとして同根)。
- 訂正: helper を **exec ラッパー型** (`live/hb`) に書き直し、env は herdr プロセスにのみ付与。pane 内で agent を起動する際は `live/agent-clean` で XDG redirect を外す (herdr 検知に必要な `HERDR_ENV` / `HERDR_SOCKET_PATH` / `HERDR_PANE_ID` は保持)。
- evidence への caveat: session 2 の (a)/(c) evidence は「XDG redirect を継承した claude」で取得された。OSC-title 検知・state 判定への実影響は無いと判断するが (検知は端末描画に対して動き XDG に依存しない)、厳密な再現条件として記録する。
- 隔離の限界の明示: 本 PoC の「sandbox」は herdr のデータ置き場の分離であり、セキュリティ境界ではない。binary は user 全権限で動き実 HOME を読み書きできる。session 2 の実 HOME 使用は j#72065 の隔離 HOME 方針からの逸脱 (agent auth のため、journal 開示済み)。信頼根拠は sha256 pin と外部通信遮断の実測である。

## 運用上の細かい教訓 (tooling)

- macOS に `timeout` コマンドはない。sampler は python 側でループ時間管理する。
- zsh の heredoc/quoting 内で python f-string を書くとエスケープ地獄になる — probe/sampler は最初から `.py` ファイルに落とすのが速い。
- POSIX sh は関数名にハイフンを許さない (`hb-state` は bash/zsh 拡張)。配布 helper は `hbstate` のような名前にする。
- `git cat-file -e` 系と同様、プロセス単位の観測 (lsof) は子プロセスに盲目 — 成果物側で判定する (E3)。

## 未消化 / 引き継ぎ

- Codex evidence review の re-review (findings 補完 j#72258 に対する) + direct_owner close approval (#13175 close 条件)。
- go 確定時の adapter US 分割案: j#72240 の 6 分割 (transport / state / identity / turn-start / pin config + hook installer / license disposition)。E12〜E14 の追加拘束: check-then-wait 契約 / Codex Enter 再送 rail / event 購読時挙動の仕様確認。
- AGPL license disposition は owner/legal 判断 (bundle/fork/再配布/hosted service は escalate)。
