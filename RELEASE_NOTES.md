# リリースノート

このファイルは、各リリースで何が変わったのか、そしてなぜ必要だったのかを人間向けに説明するためのものです。単なるコミット履歴ではなく、プロダクトとしての流れが分かる粒度で書いています。

記載は Git の release commit と利用可能な tag を元にしています。一部の過去バージョンは release commit はありますが、現在の repository には対応する tag がありません。

## Unreleased

次の release 候補 — Version #218 `v0.7.1 cockpit dogfooding stabilization`。

この期間の主題は、v0.7.0 で建てた **workspace 横断 session 基盤 / ユニット状態コックピット** を実運用(dogfooding)で **安定化**することです。registry-aware な identity 解決の完成、cockpit サブレーン運用の primitive 強化と運用 runbook の整備、scaffold 配布物の最小化方針の固定が中心です。いずれも additive / 後方互換で、既存 CLI の JSON / text 出力互換は壊していません。version 決定 / tag / publish はこのメモでは行わず、別 gate と owner 承認のもとで実施します。

### registry-aware identity の完成

- `doctor` を registry / anchor / runtime の整合性診断へ拡張しました。home registry の有無・schema・readability、workspace 登録状態、anchor との不一致、`last_seen` と tmux runtime の関係を read-only で診断します。(#11426)
- smart `init` を registry-aware にしました。未登録 workspace は guarded adoption の中で登録し、既登録 workspace は registry の canonical session を再利用し、`@mozyo_agent_role` pane option で role bind を安定化します。fail-closed 順序(preflight 後・tmux/vscode mutation 前に登録)を維持します。(#11427)

### unit target model / TargetRecord projection

- unit target model を設計 doc 化し、design gate を明確化しました。(#11906)
- `agents targets` を TargetRecord canonical projection へ拡張しました。(#11907)
- handoff の explicit-pane preflight を同じ TargetRecord projection 経由に統一しました。(#11908)
- unit presentation state の DB 境界を doc 化しました。(#11909)

### Claude pane auto permission mode の起動 policy

- managed Claude pane の permission mode を pure policy module で解決するようにしました(env override > launch-context policy default > none)。cockpit / layout / sublane(cockpit append)の pane 生成は launch-context default `auto` を渡すため、`MOZYO_CLAUDE_PERMISSION_MODE` が未設定でも managed Claude pane は再現的に `claude --permission-mode auto` で起動します。standalone `mozyo` window は default を渡さず従来の bare `claude` 起動を維持し、env var は互換 / 明示 override rail(設定時に優先)として残ります。Codex pane は影響を受けず、repo-local `.claude/settings.json` も書きません。`doctor` に read-only な `claude_launch_policy` section を追加し、未設定 / override で auto にならない状態を `--dry-run` plan で検出できます。非遡及(起動済み pane の mode は変えません)。これは #11850 PoC で観測された「Claude pane を毎回手動で auto mode に切り替える」摩擦の解消です。(#11924 / #11925, commit `8566883`)

### workspace anchor / project-defaults rename 互換

- workspace anchor の project-defaults rename について migration を doc 化しました。(#11910)
- rename の後方互換を実装し、現行 path を壊さずに移行できるようにしました。(#11921)
- active docs / install text で `project-defaults.yaml` を primary 名として明記しました。(#11921)

### 配布物最小化 / worktree 境界 / 運用 runbook の方針固定

- scaffold 配布物を 4 分類し、home registry + thin anchor 中心の最小化目標像・移行/後方互換境界を方針正本化しました。(#11428)
- git worktree lifecycle を mozyo-bridge core ではなく skill / runbook / operator recipe で扱う責務境界を固定しました(core は identity / discovery / safety primitive に限定)。(#11889)
- 他プロジェクトでも再現できる portable な sublane / worktree 運用 runbook を整備しました。(#11929)
- local-only catalog overlay(`catalog.local.yaml`)の governance を配布しました。(#11922)

### cockpit 運用 docs

- cross-project cockpit smoke runbook を整備しました。(#11911)
- local / remote cockpit host 境界を doc 化しました。(#11817)

### リリース準備メモ(release gate 用)

本 Unreleased 期間を Version #218 `v0.7.1 cockpit dogfooding stabilization` の release 候補として整理します。version 決定 / tag / publish はこのメモでは行わず、別 gate と owner 承認のもとで実施します。

- **変更概要**: registry-aware identity の完成(#11426 / #11427)、unit target model / TargetRecord projection(#11906 / #11907 / #11908 / #11909)、Claude pane auto permission mode 起動 policy(#11924 / #11925)、workspace anchor / project-defaults rename 互換(#11910 / #11921)、配布物最小化・worktree 境界・portable runbook の方針固定(#11428 / #11889 / #11929 / #11922)、cockpit 運用 docs(#11911 / #11817)。いずれも additive で、破壊的 CLI rename を含みません。
- **version 候補の考え方**: v0.7.0 cockpit ラインの stabilization + additive であり、破壊的変更を含まないため、Version 名(#218 `v0.7.1`)どおり `0.7.0` からの **patch(0.7.1)** が妥当な目安です。最終的な version 決定は owner 判断とします。
- **release gate の実行手順**: 実際の検証・配布手順は `skills/mozyo-bridge-agent/references/release.md` の Standard Verification / Release Flow / Release Artifact Guardrails / Distribution Gates / Trusted Publishing を正本とします。version bump は standalone commit とし、`main` push → GitHub Actions `Test` 成功確認 → TestPyPI → `pipx` install 検証 → owner 判断で production PyPI、の順で進めます。本メモはこの gate の入口を明確化するもので、本 issue では gate を実行しません。

## v0.7.0 - 2026-06-12

`0.6.2` から minor 相当の additive な機能追加をまとめた release です。tag / TestPyPI / production publish は別 gate と owner 承認のもとで実施します。この期間の主題は **workspace 横断の session 基盤** と、その上に建てた **ユニット状態コックピット** です。「複数 workspace で今どの agent が動いていて、どこで止まっているか」を発見・観測・ジャンプできるところまでを、既存の best-effort / runtime-first 原則を崩さずに積み上げました。

### workspace 横断 session inventory

- `mozyo-bridge session list [--json]` を追加しました。複数 workspace で起動している mozyo session / agent pane を一覧化し、operator や外部 UI が既存 session を安全に発見できます。live tmux runtime を正本に毎回収集し、`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite` を durable cache として使います。SQLite を唯一の真実にはせず、tmux が見えないときだけ cache を `stale: true` 明示で返します。(#11421 / #11422)
- inventory の同一性キーを `pane_id` に統一し、`agents list` も同じ model に揃えました。tmux session group で 1 つの pane が複数 session に所属しても、agent としては 1 行に畳み、所属 session は `views` 配列で保持します。コックピット上で台数を数え間違えたり二重通知したりする事故を防ぎます。(#11628)
- session 名導出の Unicode 正規化差(NFC / NFD)バグを修正しました。macOS の readdir は NFD、文書・Redmine・agent 経由のパスは NFC で、同じ日本語ディレクトリから別々の session 名が導出され同一 workspace に複数 session が生まれていました。path identity を NFD に固定する共有 helper を入れ、session 名 hash・handoff の `--target-repo` gate・inventory の registry 照合をすべて同じ正規化に通します。(#11625)
- 上記基盤は home-registry-first の workspace identity(registry → anchor → 導出)を踏襲しており、home / registry が消えても workspace-local anchor から identity を復元できます。受け入れ検証として、初回作成・再利用・home 消失復元・JSON schema・日本語/長い/non-git path・stale cache の各ケースを確認しました。(#11423)

### ユニット状態コックピット(OTel イベントストア + 三層 join)

「今 Claude / Codex のどちらで止まっているか」を見えるようにし、当該ユニットへワンアクションで跳ぶための中核機能です。Redmine=誰の番か / OTel ストア=動いているか / tmux=生きているか、の三層を join して表示します。(#11639)

- **OTel イベントストア(段階1)**: agent CLI が直接発行する OpenTelemetry を localhost で受ける自前 OTLP/HTTP 受け口と、SQLite single-writer のイベントストアを追加しました。公式 Collector は立てず、既存の Python/pipx 資産の延長に収めています。受動・帯域外のため、壊れても「データが来ない」に留まり agent 本体に波及しません。プロンプト本文は保存せず、usage / イベント種別 / 最小 metadata のみを allowlist で保存します(本文系 key は deny 優先で落とす)。Claude Code の実測でイベント深度が入力待ち判定に足ることを確認しました。
- **inventory への activity 統合(段階2)**: session bootstrap で agent 起動時に OTel endpoint と join 用属性(`mozyo.session` / `mozyo.agent` / `mozyo.workspace_id`)を注入し、`session list` の各 unit に activity(active / idle / unknown)を additive に join しました。OTel の沈黙は「待機」と「死亡」を区別できないため、idle / unknown は死亡を意味せず tmux 生死確認へ縮退します。`doctor` に受け口 health と「telemetry を一度も発していない agent(観測漏れ)」検出を追加しました。
- **コックピット Web UI(段階3)**: 同じ daemon が `http://127.0.0.1:4318/` に unit 一覧 UI を配信します(127.0.0.1 のみ。iTerm2 Toolbelt webview / 任意ブラウザで同一 UI)。各 unit の状態表示・状態遷移フィード・Reveal in Finder・ジャンプ(attach client への `tmux switch-client`)を提供します。action は明示クリックでのみ実行し(自動前面化なし)、Content-Type / per-process token / loopback Origin の三重ゲートでブラウザ経由の意図しない実行を防ぎ、UI は DOM API のみで描画して workspace 名由来の HTML/JS 注入を塞いでいます。Toolbelt 登録手順は runbook 化しました。
- **Redmine gate 表示(段階4)**: daemon の環境変数で信頼 Redmine base URL と API key を渡すと、各 unit にその workspace の最新更新 open issue(gate / workflow 文脈)を読み取り専用で表示します。送信先は環境変数の URL だけに固定し、repo 側の設定ファイルが送信先を変えることはできません(host 不一致は fetch せず `unconfigured`)。Redmine への書き込みは一切せず、ランタイムのハートビートを journal に残しません。
- **launchd 常駐**: `mozyo-bridge otel launchd install / status / restart / uninstall`(macOS)で受け口を常駐できます。plist には環境変数を一切書かないため secret が乗りません。upgrade 後の restart 手順を runbook 化しています。

### managed desired-state event log の設計・PoC・command-boundary 配線

mozyo-bridge 管理下の session / workspace について「mozyo が何を作ろうとしたか(desired state)」を replay 可能な event log として正本化し、「今 生きているか(observed liveness)」は live tmux 正本のまま維持する、という 4 層モデルを設計 doc 化しました。registry=identity / event log=desired / live tmux=liveness・handoff / inventory=projection の境界を明文化し、managed marker は registry anchor 一次 + tmux user option 二次(session 名 prefix は権限境界にしない)としています。設計に続いて、marker 分類と desired-state append の最小 PoC を実装しました(liveness / handoff には一切触れない)。(#11695 / #11698)

さらに、その append surface を mozyo が managed pane を作る唯一の境界(`new_agent_session_window` / `new_agent_window` の pane_id 確定直後)へ最小配線しました。`created` event を best-effort で append し(pane_id を identity key、session を attribute、repo_root を NFD 正規化、`socket` 拡張点を保持)、あわせて二次 marker を付与します。append / marker が失敗しても pane 生成コマンド本体は壊れません(telemetry と同じ posture)。liveness / handoff target 解決 / preflight / tmux discovery は live tmux 正本のままで、runtime 正本 module に event log / marker の read path を足していないことを guard test で固定しています。observable contract(外から呼ぶと event が残る / 失敗時も pane_id を返す / 正本境界を侵さない)を古典学派寄りに検証しています。(#11726)

### Claude design consultation の運用ルール化

owner / Codex の設計議論を実装担当へ「Design Consultation」として投げ、実装側から反証・懸念・最小 task 分割を返す進め方を、再現可能な project-local rule として整備しました。毎回必須にはせず、正本境界・責務境界・security/credential/handoff・後戻りコストが高い場面だけ発火する high-signal gate として位置づけています。(#11702)

### operator QoL(tmux UI パック)

- 複数 iTerm2 window が全部「tmux」表示で見分けられない問題に対し、`agent-ui.conf` に OS window title(session 名ベース)を追加しました。あわせて `<prefix> f` で現在 pane の作業ディレクトリを Finder で開く keybinding と、tmux の `extended-keys` 透過(Claude Code の Shift+Enter 改行を tmux 越しに効かせる)を追加しています。役員環境向けの再現可能な端末セットアップ runbook も整備しました。(#11638)
- handoff の `--target 'session:window名'` 形式が、対象 pane が存在していても常に失敗していたバグを修正しました。location 形式を pane id へ正規化するようにし、cross-session gateway の案内どおりの指定が実際に通るようになりました。(#11666)

これらはいずれも docs / 機能の追加です。本 release で version を `0.7.0` に確定しました。tag / publish は別 gate と owner 承認のもとで実施します。

### リリース準備メモ(release gate 用)

本 Unreleased 期間を 1 つの release 候補として整理します。version 決定 / tag / publish は本メモでは行わず、別 gate と owner 承認のもとで実施します。

- **変更概要**: workspace 横断 session inventory(#11421 系)、pane_id 同一性統一(#11628)、Unicode NFC/NFD 由来の session 名分岐修正(#11625)、ユニット状態コックピット 4 段階 + launchd 常駐(#11639)、operator QoL / tmux UI パックと handoff `--target` バグ修正(#11638 / #11666)、managed desired-state event log の設計・PoC・command-boundary 配線(#11695 / #11698 / #11726)、Claude design consultation の運用ルール化(#11702)。いずれも additive で、既存 CLI の JSON / text 出力互換を壊していません。
- **version 候補の考え方**: 破壊的 CLI rename を含まず additive 中心(新 `session list` / コックピット / OTel 受け口 / managed-state surface 等の機能追加)のため、直近 `v0.6.x` からの **minor 相当**が妥当な目安です。最終的な version 決定は owner 判断とします。
- **未確認 / 残リスク**: launchd 配下への安全な API key 受け渡し(現状 launchd 常駐時は cockpit の Redmine 層が `unconfigured`)、Codex / Gemini の OTel event depth 実測、Linux での NFC-byte 既存 session 名の 1 回限りの変化(`workspace register` で緩和)、inventory / cockpit への managed / unmanaged additive 表示は次段階。
- **release gate の実行手順**: 実際の検証・配布手順は `skills/mozyo-bridge-agent/references/release.md` の Standard Verification / Release Flow / Release Artifact Guardrails / Distribution Gates / Trusted Publishing を正本とします。version bump は standalone commit とし、`main` push → GitHub Actions `Test` 成功確認 → TestPyPI → `pipx` install 検証 → owner 判断で production PyPI、の順で進めます。本メモはこの gate の入口を明確化するもので、本 issue では gate を実行しません。

## v0.6.0 - 2026-06-07

v0.6.0 は、#11050 / #11051 系で実装した read-only な復旧導線 `mozyo-bridge doctor instruction` の追加と、repo-local LLM runtime config command の `instruction doctor` / `instruction install` → `runtime-config check` / `runtime-config install` への rename をまとめた minor release です。破壊的 CLI rename を含むため patch ではなく minor bump とし、旧 `instruction doctor` / `instruction install` は deprecated alias + stderr 警告として 1 minor cycle(削除候補は v0.7.0 以降)維持します。push / tag / TestPyPI / PyPI publish は本 release notes では行わず、別 gate と owner 明示承認のもとで実施します。

### 変更点

- 環境復旧導線の read-only runbook として `mozyo-bridge doctor instruction` を追加しました。`doctor` は従来どおり read-only な env 診断(`cli` / `rules` / `codex_skill` / `claude_skill` / `scaffold` / `claude_nagger` / `tmux`)に徹し、`doctor instruction` はその診断結果を消費して「どの順で何を直すか」を番号付きの復旧手順に並べます。手順は central rules → agent skills → scaffold drift → runtime config → 最終検証の順で、agent skill では Claude の primary path(plugin marketplace)と legacy fallback(curl script)、Codex の primary path(`$skill-installer`)と fallback を明示し、scaffold drift は `scaffold status` / `scaffold diff` で差分を確認してから `scaffold apply --backup` で復元する review-before-restore 導線として案内します。`doctor instruction` は read-only で、install / write / network call は行いません。text 出力には CLI taxonomy の migration(旧名→新名)セクションも含みます。(#11050 / #11051)
- **破壊的変更**: repo-local LLM runtime config command を rename しました。`mozyo-bridge instruction doctor` → `mozyo-bridge runtime-config check`(read-only)、`mozyo-bridge instruction install` → `mozyo-bridge runtime-config install`(write-capable, dry-run default)です。新 `doctor instruction` runbook と旧 `instruction doctor` の語順衝突を解消し、"instruction" という語を `doctor` 配下の runbook に限定するための整理です。canonical command の text 出力ヘッダ・write 後メッセージ・docs はすべて新名を正本とします。(#11051)
- 旧 `mozyo-bridge instruction doctor` / `instruction install` は **deprecated alias** として 1 minor cycle 残します。実行すると機能は新 command と等価のまま動きますが、stderr に `deprecated: ... use mozyo-bridge runtime-config check/install ...` の警告を出します。旧 alias の削除は次 minor 以降(v0.7.0 想定)の候補で、最終時期は release planning 側で確定します。(#11051)
- 後方互換のため、`mozyo-bridge doctor --json` の schema は additive に保ちました。top-level `ok` と `sections.*` の既存 key / 形状は変更しておらず、`jq '.sections.scaffold.status'` などの既存 CI gate はそのまま動きます。`doctor instruction --json` は別 shape(`steps` / `migrations` / `pending_step_ids` 等)です。deprecated alias の警告は **stderr のみ**で、alias を `--json` 付きで実行しても stdout の JSON は汚染されず、そのまま parse できます。(#11051)
- 本 repository 自身の `mozyo-bridge doctor` を green に復旧しました。repo-root `<repo>/.codex/config.toml` の整備と scaffold manifest の更新による drift 解消で、`doctor` が `result: ok` を返す状態に戻しています。これは配布 CLI の挙動変更ではなく、本 repo の self-host 環境を taxonomy 変更後の状態に追従させた運用復旧です。(#11112)

### なぜ必要だったか

#11050 / #11051 は、`doctor` / skill / scaffold / instruction 系 command が増えるにつれ、環境復旧時に「どの command を、どの順で、primary とfallback のどちらで」実行すべきかが分かりにくくなっていた問題に対する整理です。`doctor` は診断に徹したまま、復旧手順そのものは read-only な `doctor instruction` runbook に切り出すことで、診断(何が壊れているか)と復旧導線(どう直すか)の責務を分けました。同時に、新しい `doctor instruction` と既存 `instruction doctor` は語順が紛らわしく、まさに今回解消したかった「分かりにくさ」の典型だったため、後者を含む `instruction` group を `runtime-config` へ rename しています。

破壊的 rename は published CLI のユーザに影響するため、いきなり旧名を削除せず 1 minor cycle の deprecated alias + stderr 警告で猶予を設け、既存スクリプトや手癖を即座に壊さないようにしました。一方で恒久 alias にすると taxonomy 整理の効果が薄れるため、削除候補であることを明記しています。`doctor --json` を additive に保ったのは、`doctor --json` を gate に使う CI / 自動化を壊さないためで、alias 警告を stderr に限定したのも同じく JSON 消費者を守るためです。version bump / publish / tag を本 issue scope 外に切ったのは、taxonomy + docs + tests の実装と、実際の release 操作を別 task として分離する合意(#11051 設計相談)に従ったものです。

## v0.5.6 - 2026-06-03

v0.5.6 は、v0.5.5 の TestPyPI smoke で見つかった top-level CLI help の矛盾を直す correction patch release です。**v0.5.5 は TestPyPI にのみ配布され、production PyPI には出していません。** production 配布対象は本 v0.5.6 です。

### 変更点

- `mozyo-bridge --help` の `instruction` group summary を修正しました。v0.5.4 まで `instruction` は read-only group でしたが、#10930 で write 可能な `instruction install --write` が加わったため、group 全体を `(read-only)` と表示するのは誤りでした。top-level summary を「`doctor`(read-only check)と `install`(write-capable, dry-run by default)」と責務差分が分かる文言に改めています。`instruction doctor` の read-only 説明と `instruction install` の write-capable / dry-run default 説明はそれぞれ維持し、top-level help に stale な `(read-only)` group 表現が戻らないことを test で pin しました。(#10932)

### なぜ必要だったか

#10932 は、v0.5.5 を production PyPI へ出す前に CLI help の事実誤認を正すためのものです。#10930 で `instruction` は read-only group ではなくなったのに top-level help は古い `Opt-in checks for repo-local LLM runtime config (read-only)` のままで、新機能 `instruction install --write` と矛盾していました。help は利用者が最初に読む契約面なので、誤表記のまま production へ出すと「instruction は read-only」という誤解を配布することになります。v0.5.5 TestPyPI smoke でこの矛盾を検出し production publish を blocker としたため、文言修正 + 回帰 test を入れた v0.5.6 を production target にしています。

## v0.5.5 - 2026-06-03

> 注: v0.5.5 は TestPyPI にのみ配布されました。production PyPI へは出していません(top-level help correction のため v0.5.6 に差し替え。#10932 参照)。

v0.5.5 は、v0.5.4 で入れた session identity / runtime config 周りの仕上げとして、`workspace-defaults.yaml` の正本から Codex runtime config を生成する install 経路を追加する patch release です。

### 変更点

- `mozyo-bridge instruction install --profile redmine-codex --target .` を追加しました。`instruction doctor` が「正本はあるが `<repo>/.codex/config.toml` が無い」状態を検出するだけだったのに対し、これは検査と修復の間の手作業を埋める write 側経路です。`<repo>/.mozyo-bridge/workspace-defaults.yaml` の **verified** な Redmine default project を正本として、repo-root `<repo>/.codex/config.toml` の `[redmine]`(`default_project` / `default_project_name` / `default_project_url`)と `[mcp_servers.redmine_epic_grid]`(`url` + `http_headers.X-Default-Project`)を生成 / merge します。MCP RPC URL は `default_project.url` の host から導出するため、配布 source に project 固有 host を焼きません。default は dry-run で、実書き込みは `--write` を明示したときだけ行います。既存 config がある場合は無関係 table を保持して append し、managed table が既存値と conflict する場合は fail して operator 確認を促します(`--force` は managed table のみ再生成し、他 table は保持)。unverified な default project や credential-shape 値は拒否し、credential は一切生成しません。書き込み後は `instruction doctor` が green になることを検証する lockstep を持ちます。`.mcp.json` は引き続き deferral(runtime 検証なしに authoritative 生成しない)です。(#10930)

### なぜ必要だったか

#10930 は、Redmine default project の正本(`workspace-defaults.yaml`)が存在し `mozyo-bridge workspace-defaults --check` が clean でも、`<repo>/.codex/config.toml` が無ければ `instruction doctor` が fail する、という「検査はあるが修復は手作業」の段差を埋めるためのものです。v0.5.4 までで「正本の docs 化」「session identity の機械化」「runtime config の read-only 検査」は揃っていましたが、正本から runtime config を反映する install 経路だけが欠けていました。`instruction install` で `workspace-defaults.yaml` → `workspace-defaults --check` → `instruction install --write` → `instruction doctor` green という一連の流れを機械化し、LLM startup / bootstrap の設定漏れを減らします。正本の二重管理を避けるため値は projection に限定し、unverified default や home config 書き込み、credential 生成といった危険な近道は塞いでいます。

## v0.5.4 - 2026-06-03

v0.5.4 は、v0.5.3 以降に入った bootstrap 入口の整理、distributed governed preset への Codex pre-edit 分類 gate 配布、そして日本語 / 非 ASCII workspace と VS Code tmux-integrated 環境での agent repo identity 喪失の抜本対応をまとめた increment です。

### 変更点

- bootstrap docs の入口を整理しました。`README.md` の Quick Start を install / bootstrap の入口に戻し、最初に `mozyo-bridge doctor --target .` と `mozyo-bridge instruction doctor --target . --profile redmine-codex` を順に実行する判断順を README に置きました。`doctor`(toolchain health)と `instruction doctor`(repo-local LLM runtime config の合否正本)の責務差分、`instruction doctor` の代表的 failure(`.codex/config.toml` missing / `X-Default-Project` mismatch / `.mcp.json` deferral / credential 混入)と次に読む FAQ への導線を README に追加しています。`vibes/docs/logics/bootstrap.md` は「canonical entrypoint」から、詳細 stage order / FAQ / troubleshooting の reference へ降格し、`instruction doctor` FAQ(各 failure の原因・対処、home config 禁止理由、agent が自動修復してよい範囲と operator 確認が必要な範囲)を Stage 7 に追加しました。(#10857)
- distributed governed preset (`redmine-governed` / `redmine-rails-governed`) の `agent-workflow.md` に **Codex Pre-Edit Classification Gate** を追加しました。Codex が `apply_patch` / file 作成・更新 / commit の前に「どの実装主体に属するか」を分類することを求め、repo 内の正本成果物は拡張子・内容種別に関係なく(Markdown / HTML / 調査メモ / ドラフト / 表 / report / runbook / 設定例も)実装成果物として扱う、「コードではない」「commit hash を journal に書く必要がある」を direct edit の根拠にしない、direct edit は Repo-Local Guardrail Autonomous Lane か `codex_direct_edit` gate のみ、誤った先行成果物は完了扱いにせず correction flow へ戻す、を distributed preset 側で明文化しています。canonical source から両 preset を再生成し、両 governed preset VERSION を `2026.06.02.1` に bump しました(配布内容変更に伴う version mirror 整合)。(#10899)
- 日本語 / 非 ASCII workspace basename や VS Code tmux-integrated 環境で tmux session identity が失われる問題に抜本対応しました。VS Code tmux-integrated / TaskPilot menu は basename を sanitize して session 名を作るため、`2026PBL_ローカル` のような basename は `2026PBL_____` のような低情報量名へ潰れ、複数 workspace の `____` session が衝突して `mozyo-bridge agents list` / `--target-repo` handoff gate が実 repo identity を復元できなくなっていました。新 CLI `mozyo-bridge session name --repo <path>`(`--json` 対応)を追加し、`<repo>/.mozyo-bridge/workspace-defaults.yaml` の `redmine.default_project.identifier` があれば `mozyo-<identifier-slug>` を、無ければ repo path の短い hash を suffix した collision-safe fallback `mozyo-<basename-slug>-<hash>` を返す導出に統一しました。非 ASCII basename は `____` に潰さず、同名 / 非 ASCII basename でも path hash で区別されます。bare `mozyo` と `mozyo-bridge status` の session 解決もこの導出に揃え(明示 `--session` override と current tmux session 優先は維持)、旧 basename session が残っている場合は移行 notice を表示します。さらに `mozyo-bridge session vscode-settings --repo . --write` を追加し、workspace-local `<repo>/.vscode/settings.json` の `tmux-integrated.sessionName` を導出名に設定します(user-global VS Code settings / credential は読み書きせず、コメント付き JSONC は壊さず手編集を促して停止)。README / bootstrap / workspace-defaults renderer docs に VS Code 向け運用・移行手順・TaskPilot snippet を追記しています。`--session` override / 誤 attach guard / `init` の同名 window fail-closed / `--target-repo` gate の安全境界は変更していません。(#10796)

### なぜ必要だったか

#10857 は、最初に読む入口が深い docs 側(`bootstrap.md`)に残っていると、LLM が詳細 docs を読み飛ばして設定漏れを再発させる、という問題を断つためのものです。v0.5.3 で `instruction doctor` という機械判定が入ったので、README を入口にして「まず 2 つの doctor を実行し、結果で判断する」導線を一番上に置き、詳細・背景・失敗時の解釈は FAQ / troubleshooting へ降ろすことで、入口の軽さと詳細の網羅を両立させます。

#10899 は、#10898 で project-local doc に追加した Codex pre-edit classification gate を、downstream の governed repo にも効かせるためのものです。Codex が Markdown / 調査メモ / ドラフトなどの repo 正本成果物を「コードではないから安全」と誤分類して直接編集する事故は、配布 preset を採用した repo でも同様に起こり得ます。distributed preset に同じ分類 gate を載せることで、commit / journal 要件を direct edit の免罪符にせず、autonomous lane か `codex_direct_edit` gate が成立した場合だけ direct edit に切り替える運用を、配布先でも既定にします。

#10796 は、tmux session 名を「workspace basename の ASCII sanitize」に依存させていたことが root cause でした。日本語など非 ASCII を含む basename は `____` のような低情報量名に潰れ、別 workspace の同名 session と衝突して、どの session がどの repo に属するか復元できなくなります。この状態では安全な `--target-repo` gate が false negative になり、operator が gate を外して送る誘惑が生じます。session identity の入力を、既に workspace-local の正本である Redmine project identifier(+ path hash fallback)に寄せ、bare `mozyo` / status / VS Code 入口を同じ導出に統一することで、「運用でカバー」ではなく入口そのものを修正しました。VS Code は `mozyo` を経由せず自前で session を立てるため、workspace-local settings writer を用意して機械的に同じ名前を渡せるようにし、user-global settings や credential には一切触れない制約を維持しています。

## v0.5.3 - 2026-06-01

v0.5.3 は、v0.5.2 以降に入った LLM instruction runtime / repo-local runtime config 周りの guardrail 強化をまとめた increment です。Claude / Codex が startup docs を読み飛ばしたり、別 workspace の default project を取り違えたりする運用事故を、docs の明確化と機械的な検査(`instruction doctor`)の両面で減らします。

### 変更点

- Claude Nagger の config skeleton に、Redmine-governed implementer 向けの session `startup_checkpoint` を追加しました。作業開始時に自分の role と現在の gate を宣言し、Implementation Done で止まらず Review Request gate と Codex への通知まで進み、通知の delivery result(または blocked reason)を durable record に残すことを促します。両 governed preset (`redmine-governed` / `redmine-rails-governed`) の skeleton と repo root の配置を同期しています。あくまで reminder であり、強制はしません(durable contract は引き続き central preset の `agent-workflow.md`)。(#10795)
- Codex workspace 向け bootstrap docs で、`.codex/config.toml` の Redmine MCP default project 設定を「optional な配置例」から **startup checkpoint** に格上げしました。起動時に設定の有無を確認し、無ければ operator に確認してから作成・更新し、restart / reload 後に `project_id` を省いた MCP call で default 解決を検証する、という手順を明記しています。(#10814)
- runtime config の配置先を明確化しました。`.codex/config.toml` と `.mcp.json` は home directory ではなく `<repo>/.codex/config.toml` / `<repo>/.mcp.json` という repo-root 配置を前提とすることを docs に明記しています。`.mcp.json` は repo-root 候補としつつ、対象 runtime が実際にその file を読むことを検証するまでは authoritative にしない deferral 制約を維持しています。いずれの repo-local config にも credential は置きません。(#10821)
- repo-local LLM runtime config を read-only で検査する opt-in command `mozyo-bridge instruction doctor --target . --profile redmine-codex`(`--json` 対応)を追加しました。`<repo>/.codex/config.toml` の存在・TOML parse・`[redmine]` の default_project / default_project_name / default_project_url・`[mcp_servers.redmine_epic_grid]` の url / `http_headers.X-Default-Project`、そして `X-Default-Project` が `[redmine].default_project` と一致するかを確認します。`.codex/config.toml` と(存在する場合の)`<repo>/.mcp.json` に credential 形状の値が無いことも検査します。`--target` 省略時は他 CLI と同じく `MOZYO_REPO` / repo marker で repo root を解決します。TOML parse は Python 3.11+ では標準 `tomllib`、3.10 では `tomli` に fallback します(`requires-python >=3.10` 維持)。Redmine MCP への network call・自動生成・自動修復・home config への書き込みは行わず、`.mcp.json` は欠落しても deferral として info 扱いにとどめ authoritative と断定しません。既存の `mozyo-bridge doctor` は変更せず、Asana / Claude-only / none preset を既定で fail させない opt-in slice です。(#10854)

### なぜ必要だったか

これらはいずれも、LLM が runtime guardrail / config を「読んだつもり」で読み飛ばしたり、別 workspace の事実を取り違えたりする運用事故を減らすための変更です。

#10795 は、Redmine-governed task で Claude が Implementation Done の journal を残しただけで「完了」と判断し、Review Request gate と Codex への review 通知を省略してしまう事故を防ぐためのものです。central workflow には `Implementation Done → Review Request → Codex 通知` が明記されているにもかかわらず、明示指示に通知が含まれないと省略され得たため、session 起動時の checkpoint として workflow 遵守を促します。

#10814 / #10821 は、Redmine default project の解決を agent の推測や home-directory 設定に委ねないためのものです。`.codex/config.toml` を単なる例ではなく起動時の確認対象(checkpoint)とし、設定の検証手順まで示すことで、未設定・未検証の default を黙って使う事故を防ぎます。さらに `.codex/config.toml` / `.mcp.json` を home ではなく repo root 配置に固定することで、ある workspace の default project が別 workspace へ漏れることを防ぎ、workspace-local な事実として隔離します。`.mcp.json` の authoritative 化を runtime 検証まで保留する制約は維持し、「実際には読まれていない config」を fact として扱う risk を避けています。

#10854 は、上記の docs 方針(repo-root の runtime config)を文章だけに頼らず機械的に検出できるようにするためのものです。LLM が startup docs を読み飛ばすと設定漏れに気付けないため、`instruction doctor` で `.codex/config.toml` の欠落・不整合・credential 混入を CI / agent が読める形で fail させます。既存 `doctor` を全 project で hard-fail させると Redmine/Codex 以外の workspace を巻き込むため、profile-aware な opt-in command として切り出し、`.mcp.json` の deferral も崩さない設計にしています。

## v0.5.2 - 2026-05-29

v0.5.2 は、v0.5.1 以降に入った LLM instruction runtime の健全化と、この repository 自身の governed scaffold 追従、handoff の体感改善をまとめた increment です。機能の大きな追加ではなく、runtime guardrail が想定どおり読まれ・配布物と repo が揃い・Claude TUI 環境で誤失敗しにくくなる、という運用品質の底上げが中心です。

### 変更点

- project Claude skill mirror (`.claude/skills/mozyo-bridge-agent/**`) を canonical (`skills/mozyo-bridge-agent/`) と同期し、`SKILL.md` の `description` と `references/` 一式 (`safety.md` / `workflow.md` / `project-map.md` / `release.md`) を最新化しました。あわせて、project skill を precedence で override していた deprecated な legacy global skill (`~/.claude/skills/mozyo-bridge-agent`) を整理し、`mozyo-bridge doctor` の `claude_skill` warning を解消しました (`doctor` が `claude_skill: ok` / `ok=true` を返す状態)。(#10744)
- 入口 router (`AGENTS.md` / `CLAUDE.md`) に、central preset を読む前の bootstrap として `mozyo-bridge rules home --resolved` の使い方を短く明記しました。committed docs には portable な `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` 表記を残し、runtime で実ファイルを読むときだけ `--resolved` 出力に `/rules/presets/<preset>/agent-workflow.md` を連結します。あわせて catalog resolver (`mozyo-bridge docs resolve`) の使用契約を central workflow / skill workflow 側に明文化し、governed preset version を `2026.05.29.1` に更新しました。(#10746)
- この repository 自身の root scaffold を、現行の `redmine-governed` preset (`2026.05.29.1`) に追従させました。`AGENTS.md` / `CLAUDE.md` に `rules home --resolved` の runtime bootstrap 文言を反映し、`.mozyo-bridge/scaffold.json` の preset metadata (`preset_version` / `generated_by` / router hashes) を再生成結果へ更新しました。mode は `central` のまま、project-local 追記は保持しています。(#10745)
- Redmine default project の startup 設定を docs 化しました。`<repo>/.mozyo-bridge/workspace-defaults.yaml` と `redmine-defaults.md` を正本に、agent が起動時に default project を解決する手順を README / `vibes/docs/logics/bootstrap.md` に明記し、verified / unverified default の扱いを揃えました。(#10753)
- `mozyo-bridge message` / `notify-*` / `handoff send` の `--landing-timeout` default を `5.0` から `8.0` 秒へ引き上げました。Claude / Codex TUI の描画遅延で marker 観測が間に合わず誤失敗するケースを減らすためで、marker を観測した時点で即座に進むため正常時の待ち時間は増えません。`read-lines` と `submit-delay` の default は据え置き、CLI help text に Claude TUI 環境向けの `--submit-delay 0.5` 推奨を併記しました。strict rail の rollback / fail-closed と queue-enter semantics は変更していません。(#10756)

### なぜ必要だったか

#10744 は、LLM instruction runtime の health check (`mozyo-bridge doctor`) が `claude_skill: warning` で `ok=false` を返していた問題を解消するためのものです。warning は、personal scope の legacy global skill が project skill を precedence で override していたことと、project mirror が canonical から drift していたことの複合でした。canonical を source of truth とした mirror 同期と legacy global の整理により、runtime guardrail が想定どおりの skill 内容で読まれる状態へ戻しています。

#10746 は、router が portable 表記で central preset を指していても、LLM が実ファイルを読むには rules home の resolved path が必要になる、という bootstrap の段差を埋めるためのものです。「committed docs に貼ってよい portable 表記」と「runtime でだけ使う resolved path」を入口で明確に分け、catalog resolver も「いつ・何のために使うか」を workflow / skill 側の実行契約として書くことで、agent が作業開始時に正本 docs へ迷わず辿り着けるようにしました。

#10745 は、#10746 で canonical / packaged 側に入れた bootstrap 文言と preset version が、この repository 自身の root router にはまだ反映されていなかった drift を解消するためのものです。mozyo_bridge は自分自身の governed preset を dogfood する repo であり、配布物と repo の入口が食い違ったままだと、ここで作業する agent が古い router を正本として読んでしまいます。root を現行 preset に揃えることで、配布する内容とこの repo で実際に読まれる内容を一致させました。

#10753 は、default project の解決を agent の推測に委ねず、検証済みの正本ファイルに寄せるためのものです。起動時にどの Redmine project を default とするかが docs 化されていないと、issue 作成や検索の宛先がぶれます。`workspace-defaults.yaml` を単一の入力とし、verified / unverified を明示することで、未検証の default を誤って使う事故を防ぎます。

#10756 は、Claude TUI の描画遅延環境で `mozyo-bridge message` の marker 観測が timeout し、実際には届いているのに誤って失敗扱いになるケースを減らすためのものです。polling rail は marker を観測した時点で即 return するため、default を 8.0 秒へ広げても正常時の体感 latency は増えません。`read-lines` を広げないのは、marker landing が内部で十分な capture window を別に使っており、読み取り範囲の拡大が主因に直接効かないためです。strict rail の安全性 (未観測時の rollback / fail-closed) は維持しています。

## v0.5.1 - 2026-05-29

v0.5.1 は、`redmine-governed` preset とこの project 自身の governed scaffold への移行、docs / guardrail catalog の実体化という governed-scaffold の中核をまとめて出すリリースです。あわせて、commit される router / docs に個人ホームパスを貼る事故を減らす `rules home` CLI、release 前の source tree hygiene を妨げていた fixture / docs 例の表現整理、cross-workspace handoff / autonomous lane / canonical renderer などの運用 guardrail も同梱しています。

### 変更点

- `redmine-governed` scaffold preset を追加しました。framework 非依存の `redmine` base に full governance package (gate schema / role split / Codex direct edit gate / docs catalog governance) を opt-in で被せる preset で、`redmine-rails-governed` (v0.5.0 の foundation) と並ぶ Redmine 向けの governed 入口です。
- この repository 自身の project scaffold を `redmine-governed` へ移行しました。`AGENTS.md` / `CLAUDE.md` を governed router として再生成し、`.mozyo-bridge/` 配下に governance artifact を配布して、mozyo-bridge を自身の governed preset で dogfood する状態にしました。
- mozyo_bridge の docs catalog と guardrail catalog を実体化しました。`.mozyo-bridge/docs/catalog.yaml` を documents / related_document_refs / file_conventions の正本とし、`mozyo-bridge docs validate / resolve / generate-file-conventions / audit-impact` で変更 path から紐づく guardrail / spec / convention を解決・検証できるようにしました。generated 物 (`file_conventions.generated.yaml`) は手編集禁止で generator 経由のみ更新します。
- Codex direct edit guardrail を強化し、startup decision flow docs を再設計しました。短い命令形だけでは Codex が gated surface (実装ファイル / ガードレール) を直接編集できないことを gate schema / invalid marker として明文化し、agent が作業開始時に正本へ辿り着く導線を整理しました。あわせて `mozyo-bridge-agent` skill を Redmine-governed 運用向けに更新し、既存 install / update path を docs に明記しました。
- `mozyo-bridge rules home` を追加しました。引数なしでは committed docs に貼れる portable な `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` 表記だけを出力し、`--resolved` を付けたときだけ `MOZYO_BRIDGE_HOME` と `~` を展開した machine-local の絶対パスを出力します。help text と README で、どちらを docs に貼ってよいか / debug 専用かを明示しています。既存の `doctor` / `rules status` の動作は変えていません。
- workspace-defaults renderer の credential-shape rejection テストと、その説明 docs (`vibes/docs/logics/workspace-defaults-renderer.md`) を、release tree hygiene scanner と両立する表現へ整理しました。テストは credential-shape 文字列を実行時に組み立てる形にし、docs は `<value>` placeholder 表記にすることで、tracked source に release-blocking な literal を残さずに「credential 代入形は拒否される」という検証・説明を維持しています。real secret detection は一切弱めていません (scanner / renderer のロジックは未変更、broad な allowlist も追加していません)。
- docs catalog に `coverage_roots` field を追加しました。`mozyo-bridge docs validate --check-file-coverage` は catalog 側 roots を読みつつ、CLI `--coverage-root` が指定された場合は CLI 側が優先されます。
- `scaffold status` の出力ラベルを実態に合わせて `tracked files:` に変更しました。manifest が router 以外の scaffolded artifact も追跡している現状を誤解させない表現にします。
- governed preset 配布の `.mozyo-bridge/tmux/agent-ui.conf` を host 側 tmux 設定 (`~/.tmux.conf` など) から安全に source できる新 subcommand `mozyo-bridge tmux-ui {install,uninstall,status}` を追加しました。**operator の既存 tmux 設定を丸ごと上書きせず**、`# >>> mozyo-bridge tmux-ui >>>` / `# <<< mozyo-bridge tmux-ui <<<` で囲んだ管理ブロックだけを安全に追加 / 更新 / 削除します。ブロック内部は `if-shell` で snippet の存在を確認してから `source-file` するため、repo を移動・削除しても tmux 起動が壊れません。同じ repo path への 2 回目の install は no-op で、`uninstall` は byte-for-byte で原状復元します。`--dry-run` / `--backup` / `--force` をサポートし、`status --json` は drift 時に exit 1 を返します。`doctor` の `tmux.artifact.host_wiring` セクションでも同じ状態 (`not-installed` / `installed` / `drift`) と推奨復旧コマンドを表示します。
- cross-workspace handoff を安全化しました。`mozyo-bridge agents list` (`--json` / `--session` / `--agent` 対応) で別 tmux session の window / pane / process / cwd / 推定 repo root / agent 種別を read-only に列挙できます。cross-session の `handoff send --to claude` は CLI で reject され、別 workspace へは対象 session の Codex window を gateway にする経路に限定しました。さらに gateway 送信では `--mode standard` / `--mode pending` の明示が必須で、default の `queue-enter` rail は cross-session target を拒否します。
- repo-local guardrail の育成を阻害しないよう、governed preset に **Repo-Local Guardrail Autonomous Lane** を追加しました。`vibes/docs/rules/**` / `vibes/docs/logics/**` / `vibes/docs/specs/**` / `.mozyo-bridge/docs/catalog.yaml` を Codex が事前 gate なしで編集でき、代わりに `codex_autonomous_edit` journal で監査可能性を担保します。distributed surface (`AGENTS.md` / `CLAUDE.md` / `.mozyo-bridge/rules/**` / skills / scaffold preset templates / `src/**` / `tests/**`) は lane に含めず、従来の gate を維持します。
- router / governed workflow 出力 (`AGENTS.md` / `CLAUDE.md` / preset の `agent-workflow.md`) を、単一の canonical source から条件分岐で描画する renderer に集約しました。同じ文言を複数ファイルへ手書き複製する drift を無くし、`scaffold canonical --check` (release drift gate にも同梱) が逸脱を検出します。
- `mozyo-bridge release check drift` を、canonical source 再描画チェックと plugin skill mirror (`sync_plugin_skill.sh --check`) の両 gate を束ねる 1 command に拡張しました。release 前に「canonical と生成物」「canonical skill と plugin mirror」双方の drift を 1 回で確認できます。plugin skill sync には `--check` mode を追加し、repo-root から実行できるよう drift recovery 文言も修正しました。
- Redmine default project 設定を生成する workspace-defaults renderer を追加しました。`<repo>/.mozyo-bridge/workspace-defaults.yaml` から `redmine-defaults.md` を render し、agent が default project を解決する際の正本にします。出力 kind は型付き (`KNOWN_OUTPUT_KINDS`) で、kind ↔ target suffix 互換 (`redmine_markdown` は `.md` / `.markdown` のみ) を load 時に検証し、`.toml` / `.json` / 拡張子なし target への Markdown 誤出力を防ぎます。credential-shape の key / value を含む YAML は load 時に die します。
- README / docs に埋め込んだ install / update command snippet を、occurrence 数まで pin する test-layer drift gate で固定しました。手順の literal が docs と実体でずれることを CI で検出します。

### なぜ必要だったか

`redmine-governed` preset とこの project の governed scaffold 移行、docs / guardrail catalog 実体化は、v0.5.1 の中核です。v0.5.0 までで governed preset の foundation (素材配布・package CLI 化・nagger/tmux artifacts) は整っていましたが、framework 非依存の `redmine` base へ full governance を被せる `redmine-governed` 入口と、それを使った catalog 駆動の docs 解決 (変更 path → guardrail / spec / convention) は未実体でした。mozyo_bridge 自身をその preset で dogfood し、catalog を正本として guardrail を運用する状態に移すことで、「どの rule が正本か」「generated 物を正本にしない」という governance posture を、この repo の日常運用で検証できるようにしています。Codex direct edit guardrail の強化と startup decision flow の再設計も、短い命令形が gated surface への直接編集許可と誤解される事故を構造的に止めるための同じ governance の一部です。

`rules home` を追加したのは、`AGENTS.md` / `CLAUDE.md` のような commit される router 文書に、operator の `/Users/<name>/...` のような解決済み絶対パスが紛れ込む事故を減らすためです。これまで mozyo-bridge home を確認する手段は `doctor` / `rules status` で、いずれも resolved な絶対パスを表示していました。docs に貼る portable 表記と、手元 debug 用の resolved path を 1 つの CLI で明確に分けることで、「確認のつもりで出した出力をそのまま docs に貼る」経路を断ちます。`--resolved` 出力は意図的に絶対パスを出すため、help text と README に「committed docs には貼らない」注意を併記しています。

credential-shape fixture / docs 例の整理は、release 直前の source tree hygiene gate (`release check tree`) が、実 secret ではない rejection テスト fixture と説明用 docs 例で blocker を出していた問題を解消するためです。これらは「credential 形の入力は拒否される」ことを保証・説明するために必要な文字列でしたが、tracked source に credential 代入形の literal が残ると release scanner が誤検知します。テスト側は実行時に文字列を組み立て、docs 側は placeholder 表記にすることで、検証・説明の意図を保ったまま scanner を green にしました。real secret detection を弱めない形 (scanner / renderer 本体は未変更) で両立させた点が要点です。

`coverage_roots` の構造化は、`--coverage-root` CLI option だけだと「どの roots が必須なのか」が project に残らなかった点を補うためです。catalog 側に書くことで、project が必要な root の集合を docs として宣言でき、`--coverage-root` を毎回打たなくても `mozyo-bridge docs validate --check-file-coverage` が project に合った挙動になります。`scaffold status` のラベル変更は、governed preset によって manifest 追跡対象が増えた現状に合わせた表記の修正です。

cross-workspace handoff の constraint・autonomous lane・canonical renderer・release drift helper・workspace-defaults renderer・install-command pinning は、いずれも「複数 agent / 複数 repo / 複数 surface をまたいで運用するときに、正本がぶれたり監査経路が壊れたりする」事故を減らすための実行時 / release 時 guardrail です。cross-workspace の通知を Codex gateway + 明示 mode に限定したのは、別 workspace の Claude pane へ直接打ち込むと、その workspace の audit 境界を飛び越えてしまうためです。autonomous lane は逆に、repo-local guardrail の育成を毎回 gate で止めない代わりに `codex_autonomous_edit` journal で記録を残す設計で、growth と監査可能性を両立させます。canonical renderer と release drift helper は、同じ文言を複数生成物へ手書き複製する drift を構造的に無くし、release 前に 1 command で検出できるようにするためのものです。workspace-defaults renderer は、default project 解決を agent の推測ではなく検証済みの正本ファイルに寄せ、型付き kind と suffix 互換チェックで誤出力を防ぎます。install-command pinning は、docs の手順 literal が実体とずれて利用者が古い経路を踏む事故を CI で止めるためです。

`tmux-ui install` を新設したのは、governed preset が配布する `agent-ui.conf` を host 側 tmux 設定に組み込む手順を operator 任せにすると、抜けや手書き drift、`source-file` 行の追記事故 (snippet が無いと tmux 起動が壊れる) が起きやすかったためです。phase A で repo-local artifact 配布と doctor 表示は完了していましたが、host 設定への組み込みは「手で `~/.tmux.conf` を編集する」前提のままで、複数 repo を横断する operator にとって reproducible でない手順が残っていました。今回の subcommand は managed block 方式を採用し、`if-shell` で snippet 不在時に no-op になる安全策をブロック内部に組み込み、operator の既存設定は決して触れないことを byte-for-byte の round-trip test で担保しています。`uninstall` / `status` / `--dry-run` / `--backup` / `--force` を同時に揃えたのは、host 設定という最も触りたくない領域を変更する以上、変更前確認・原状復帰・drift の検出を operator 側で自由に組み合わせられる必要があったからです。

## v0.5.0 - 2026-05-21

v0.5.0 は、v0.4.0 以降に整備した governed scaffold の foundation をまとめて出すリリースです。実運用で full governance package を即座に被せられる governed preset と、その配布物 (Claude Nagger / tmux UI artifacts、docs catalog tooling の package CLI 化) を中心に、薄い router から full governance への移行経路を整えました。

### 変更点

- 新しい scaffold preset `redmine-rails-governed` を追加しました。`redmine-rails` を継承しつつ、full guardrail governance package を opt-in で被せられます。
- `scaffold apply redmine-rails-governed` は target repo の `.mozyo-bridge/` 配下に rule files と docs catalog skeleton (`catalog.yaml.example`) を配布します。docs catalog tooling (validator / resolver / generator / impact checker) は target repo に vendor copy せず、`mozyo-bridge` package に同梱された `mozyo-bridge docs ...` CLI として提供されます。配布された artifact は scaffold manifest に登録され、`scaffold status` が drift を検出します。
- governed preset に Claude Nagger 設定 skeleton (`.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example` と `.gitignore`) と、tmux agent window 用の UI 補助 snippet (`.mozyo-bridge/tmux/agent-ui.conf`) を標準で同梱しました。両者は default-on で `scaffold apply` 時に target repo に配布されます。
- 標準導入したくない project 向けに `scaffold apply --skip-nagger` と `--skip-tmux-ui` opt-out flag を追加しました。スキップした category は manifest にも記録されないため、`scaffold status` は引き続き clean を返します。
- `mozyo-bridge doctor` に `claude_nagger` セクションを追加し、`tmux` セクションには tmux UI snippet の設置状態 (`artifact:` 行) を表示するようにしました。default-on の guardrail がどこまで届いているかを 1 command で確認できます。
- 旧 governed scaffold が target repo に vendor copy していた `.mozyo-bridge/tools/*.py` を廃止し、docs catalog tooling を mozyo-bridge package 側の CLI として提供するように移しました。`mozyo-bridge docs validate / resolve / generate-file-conventions / audit-impact` で同等の運用が可能です。再 apply 時は scaffold の outgoing reconcile が旧 vendor copy を `--backup` / `--force` 経由で安全に除去します。
- governed preset の gate schema、role split、Codex direct edit gate、完了条件を preset の `agent-workflow.md` に統合しました。別配布していた `.mozyo-bridge/rules/development_flow.md` は廃止し、再 apply 時は manifest 管理下の旧 file を `--backup` / `--force` 経由で安全に除去します。

### なぜ必要だったか

`redmine-rails` preset は薄い router を維持するために、強い gate 文言や docs catalog tooling を project-local layer に委ねていました。実運用で full governance を即時に被せたい project にはこの分離が手数になっていたため、opt-in の governed preset として配布できる形にしました。実運用プロジェクトで実績のある guardrail から、固有業務ドメインや固定 path を取り除き、汎用 Rails+Redmine project に被せられる素材だけを残しています。

Claude Nagger と tmux UI snippet は、単なる便利機能ではなく、agent が別 window / 別 role / 別 context で誤動作する事故を減らす実行時 guardrail の一部です。Dev Container や ephemeral home の環境では home 側に依存した後付け手順が抜けやすく、project ごとに導入状況がバラつく問題がありました。default-on で repo-local scaffold に同梱し、`doctor` で導入状態を 1 command で見える化することで、guardrail の入り忘れを減らすのが今回の意図です。既存の host 側設定 (例えば `~/.tmux.conf`) は **触らず**、scaffold は target repo にのみ artifact を置きます。host 側への組み込みは operator が `source-file <repo>/.mozyo-bridge/tmux/agent-ui.conf` を `~/.tmux.conf` に追記するなど明示的に行う前提です。

docs catalog tooling を target repo へ vendor copy する形は、project 固有コードなのか mozyo-bridge runtime なのかが曖昧で、配布・upgrade・drift 管理がいずれも重くなっていました。今回 tooling 本体を mozyo-bridge package に同梱し、`mozyo-bridge docs ...` CLI に寄せることで、target repo には catalog 等の data だけが残り、runtime tool の version は `mozyo-bridge` の install と一致するようになります。既存環境からの移行は `scaffold apply --backup` (または `--force`) で旧 `.mozyo-bridge/tools/*.py` が outgoing reconcile されるため、operator は 1 command で切り替えられます。

`development_flow.md` を別 file として配る設計は、agent が読むべき正本を増やし、`agent-workflow.md` との責務境界を人間にも LLM にも分かりにくくしていました。governed preset では AGENTS.md / CLAUDE.md がまず `agent-workflow.md` を読むため、実行契約もそこに統合し、入口から正本までの経路を単純にしました。

## v0.4.0 - 2026-05-20

v0.4.0 は、Dev Container などの環境で guardrail を失いにくくし、複数 agent window を少し見分けやすくするリリースです。

### 変更点

- `rules install` と `scaffold apply` で、repo-local な guardrail rules store を使えるようにしました。
- repo-local mode の使い方を README と scaffold rules documentation に追加しました。
- tmux の `claude` / `codex` window に、控えめな status color を付けるようにしました。
- `claude` / `codex` の window 名は変更せず、resolver / handoff routing の互換性を維持しています。
- release note を日本語で整備しました。

### なぜ必要だったか

Dev Container や Codespace では、home directory が永続化されないことがあります。その場合、user-global な guardrail store だけに依存すると、container rebuild 後に agent が必要な rules を読めなくなる可能性があります。

repo-local mode は、必要な guardrail を対象 repo の中に置けるようにするためのものです。これにより、workspace を開いた agent が同じ repo 内の rules を参照でき、環境差による立ち上がり失敗を減らせます。

tmux の status color は、運用上の小さな混乱を減らすための変更です。派手な見た目にすることが目的ではなく、`claude` / `codex` / その他 window を一目で少し区別しやすくするために入れました。

## v0.3.0 - 2026-05-19

v0.3.0 はガードレール強化のリリースです。Claude / Codex / Asana / Redmine / Redmine Rails をまたいだ作業引き継ぎと project scaffold を、より安全に繰り返せるようにしました。

### 変更点

- v0.2.1 alpha 系で検証した内容を安定版としてまとめました。
- scaffold の操作を `scaffold diff`、`scaffold apply`、`scaffold status` に整理しました。
- scaffold 済みの `AGENTS.md` / `CLAUDE.md` を再生成するとき、project-local な追記を保持できるようにしました。
- Asana / Redmine / Redmine Rails の preset 境界を明確にしました。
- `queue-enter`、retry、通知成功が意味する範囲について handoff documentation を強化しました。

### なぜ必要だったか

mozyo-bridge は、tmux pane へ通知する小さな道具から、複数 agent の作業ルールを配布する道具へ広がってきました。この段階で問題になるのは、単に command が足りないことではありません。

どの rule が正本なのか、どの handoff 経路が安全なのか、scaffold の再生成で project 固有の知識が消えないか。そうした曖昧さが運用上のリスクになります。

v0.3.0 では、その不確実さを減らすために scaffold と handoff の手順を整理しました。

## v0.2.1 alpha series - 2026-05-17 to 2026-05-19

v0.2.1 alpha 系は、v0.3.0 に入れる運用変更を安定化するための検証期間です。

### 変更点

- `v0.2.1a1`: handoff primitive、retry guidance、ACK boundary、skill distribution rules を文書化しました。
- `v0.2.1a2`: Redmine Rails scaffold guardrails を追加し、TestPyPI dispatch の入力を release workflow に合わせました。
- `v0.2.1a3`: scaffold を `apply` / `diff` 中心に再設計し、Redmine Rails の project-local layer を明確にしました。
- `v0.2.1a4`: Redmine の review と close approval を分離しました。
- `v0.2.1a5`: stable release 前に残っていた `queue-enter` documentation gap を解消しました。

### なぜ必要だったか

この alpha 系の目的は、機能をむやみに増やすことではありません。handoff をどう送るか、scaffold をどう更新するか、review と close approval をどう分けるかという曖昧さを減らすことでした。

安定版として出す前に、運用ルールの表現と CLI の動きを揃える必要がありました。

## v0.2.0 - 2026-05-14

v0.2.0 では、現在の handoff model の土台を入れました。

### 変更点

- `handoff send` / `handoff reply` を高レベルの通知 primitive として追加しました。
- handoff の結果を後から追える delivery record を追加しました。
- tmux send safety contract を定義しました。
- relaxed `queue-enter` rail と deterministic preflight checks を追加しました。
- release helper commands と release verification docs を追加しました。
- LLM-first bootstrap guide を追加しました。

### なぜ必要だったか

以前は、通知を低レベル command の組み合わせで送ることができました。柔軟ではありますが、operator や agent ごとに挙動がぶれやすい状態でもありました。

v0.2.0 では、handoff を単一の標準経路に寄せました。これにより、失敗したときの説明や監査がしやすくなりました。

## v0.1.13 - 2026-05-13

v0.1.13 では、tmux の agent identity を window model へ移行しました。

### 変更点

- pane label を runtime identity として使う設計から移行しました。
- window-only tmux model に合わせて scaffold docs を更新しました。
- legacy pane-split tmux commands を廃止しました。
- 最初の handoff / reply notification primitive を追加しました。

### なぜ必要だったか

pane label は、実際の tmux session では曖昧になりやすいものでした。agent ごとに `claude` / `codex` window を持つ方が、resolver にも operator にも分かりやすくなります。

## v0.1.12 - 2026-05-13

### 変更点

- tmux の pane capture で message marker が折り返された場合にも扱えるようにしました。

### なぜ必要だったか

長い通知は tmux 上で折り返されます。marker detection が折り返しで失敗すると、通知が期待した場所に届いたかを誤判定する可能性があります。この release では、その経路の信頼性を上げました。

## v0.1.11 - 2026-05-13

### 変更点

- scaffold 済み handoff guidance の表現を調整しました。
- bare `mozyo` startup branch の regression tests を追加しました。

### なぜ必要だったか

bare `mozyo` が repo session を始める標準導線になったため、docs と tests もその導線に合わせる必要がありました。

## v0.1.10 - 2026-05-13

### 変更点

- bare `mozyo` を標準の tmux entrypoint にしました。

### なぜ必要だったか

Claude / Codex の pair を始めるために、複数の setup command を覚える必要がある状態は扱いにくいものでした。`mozyo` だけで repo-scoped session と window を用意できるようにしました。

## v0.1.9 - 2026-05-12

### 変更点

- audit-owned commit workflow guidance を追加しました。
- `mozyo-bridge-agent` plugin を package 化しました。
- `doctor` が Claude plugin-managed skill install を認識できるようにしました。
- default config file が無い場合の tmux startup behavior を改善しました。
- repo-aware `open-here` behavior を追加しました。

### なぜ必要だったか

導入直後や日常運用で、CLI / rules / skills / scaffold / tmux が健康な状態かを個別に確認するのは手間がかかります。`doctor` と plugin-aware な診断により、setup 状態を確認しやすくしました。

## v0.1.7 and v0.1.8 - 2026-05-11

### 変更点

- TestPyPI turnkey acceptance のために CLI version を調整しました。
- `doctor` environment readiness diagnosis を追加しました。
- fresh tester / turnkey acceptance flow を文書化しました。
- tmux handoff message を default で submit するようにしました。
- scaffold rule path を portable にしました。

### なぜ必要だったか

作者以外の環境で install / acceptance smoke を行う準備が必要でした。install し、環境を確認し、target project に scaffold し、結果を検証する流れを再現可能にするための変更です。

## v0.1.5 and v0.1.6 - 2026-05-10

### 変更点

- Asana scaffold preset に role-boundary guardrails を追加しました。
- workflow verification における Codex role boundary を明確にしました。
- `--version` flag を追加しました。
- scaffold rules の default target を current working directory にしました。
- 命令形の文言だけで Codex が policy / skill / rule files を直接編集できる、という誤解を防ぐ guard を追加しました。

### なぜ必要だったか

user request、durable task state、agent authority を分ける必要がありました。軽い依頼文が、ルール変更の直接許可として扱われると危険だからです。

## v0.1.4 - 2026-05-10

### 変更点

- ticket-system scaffold rules を追加しました。
- scaffold home path handling を修正しました。
- 日本語 scaffold routers と dogfood preset を追加しました。
- release / handoff rules を文書化しました。
- Asana preset escalation policy を追加しました。

### なぜ必要だったか

mozyo-bridge を 1 repository の中だけで使う段階から、複数 project に薄い router を配布する段階へ進める必要がありました。central rules を一箇所に置きつつ、target project には入口だけを置くための基礎です。

## v0.1.3 and earlier - 2026-05-09 to 2026-05-10

初期 release では、公開 package と agent 向け documentation の土台を作りました。

### 変更点

- PyPI / TestPyPI publishing workflows を準備しました。
- Asana-driven agent documentation router を追加しました。
- Claude / Codex 用の `mozyo-bridge` skills を追加しました。
- public release 前に documentation を sanitize しました。
- public GitHub install path と Claude skill usage path を追加しました。
- user / agent documentation を整理しました。

### なぜ必要だったか

最初の目的は配布可能にすることでした。より高度な scaffold や handoff を積み上げる前に、install でき、説明でき、安全に公開できる状態にする必要がありました。
