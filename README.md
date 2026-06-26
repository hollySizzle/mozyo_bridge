# mozyo-bridge

`mozyo-bridge` は ClaudeCode / Codex の tmux pane に Redmine journal id を通知するための小さな bridge です。

正本は Redmine です。`mozyo-bridge` は通知 transport であり、レビュー依頼・監査結果・完了判断の正本にはなりません。

`mozyo-bridge` is a small CLI that sends Redmine-gated notifications to Claude Code / Codex tmux panes.
It is only a notification transport. Redmine remains the source of truth for review requests, audit results, and completion decisions.

## Quick Start

This README is the entrypoint for install and bootstrap. Run the steps below
first; follow the links into the detailed docs only when a step fails or you
need the full stage order.

> 人間向けのリリースノートは [`RELEASE_NOTES.md`](RELEASE_NOTES.md) にあります。

1. **Install the CLI** and confirm `tmux` is on `PATH`:

   ```bash
   pipx install mozyo-bridge
   mozyo-bridge --version
   ```

   Alternative install path:

   ```bash
   python3 -m pip install mozyo-bridge
   python3 -m mozyo_bridge --help
   ```

2. **Run the two health checks, in order**, from your project root:

   ```bash
   # Toolchain health: CLI, central rules, agent skills, scaffold, tmux.
   mozyo-bridge doctor --target .

   # Repo-local LLM runtime config health (Redmine/Codex workspaces).
   mozyo-bridge runtime-config check --target . --profile redmine-codex
   ```

3. **Read the result**:
   - `doctor` exits non-zero on a toolchain gap (missing rules, skill, scaffold
     drift, tmux). It prints the next command for each finding.
   - `runtime-config check` is the source of truth for whether a Redmine/Codex
     workspace's repo-root runtime config is correct. It is read-only: it never
     creates, fixes, or writes config. On a failure, see
     `runtime-config check` failures below and the FAQ it links to.

When you want the *ordered recovery procedure* rather than a raw diagnosis, run
the read-only runbook:

```bash
mozyo-bridge doctor instruction --target .
```

It turns the current `doctor` diagnostics into a numbered fix sequence —
central rules, agent skills (primary plugin path vs legacy curl fallback),
scaffold drift (review-before-restore), runtime config, then a final
verification — and prints the CLI taxonomy migration notes.

For first-time install on a clean machine, or to follow the full
install → rules → skill → scaffold → doctor stage order end-to-end, use
`vibes/docs/logics/bootstrap.md` as the detailed stage-order / troubleshooting
reference (it is no longer the first thing you read; this Quick Start is).

> **CLI taxonomy migration (breaking, deprecated alias for one minor):** the old
> `mozyo-bridge instruction doctor` / `instruction install` were renamed to
> `mozyo-bridge runtime-config check` / `runtime-config install`. The old names
> still run but print a deprecation warning to stderr and are a removal
> candidate next minor. `doctor instruction` is the *new* read-only recovery
> runbook (distinct from the renamed `runtime-config check`).

### `doctor` vs `runtime-config check` vs `doctor instruction`

- `mozyo-bridge doctor --target .` — toolchain readiness: CLI, central rules,
  agent skills, scaffold manifest, tmux. Run this first.
- `mozyo-bridge runtime-config check --target . --profile redmine-codex` —
  repo-local LLM runtime config: `<repo>/.codex/config.toml` Redmine default
  project, the `redmine_epic_grid` MCP header, and credential-shape hygiene of
  `.codex/config.toml` / `.mcp.json`. This is the machine check that the
  Redmine-Codex startup config actually exists and is consistent, so an agent
  cannot silently skip it by skimming the docs.
- `mozyo-bridge doctor instruction --target .` — read-only recovery runbook that
  orders the fixes for whatever `doctor` found, distinguishing primary from
  legacy-fallback commands. It only reads; it never installs or writes.

### `runtime-config check` failures

`runtime-config check` is read-only and never autofixes. The common failures and
the operator action they require:

- **`<repo>/.codex/config.toml` is missing** — the workspace has no repo-root
  Redmine default. If `<repo>/.mozyo-bridge/project-defaults.yaml` (the legacy
  `workspace-defaults.yaml` name still reads) already
  carries a **verified** default project, generate the config from it with
  `mozyo-bridge runtime-config install --profile redmine-codex --target . --write`
  (see `runtime-config install` below). Otherwise ask the operator before creating
  it; do not put it in a home config. See the FAQ in
  `vibes/docs/logics/bootstrap.md`.
- **`X-Default-Project` mismatch** — the MCP header and `[redmine].default_project`
  disagree. One of them is wrong; an operator must reconcile them.
- **`.mcp.json` present/absent** — reported as `info`, never a failure on its
  own. `.mcp.json` stays non-authoritative until a runtime is verified to read
  the repo-root file (deferral).
- **credential-shape value** — a token/key/secret was found in a repo-local
  config. Remove it; credentials belong in user-level config or a secret store,
  never in `<repo>/.codex/config.toml` or `<repo>/.mcp.json`.

Detailed cause/fix for each, plus the home-config-prohibition rationale and what
an agent may auto-fix vs must confirm with an operator, is in
`vibes/docs/logics/bootstrap.md` (`Stage 7 — Failure recovery and common pitfalls`).

### `runtime-config install` (project-defaults → runtime config → check)

`runtime-config check` only checks; `mozyo-bridge runtime-config install --profile
redmine-codex --target .` closes the gap by projecting the **verified** Redmine
default project from the single source of truth
(`<repo>/.mozyo-bridge/project-defaults.yaml`; the legacy
`workspace-defaults.yaml` name still reads as a fallback) into the repo-root
`<repo>/.codex/config.toml`. The flow is: edit `project-defaults.yaml` →
`mozyo-bridge workspace-defaults --check` clean → `runtime-config install --write`
→ `runtime-config check` green.

```bash
mozyo-bridge runtime-config install --profile redmine-codex --target .            # dry-run
mozyo-bridge runtime-config install --profile redmine-codex --target . --write    # apply
```

- Source of truth stays `project-defaults.yaml` (legacy `workspace-defaults.yaml`
  still reads); install never invents values.
- Writes **only** `<repo>/.codex/config.toml` (the `[redmine]` and
  `[mcp_servers.redmine_epic_grid]` tables). Home config is never read or
  written, and no credentials are generated.
- Default is a dry-run; `--write` applies. An **unverified** default project is
  refused (verify it first) — generating runtime config from an unverified
  default is exactly what the doctor guards against.
- An existing config is preserved: when the managed tables are absent they are
  appended (other keys untouched). When they already exist and disagree, install
  fails and asks you to resolve it, unless `--force` regenerates just those
  tables. Invalid TOML is never clobbered.
- `.mcp.json` stays deferred; install does not generate it.

Use the full command in docs and durable task records:

```bash
mozyo-bridge <command>
```

The short alias is available for local interactive use:

```bash
mozyo <command>
```

### Bootstrap ownership and local secrets

For bootstrap and day-1 setup, treat this repository and any shared-drive copy of
it as the **distribution source**, not as the place where machine-local secrets
live.

Keep in the repo / shared-drive copy:

- portable config and bootstrap docs
- scaffold/rules sources
- install / doctor / bootstrap helper scripts

Keep only on each local machine:

- `auth.json`
- `~/.mcp-auth/`
- Asana OAuth tokens / refresh state
- client secrets or any other user-specific credentials

Do not commit or sync the local-secret files above back into the repository or a
shared-drive bootstrap bundle.

### Asana OAuth note

Asana OAuth is only required on machines that will actually use the Asana
connector / MCP-backed Asana workflow. A plain CLI install, rules install,
scaffold, or `doctor` run does not by itself provision Asana credentials.

When a bootstrap or day-2 workflow reaches an Asana-backed step, the
user/operator must complete the OAuth/login flow in that local environment and
leave the resulting credentials on that machine only. `mozyo-bridge` docs may
point at the Asana workflow, but the OAuth state is not part of the portable
bootstrap payload.

### Daily entrypoint: bare `mozyo`

The fastest way to start a Claude / Codex pair in a repo is to run `mozyo` with no subcommand:

```bash
cd /path/to/your-repo
mozyo
```

これは以下を一括で行います。

- repo root を解決 (`--repo` → `MOZYO_REPO` → `.git` / `.tmux.conf` / `pyproject.toml` を遡る)
- session 名を `mozyo-bridge session name` と同じ規則で解決して、無ければ作る。`mozyo-bridge workspace register` 済みの workspace では **登録済み canonical session 名** を再利用し (Redmine #11429)、未登録なら従来どおり path から導出 (`<repo>/.mozyo-bridge/project-defaults.yaml` の Redmine identifier 優先、旧名 `workspace-defaults.yaml` も fallback で読む、無ければ `mozyo-<basename-slug>-<hash>`)。`--session NAME` を明示した場合はそれを優先 (Redmine #10796)
- 1 つの repo-scoped session の中に `claude` window と `codex` window を ensure (window 別に分離)
- `claude` window を default にしてから attach

Window 分離なので、ある時点で画面に出るのは 1 agent の window だけです。tmux は同じ client 内で複数 window を同時表示する仕組みではないため、agent 同士の切り替えは `prefix + n` / `prefix + p` などの通常の window 操作で行います。

attach せずに session / window だけ用意したい場合:

```bash
mozyo --no-attach
```

iTerm2 control mode で attach したい場合 (iTerm2 が tmux window/pane を native 管理):

```bash
mozyo --cc        # ensure 後 `tmux -CC attach -t <session>` を exec
```

`--cc` は ensure 系挙動 (session 導出 / window 構成 / env 注入 / workspace identity) を変えず、attach 形だけを `tmux -CC attach` に置き換えます。`--no-attach` と `--json` は `--cc` より優先し、どちらも attach せず ensure のみで、表示 / JSON の attach コマンドが `-CC` 版を指すだけになります (`--json` payload に `control_mode` を追加)。control mode での OS window title 反映は iTerm2 側挙動に依存し実機確認が必要です (Redmine #11729)。

### Cockpit layout (`mozyo layout apply cockpit`)

複数 workspace を横並びの列にし、各列の中で Codex を上・Claude を下に縦分割した cockpit ビューを組みます (Redmine #11788)。既定比率は Codex 70% / Claude 30%。

```bash
mozyo layout apply cockpit                 # active workspace を列に並べて attach
mozyo layout apply cockpit --ratio 60      # Codex 60% / Claude 40%
mozyo layout apply cockpit --repo /a --repo /b   # 列にする workspace を明示
mozyo layout apply cockpit --dry-run       # 生成する tmux コマンドを表示 (実行しない)
mozyo layout apply cockpit --json          # plan を JSON で出力 (実行・attach しない)
mozyo layout apply cockpit --cc            # 構築後 iTerm2 control mode で attach
```

```text
workspace A          workspace B
+---------------+    +---------------+
| Codex   70%   |    | Codex   70%   |
+---------------+    +---------------+
| Claude  30%   |    | Claude  30%   |
+---------------+    +---------------+
```

責務分担:

- **tmux state が layout の正本**です。cockpit は `mozyo-cockpit` session に列 (workspace) × 縦分割 (Codex/Claude) の pane を組み、各 pane の title に `workspace · role [· anchor]` を記録します。
- **`--cc` は表示面**で、組み上がった tmux layout を iTerm2 control mode で見るための attach option です。`-CC` 自体は layout semantics を持ちません (#11729 と同じ責務)。
- **active workspace のみ**を召喚します。`--repo` 明示が無ければ live session inventory から codex/claude pane を持つ workspace を列にします。全 unit 常時表示は非目標 (fleet overview は別)。
- **reuse 優先**: 既に `mozyo-cockpit` session があれば重複 pane を作らず、その session に focus / attach します。`--session NAME` で別名にできます。
- `--dry-run` / `--json` は tmux を mutate せず plan を出します (`--repo` 明示が無いときは active workspace 判定のため inventory を read-only で読みます)。unit test では実機 tmux / iTerm2 なしに layout コマンド生成を検証できます。
- 既存の bare `mozyo` / `mozyo --cc` / `--json` / `--no-attach` 契約は不変です。

### 日常入口: `mozyo cockpit` (project を cockpit に追加 / focus)

各 project に `cd` して `mozyo cockpit` を叩くだけで、その workspace を共有 cockpit (`mozyo-cockpit`) に追加できます (Redmine #11803)。専用 launcher は不要で、`mozyo --cc` の意味は変わりません。

```bash
cd /path/to/project-A && mozyo cockpit   # cockpit が無ければ作成 + iTerm2 control mode で attach
cd /path/to/project-B && mozyo cockpit   # 既存 cockpit に project-B の列を追加 (新規 iTerm window は増やさない)
cd /path/to/project-A && mozyo cockpit   # 既に居る workspace は再追加せず、その列に focus
mozyo cockpit --dry-run    # 実行する tmux コマンドと action (create/append/focus) を表示
mozyo cockpit --json       # plan + action を JSON 出力 (tmux を mutate しない / read-only)
```

挙動:

- **create**: `mozyo-cockpit` が無ければ、現在の workspace を 1 列目として作成し `tmux -CC attach` します。`--no-attach` で attach を抑制できます。
- **append**: 既存 cockpit があり、その workspace がまだ無ければ、**新規 iTerm window を増やさず**横 column として追加します (control mode の既存 window にライブ反映)。
- **focus**: 同一 workspace が既に cockpit 内にあれば、重複 column を作らずその pane に focus します。
- workspace identity は pane title 文字列ではなく **tmux user option** (`@mozyo_workspace_id` / `@mozyo_agent_role`) に記録するため、append/focus の重複判定が title 表記に依存しません。
- layout step が失敗したら fail-closed します。create 失敗時は partial session を kill-session で後始末します。append 失敗時は、他 workspace の pane が同居するため cockpit session 自体は kill せず、その append で作成した新規 pane だけを kill-pane で後始末します (orphan pane を残しません)。
- `--dry-run` / `--json` は **read-only / non-mutating** です。create/append/focus 判定のため cockpit state を読みますが tmux を mutate せず、stale / 未識別の cockpit でも abort しません (append が anchor を取れない場合は `blocked` として理由を表示します)。
- `--cc` 起動 (#11729) と `mozyo layout apply cockpit` (#11788) の上に載る薄い入口で、それらの契約は不変です。

session 名が同じでも repo root の下に pane が 1 つも無い場合 (= 別 project の session が同名で居る場合) は、誤 attach を避けるためにエラーで止まります。明示的に session 名を分離する場合は `mozyo --session NAME` で session 名を上書きするか、bare `mozyo --repo /path/to/another` で別 repo root を指定してください。

**移行メモ**: 以前の bare `mozyo` は session 名を repo basename にしていました。導出名へ移行したため、古い basename session が残っている repo では bare `mozyo` 実行時に notice を出します。古い session に入りたい場合は `mozyo --session <旧basename>` (または `tmux attach -t <旧basename>`)、空になったら `tmux kill-session -t <旧basename>` で片付けてください。`--target-repo` gate と `init` の同名 window fail-closed は従来どおりです。

`open-here` / `tmux-ui-open` / `tmux-ui-setup` / `tmux-ui-ensure-pair` / `tmux-ui-ensure` / `tmux-ui-spawn` の pane-split 系 subcommand は廃止されました。標準導線は bare `mozyo` (1 repo = 1 session, 1 agent = 1 window) です。既存の標準 tmux pane や VS Code `tmux-integrated` pane を agent target にしたい場合は、その pane の中で `mozyo-bridge init <agent>` を実行して window 名を `<agent>` に rename してください。

### Workspace registry (`mozyo-bridge workspace register`)

session 名の導出入力 (workspace-defaults の identifier や path 自体) が後から変わると、path からの再導出だけでは session identity が動いてしまいます。home registry は **初回に決まった identity を正本として固定** します (Redmine #11429)。

```bash
cd /path/to/your-repo
mozyo-bridge workspace register            # 登録 (idempotent)
mozyo-bridge workspace list                # home registry の一覧
mozyo-bridge workspace inspect --repo .    # registry / anchor / 導出 fallback の突き合わせ
```

- 正本は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/registry.sqlite`。workspace id / canonical path / readable name / canonical session 名 / preset version を管理します。live な tmux window / pane / process state は registry に入れません (`last_seen` のみ cache として分離保持)。
- 登録時に **workspace-local anchor** (`<repo>/.mozyo-bridge/workspace-anchor.json`、旧名 `workspace.json` も fallback で読む) を書きます。home registry が消えた環境 (ephemeral home / 再 install) でも、workspace 内で `workspace register` を再実行すれば anchor から同じ workspace id と canonical session 名が復元されます。
- 登録後は `mozyo-bridge session name` / bare `mozyo` / `status` / smart `init` が **登録済み canonical session 名を優先** します。path からの導出は初回登録時と未登録 workspace の fallback に限定されます。未登録 workspace の挙動は従来と完全互換です。
- 読み取り系 (`session name` / `list` / `inspect` / bare `mozyo` の session 解決) は registry を作らず書き換えません。書き込みは `workspace register` だけです。
- `--name` で readable name (日本語可) を上書きできます。非 git workspace も `--repo` 明示で登録できます。

### Workspace 横断 session inventory (`mozyo-bridge session list`)

複数 workspace で起動中の mozyo session / agent pane を一覧します (Redmine #11422)。operator の俯瞰と外部 UI の発見用で、特定 VS Code extension の専用 backend ではありません。

```bash
mozyo-bridge session list           # 1 pane = 1 行のテーブル
mozyo-bridge session list --json    # 機械可読 snapshot (schema_version / source / stale / panes[])
```

- **正本は tmux runtime** です。実行のたびに live な session / window / pane / process / cwd を収集し、各 pane の repo root を workspace identity (registry → anchor → 導出、Unicode 正規化差を吸収) に解決します。
- 収集結果は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/inventory.sqlite` に **cache として** 保存されます。tmux が使えない環境では最後の snapshot を `stale` 明示付きで返します。cache が消えても次の実行で再構築されるため復元手順は不要です。
- 同一 pane が tmux session group で複数 session に属する場合も **1 行に畳まれます** (同一性キーは `pane_id`、Redmine #11628)。所属 session は `views` 配列で保持し、workspace の canonical session と一致する view を正準として表示します。
- 低レベルの pane 列挙が必要な場合は従来どおり `mozyo-bridge agents list` を使ってください。

### OTel イベントストア (`mozyo-bridge otel`)

エージェント CLI (Claude Code 等) が発行する OpenTelemetry を localhost で受けて SQLite に貯め、「ユニットが動いているか」を判定します (Redmine #11639 段階1)。

```bash
mozyo-bridge otel serve                 # OTLP/HTTP 受け口 (127.0.0.1:4318) を foreground 起動
mozyo-bridge otel status [--json]       # ストア件数 + 受け口疎通
mozyo-bridge otel activity [--json]     # source ごとの active / idle 判定
mozyo-bridge otel events --json         # 受信イベントの tail (debug / 深度実測)
```

- **best-effort** です。受け口停止中のイベントは失われ、ストアは正本になりません。`idle` / `unknown` は死亡を意味せず、生死は `agents list` / `session list` (tmux 層) で確認します。
- **プロンプト本文は保存しません**。usage / イベント種別 / 最小 metadata のみ allowlist で保存し、本文系 key は deny 優先で落とします。
- **env 注入は自動です**: bare `mozyo` / `mozyo-bridge init` が agent window を起動する際、OTel env (endpoint / http-json / exporters) と join 用 `OTEL_RESOURCE_ATTRIBUTES` (`mozyo.session` / `mozyo.agent` / `mozyo.workspace_id`) を launch command に注入します。注入前に起動した既存 agent は `unknown` のままで、再起動 (`mozyo` / `init`) で注入されます。protobuf を受けたい場合は `pip install 'mozyo-bridge[otel]'`。
- `mozyo-bridge session list` の各 pane に `activity` (active / idle / unknown) が付き、text 出力にも ACTIVITY 列が出ます。`mozyo-bridge doctor` の `otel` section が receiver 疎通と「telemetry を一度も発していない agent (観測漏れ)」を報告します。
- **launchd 常駐 (macOS)**: `mozyo-bridge otel launchd install` で受け口を LaunchAgent 常駐化できます (`status` / `restart` / `uninstall`)。plist には環境変数を一切書かないため secret は乗りません (その帰結として launchd 配下では Redmine 表示は `unconfigured` です)。upgrade 後は `otel launchd restart` (実行 path が変わった場合は install から)。詳細設計と runbook は `vibes/docs/logics/otel-event-store.md`。
- **コックピット Web UI**: `mozyo-bridge otel serve` が同じ port で `http://127.0.0.1:4318/` に unit 一覧 UI を配信します (127.0.0.1 のみ。iTerm2 Toolbelt webview / 任意ブラウザで同一 UI)。各 unit の activity 表示・遷移フィード・Reveal in Finder・jump (attach client への `switch-client`。`-CC` の focus 移動は対象外) を提供します。Toolbelt 登録手順は `vibes/docs/logics/cockpit-web-ui.md`。
- **Redmine gate 表示 (読み取りのみ)**: daemon 起動時に `MOZYO_REDMINE_URL` (信頼する Redmine の base URL) と `MOZYO_REDMINE_API_KEY` を環境変数で渡すと、各 unit に workspace の Redmine project の最新更新 open issue (gate / workflow 文脈) が表示されます。request は **`MOZYO_REDMINE_URL` だけ** に発行され、workspace 側 file が key の送信先を変えることはできません (host 不一致 workspace は fetch なしで `unconfigured`)。未設定は `unconfigured`、到達不能は `unavailable` として安全に縮退し、Redmine へ書き込みは一切しません。

### Module health gate (`mozyo-bridge health`)

runtime package の module health を主観ではなく計測と gate で管理します (Redmine #12321)。PyLint を runtime 依存に足さず、`too-many-lines` 相当の **equivalent native gate** を提供します。

```bash
mozyo-bridge health report [--limit N] [--json]   # module ごとの LOC / 近似 complexity / top-level symbol
mozyo-bridge health check [--json]                # oversized-module gate (CI 接続済)
```

- 閾値は `module_health.yaml` の `max_module_lines` (default **1000** = PyLint default)。`include` で scope (default `src/mozyo_bridge`) を制御します。
- 既存 oversized file は `module_health.yaml` の `allowlist` に `reason` / `owner_issue` / `resolution_version` と baseline `lines` を記録します (本 issue は既存 file を分割しません)。
- `health check` は **新規** oversized file (allowlist 未登録) と、allowlist 済 file の baseline 超過 (**成長**) を fail させます。改善 (縮小) は warning に留めます。
- `.github/workflows/test.yml` の `Module-health gate` step が install 後に `mozyo-bridge health check` を実行します。設計正本は `vibes/docs/logics/module-health-gate.md`。

### VS Code `tmux-integrated` の session 名 (`mozyo-bridge session name`)

VS Code の `tmux-integrated` 拡張 / TaskPilot menu は workspace basename から tmux session 名を導出します (典型的には `basename "$PWD" | sed ...`)。basename が日本語など非 ASCII を含むと (`2026PBL_ローカル` など) `2026PBL_____` のような低情報量名に潰れ、同名の `____` session が複数 workspace で衝突すると `mozyo-bridge agents list` / `--target-repo` handoff gate が repo identity を復元できなくなります。

bare `mozyo` は既にこの導出名で session を作りますが、VS Code は `mozyo` を経由せず自前で session を立てるため、VS Code 側にも同じ導出名を渡す必要があります。

`mozyo-bridge session name` は **衝突しにくい ASCII session 名**を返します。`mozyo-bridge workspace register` 済みなら登録済み canonical session 名をそのまま返し (registry → workspace anchor の順、Redmine #11429)、未登録なら repo path から導出します: `<repo>/.mozyo-bridge/project-defaults.yaml` (旧名 `workspace-defaults.yaml` も fallback で読む) の `redmine.default_project.identifier` があればそれを優先し (`mozyo-<identifier-slug>`)、無ければ repo path の短い hash を付けた fallback (`mozyo-<basename-slug>-<hash>`) を返します。非 ASCII basename を `____` に潰すことはなく、同名 basename でも path hash で区別されます。

```bash
# 単一行出力 (shell / task script から使う)
mozyo-bridge session name --repo /path/to/your-repo
# => mozyo-giken-3800-mozyo-bridge

# 導出元込みの JSON
mozyo-bridge session name --repo /path/to/your-repo --json
```

運用方針:

- **user-global の `tmux-integrated.sessionName` 固定値は使わない**でください。全 workspace が同一 session 名に collapse し、別 repo へ誤送信する危険が出ます。
- **workspace-local で固定する (推奨・機械化)**。`mozyo-bridge session vscode-settings --repo . --write` を実行すると、`<repo>/.vscode/settings.json` の `"tmux-integrated.sessionName"` を導出名に設定します。**workspace-local 設定のみ**を触り、credential を含み得る user-global 設定は読み書きしません。コメント付き (JSONC) の settings は壊さず、手編集を促して停止します。`--write` 無しは適用内容を表示する dry-run です。

  ```bash
  mozyo-bridge session vscode-settings --repo .            # dry-run
  mozyo-bridge session vscode-settings --repo . --write    # .vscode/settings.json に書き込み
  ```

- TaskPilot / 自前 task menu で動的に session 名を組む場合は、`basename "$PWD" | sed ...` を `mozyo-bridge session name --repo .` の出力に置き換えてください。例:

  ```bash
  s=$(mozyo-bridge session name --repo .)
  tmux new-session -A -s "$s"
  ```

- `session name` / `session vscode-settings` (dry-run) は tmux state も Redmine も変更しません。`--target-repo` gate と `init` の同名 window fail-closed の安全境界は変更していません。

### TaskPilot / 自前ランチャーの「新規 window 検出」(`No new tmux window found`)

TaskPilot 等が `mozyo` 起動直後に **新規 tmux window の出現** を待ち受ける startup 検出を行うと、既存 session の再利用時に次のエラーで失敗することがあります。

```text
TaskPilot: Shell command failed: No new tmux window found for session: mozyo-gk-0999-ningyo-tsukai (ステップ 9)
```

これは mozyo-bridge の不具合ではなく、**idempotent な window model** の正しい挙動です (Redmine #11312)。

- bare `mozyo` は「1 repo = 1 session, 1 agent = 1 window」を**冪等に保証**します。session に既に `claude` / `codex` window があれば、**新規 window を作らず既存を再利用**し、stdout に `created=-` (新規作成なし) と現在の window 一覧 (`INDEX / NAME / PROCESS` table) を出します。
- したがって、2 回目以降の `mozyo` 実行 (例: `mozyo --repo <target> --no-attach` で一度作成済みの session) では新規 window が現れません。「新規 window の出現」を success 条件にしている launcher はここで取りこぼします。
- エラー中の `(ステップ 9)` は **TaskPilot 内部の step 番号**で、mozyo-bridge handoff preflight の "Step 9" (window-name binding) とは無関係です。
- session 名 `mozyo-gk-0999-ningyo-tsukai` は既に `mozyo-bridge session name` の導出名です。よって本件の原因は session 名 mismatch (前節) ではなく、**window の再利用**です。

外部 launcher 側の推奨修正 / 運用回避策:

- **推奨: `mozyo --no-attach --json` の `ready` boolean を見る。** `--json` を付けると bare `mozyo` は human table の代わりに安定した JSON を stdout に出します。`ready` は `claude` / `codex` window が存在するときに `true` で、新規作成か再利用かに依存しません (`created` が空でも `ready: true`)。`--json` は attach を行わない (stdout を取り込む launcher の process が `tmux attach` に置換されない) ため、`--no-attach` 用途に最適です。`jq` でそのまま判定できます。

  ```bash
  # ready が true なら成功。created/windows/attach も同じ payload に含まれます。
  mozyo --repo . --no-attach --json | jq -e '.ready' >/dev/null \
    && echo "ready" || echo "not ready"
  ```

  payload schema (安定 key):

  ```json
  {
    "session": "mozyo-gk-0999-ningyo-tsukai",
    "repo_root": "/abs/path/to/repo",
    "cwd": "/abs/path/to/repo",
    "created": ["claude:%1", "codex:%2"],
    "windows": [
      {"index": 0, "name": "claude", "process": "claude"},
      {"index": 1, "name": "codex", "process": "node"}
    ],
    "ready": true,
    "attach": "tmux attach -t mozyo-gk-0999-ningyo-tsukai",
    "attach_target": "mozyo-gk-0999-ningyo-tsukai",
    "attached": false,
    "no_attach": true,
    "legacy_session_notice": null
  }
  ```

- **fallback: human stdout / tmux を parse する。** `--json` を使えない場合は、`mozyo --no-attach` の決定的な stdout (`session=<name> created=...` と `INDEX / NAME / PROCESS` table) か tmux を直接 parse し、`claude` / `codex` window が在ることを確認します。新規作成か再利用かには依存させません。

  ```bash
  s=$(mozyo-bridge session name --repo .)
  mozyo --repo . --no-attach >/dev/null
  # 期待する agent window が在れば成功 (新規作成かどうかは問わない)
  tmux list-windows -t "$s" -F '#{window_name}' | grep -qx claude \
    && tmux list-windows -t "$s" -F '#{window_name}' | grep -qx codex \
    && echo "ready: $s"
  ```

- 既存 session を毎回まっさらにしたい場合に限り、起動前に `tmux kill-session -t "$s"` してから `mozyo` を実行すると新規 window が必ず作られます。ただし**実行中の agent も落とす**ため常用は非推奨です。
- session 名は前節のとおり `mozyo-bridge session name --repo .` に揃えてください。

### TaskPilot 起動メニューを User settings で共有する (`taskPilot.configPath` + smart init)

TaskPilot の mozyo 起動メニューは、**workspace-local な `.vscode/task-menu.yaml` を長期 source-of-truth にしない**でください。workspace ごとに menu を複製すると、`mozyo-bridge init` の挙動更新 (smart adoption など) が全 workspace へ伝播せず drift します。代わりに **VS Code User settings の `taskPilot.configPath`** で User 配下の共通 `task-menu.yaml` を 1 つ参照し、全 workspace で同じ menu を使ってください。

User settings (`Preferences: Open User Settings (JSON)`) に追加します。**TaskPilot は `configPath` に実際の絶対パスを要求し、`${userHome}` などの VS Code 変数を展開しません** (絶対パスでない値は workspace root と結合されるため、相対扱いになると User-level menu を読めません)。下記の `<user>` を自分の username に置き換えてください (個人名は環境ごとに異なるため、テンプレートには固定値を埋め込みません):

```jsonc
{
  // <user> を自分の username に置き換える。configPath は絶対パスのみ (変数展開なし)。
  // macOS:   /Users/<user>/Library/Application Support/Code/User/task-menu.yaml
  // Linux:   /home/<user>/.config/Code/User/task-menu.yaml
  // Windows: C:\\Users\\<user>\\AppData\\Roaming\\Code\\User\\task-menu.yaml
  "taskPilot.configPath": "/Users/<user>/Library/Application Support/Code/User/task-menu.yaml"
}
```

参照先の `task-menu.yaml` (共通テンプレート)。**固定個人 repo パスを書かず**、`repo="$(pwd -P)"` で TaskPilot の shellCommand cwd から解決します:

```yaml
version: "1.0"

menu:
  - label: モジョ-Bridge
    icon: "$(rocket)"
    children:
      - label: 起動[整列!]
        icon: "$(terminal)"
        description: TaskPilot/tmux-integrated 用。settings を同期し、期待 session に作られた新 window を claude/codex pane として初期化する
        actions:
          - type: shellCommand
            command: |
              set -eu
              repo="$(pwd -P)"
              session="$(mozyo-bridge session name --repo "$repo")"
              state="/tmp/taskpilot-mozyo-${session}-claude.before"
              tmux list-windows -a -F '#{window_id}' 2>/dev/null > "$state"

          - type: vscodeCommand
            command: tmux-integrated.newTerminal
          - type: vscodeCommand
            command: workbench.action.focusActiveEditorGroup
          - type: vscodeCommand
            command: workbench.action.newGroupRight
          - type: vscodeCommand
            command: workbench.action.focusRightGroup
          - type: vscodeCommand
            command: workbench.action.terminal.moveToEditor
          - type: vscodeCommand
            command: workbench.action.lockEditorGroup

          - type: shellCommand
            command: |
              set -eu
              repo="$(pwd -P)"
              session="$(mozyo-bridge session name --repo "$repo")"
              state="/tmp/taskpilot-mozyo-${session}-claude.before"
              current="/tmp/taskpilot-mozyo-${session}-claude.current"
              pane=""
              i=0
              while [ "$i" -lt 50 ]; do
                tmux list-windows -a -F '#{window_id}|#{session_name}|#{pane_id}|#{pane_current_command}' 2>/dev/null > "$current"
                # before に無く current に在る window_id だけを新規とみなし、候補を
                # 期待 session か underscore fallback session (`^_+$`) の shell pane に限定する。
                pane="$(
                  awk -F'|' -v session="$session" '
                    NR == FNR { seen[$1] = 1; next }
                    !($1 in seen) && ($2 == session || $2 ~ /^_+$/) && ($4 ~ /^(zsh|bash|fish)$/) {
                      print $3
                      exit
                    }
                  ' "$state" "$current"
                )"
                [ -n "$pane" ] && break
                i=$((i + 1))
                sleep 0.1
              done
              if [ -z "$pane" ]; then
                echo "tmux-integrated did not create a new window" >&2
                echo "reopen this workspace if tmux-integrated was already attached to a fallback session." >&2
                echo "Current tmux windows:" >&2
                tmux list-windows -a -F '#{session_name} #{window_index} #{window_name} #{pane_id} #{pane_current_path} #{pane_current_command}' >&2 || true
                exit 1
              fi
              # init の前に対象 pane を repo へ移動し、pane cwd が反映されるまで待つ。
              tmux send-keys -t "$pane" "cd -- '$repo'" C-m
              i=0
              while [ "$i" -lt 50 ]; do
                [ "$(tmux display-message -p -t "$pane" '#{pane_current_path}')" = "$repo" ] && break
                i=$((i + 1))
                sleep 0.1
              done
              # smart init: fallback session を期待 session へ rename し、vscode 設定を
              # pin し、window を claude に rename する (`mozyo-bridge init` を参照)。
              mozyo-bridge init claude "$pane"
              tmux send-keys -t "$pane" "claude" C-m

          - type: shellCommand
            command: |
              set -eu
              repo="$(pwd -P)"
              session="$(mozyo-bridge session name --repo "$repo")"
              state="/tmp/taskpilot-mozyo-${session}-codex.before"
              tmux list-windows -a -F '#{window_id}' 2>/dev/null > "$state"

          - type: vscodeCommand
            command: tmux-integrated.newTerminal
          - type: vscodeCommand
            command: workbench.action.focusActiveEditorGroup
          - type: vscodeCommand
            command: workbench.action.newGroupRight
          - type: vscodeCommand
            command: workbench.action.focusRightGroup
          - type: vscodeCommand
            command: workbench.action.terminal.moveToEditor
          - type: vscodeCommand
            command: workbench.action.lockEditorGroup

          - type: shellCommand
            command: |
              set -eu
              repo="$(pwd -P)"
              session="$(mozyo-bridge session name --repo "$repo")"
              state="/tmp/taskpilot-mozyo-${session}-codex.before"
              current="/tmp/taskpilot-mozyo-${session}-codex.current"
              pane=""
              i=0
              while [ "$i" -lt 50 ]; do
                tmux list-windows -a -F '#{window_id}|#{session_name}|#{pane_id}|#{pane_current_command}' 2>/dev/null > "$current"
                pane="$(
                  awk -F'|' -v session="$session" '
                    NR == FNR { seen[$1] = 1; next }
                    !($1 in seen) && ($2 == session || $2 ~ /^_+$/) && ($4 ~ /^(zsh|bash|fish)$/) {
                      print $3
                      exit
                    }
                  ' "$state" "$current"
                )"
                [ -n "$pane" ] && break
                i=$((i + 1))
                sleep 0.1
              done
              if [ -z "$pane" ]; then
                echo "tmux-integrated did not create a new window" >&2
                echo "reopen this workspace if tmux-integrated was already attached to a fallback session." >&2
                echo "Current tmux windows:" >&2
                tmux list-windows -a -F '#{session_name} #{window_index} #{window_name} #{pane_id} #{pane_current_path} #{pane_current_command}' >&2 || true
                exit 1
              fi
              tmux send-keys -t "$pane" "cd -- '$repo'" C-m
              i=0
              while [ "$i" -lt 50 ]; do
                [ "$(tmux display-message -p -t "$pane" '#{pane_current_path}')" = "$repo" ] && break
                i=$((i + 1))
                sleep 0.1
              done
              mozyo-bridge init codex "$pane"
              tmux send-keys -t "$pane" "codex" C-m

      - label: 状態確認
        icon: "$(info)"
        type: shellCommand
        command: |
          set -eu
          session="$(mozyo-bridge session name --repo .)"
          echo "$session"
          tmux list-windows -t "=$session" -F '#{window_index}\t#{window_name}\t#{pane_current_path}\t#{pane_current_command}'
```

起動 snippet の仕組み (smart init 前提):

- **before/current の window_id 差分で新規 window を特定する。** `tmux-integrated.newTerminal` の前に `tmux list-windows -a -F '#{window_id}'` を snapshot し、生成後の一覧と比較して「before に無い window_id」だけを新規候補にします。**index ではなく `window_id`** を使うのは、index が再利用・並び替えされても一意だからです。
- **全 session を横断し、候補を絞る。** 検出は `list-windows -a` (全 session) で行い、候補 pane を **期待 session (`mozyo-bridge session name`) か underscore fallback session (`^_+$`、tmux-integrated の低情報量名)** かつ **shell pane (`zsh|bash|fish`)** に限定します。これで他 workspace の window や既に agent 化済みの pane を誤検出しません。
- **`init` の前に `cd -- <repo>` を送り、pane cwd を待つ。** smart `mozyo-bridge init` は pane の cwd から workspace root と期待 session を導出するため、init 前に対象 pane を repo へ移動し、`#{pane_current_path}` が `$repo` になるまで待ってから `mozyo-bridge init claude|codex "$pane"` を呼びます。
- **smart `init` が session/window/settings をまとめて整える。** fallback session を期待 session へ rename し、`.vscode/settings.json` の `tmux-integrated.sessionName` を pin し、window を `claude` / `codex` に rename します (詳細は上記 `VS Code tmux-integrated の session 名` の節と `mozyo-bridge init --help`)。menu 側で別途 `session vscode-settings --write` を呼ぶ必要はありません。

古い `taskPilot.globalMenu` の扱い:

- 以前 **`taskPilot.globalMenu`** に mozyo 起動 entry を直接書いていた場合、その entry が残っていると新しい `configPath` menu と二重に出たり、古い起動ロジックで rollout を **shadow** することがあります。`configPath` へ移行したら、`taskPilot.globalMenu` 側の旧 mozyo entry は **削除する** か、`configPath` menu と **同一 label** にして上書き (override) させてください。両方に別 label で残すと、利用者が古い導線を踏み続けます。

### Subtle window status colors

bare `mozyo` と `mozyo-bridge init <agent>` は agent window の tmux status bar entry に**控えめな色**を付けます。`claude` は muted sage green (`colour108`)、`codex` は muted slate blue (`colour67`)、それ以外の window は user 設定の default のまま無加工です。配色は fg のみで、背景塗りや点滅は使いません。

色は window 識別のためであり、resolver / handoff routing は依然 window 名 (`claude` / `codex`) を exact key として使うため、**window 名は変更されません**。色を外したい場合は `tmux set-window-option -u -t <session>:<window> window-status-style` (および `window-status-current-style`) で個別 unset するか、operator の `.tmux.conf` 側で上書きしてください。`.tmux.conf` / `~/.config/mozyo-bridge/tmux.conf` の読み込み挙動には影響しません。

### tmux UI host wiring (`tmux-ui install`)

governed preset が配布する `.mozyo-bridge/tmux/agent-ui.conf` を host 側 tmux 設定 (default `~/.tmux.conf`) から自動 source したい場合は、`mozyo-bridge tmux-ui install` を使います。**operator の既存 `~/.tmux.conf` を丸ごと上書きすることはなく**、`# >>> mozyo-bridge tmux-ui >>>` / `# <<< mozyo-bridge tmux-ui <<<` で囲んだ管理ブロックのみを追加 / 更新 / 削除します。ブロック内部は `if-shell` で snippet の存在確認をしてから `source-file` するため、repo を移動 / 削除しても tmux 起動が壊れません。

主な操作:

```
# repo に scaffold 済みの agent-ui.conf を ~/.tmux.conf に wiring する
mozyo-bridge tmux-ui install --target <repo>

# 何が書き換わるか確認だけしたい
mozyo-bridge tmux-ui install --target <repo> --dry-run

# 既存 ~/.tmux.conf のバックアップを残してから wiring する
mozyo-bridge tmux-ui install --target <repo> --backup

# 既存ブロックが別 repo の path を指している (drift) ときに上書きする
mozyo-bridge tmux-ui install --target <repo> --force

# wiring を解除する (管理ブロックのみ削除、surrounding 設定は無傷)
mozyo-bridge tmux-ui uninstall

# wiring 状態を確認する (not-installed / installed / drift)
mozyo-bridge tmux-ui status --target <repo>
mozyo-bridge tmux-ui status --target <repo> --json   # drift 時は exit 1
```

idempotent な操作です。同じ repo path に対する 2 回目の `install` は no-op、`install` → `uninstall` の round-trip は byte-for-byte で原状復帰します (managed block 周辺の blank line 含む)。host 側の tmux 設定パスを上書きしたい場合 (例: `$XDG_CONFIG_HOME/tmux/tmux.conf`) は `--tmux-conf <path>` を指定してください。

`doctor` は `tmux.artifact.host_wiring` で同じ状態を report します。`installed` / `not-installed` / `drift` の三状態と、現在 source している path、推奨復旧コマンドを表示します。導入したくない operator は単に `install` を実行しなければよく、scaffold は host 設定に一切触れません。

## Beta Tester Install (GitHub main)

PyPI release 前の beta tester 向け手順です。`Quick Start` の PyPI install とは別経路で、GitHub `main` の最新 commit を直接 install します。`mozyo-bridge --version` が表示する package version 文字列は `pyproject.toml` の値なので、PyPI release と GitHub `main` で同じ string になる場合があります。実体差は新規 sub-command (例: `mozyo-bridge scaffold status --help` / `mozyo-bridge doctor --json`) や、`mozyo-bridge rules install` が配布する preset 内容で確認してください。

PyPI / TestPyPI release の検証手順は本節と同じ acceptance smoke を、GitHub `main` install のかわりに該当 PyPI install で実行してください。release 経路の詳細は `vibes/docs/logics/release-flow.md` を見ます。

### Isolation principle

`mozyo-bridge scaffold apply <preset>` は対象 directory の `AGENTS.md` / `CLAUDE.md` を生成 / 上書きします。本 repository (`mozyo_bridge` 自身) の tracked router を壊さないために、検証は必ず以下のどちらかで行います。

- `./tmp/mb-smoke-asana` / `./tmp/mb-smoke-redmine` / `./tmp/mb-smoke-redmine-governed` / `./tmp/mb-smoke-redmine-rails` のような isolated target を使う (`./tmp/` は `.gitignore` 配下の作業領域)。
- もしくは別 directory で `git clone` した fresh checkout、または任意の `/tmp/...` directory を使う。

本 repo の working tree で `mozyo-bridge scaffold apply <preset>` を `--target` 無しで実行しないでください。tracked `AGENTS.md` / `CLAUDE.md` が上書き候補になり、`scaffold apply` 自体は default で既存ファイルを保護しますが、`--force` / `--backup` を伴うと取り違える可能性があります。事前に `mozyo-bridge scaffold diff <preset>` で差分を確認してください。

### Acceptance smoke

1. GitHub `main` から install (既存 PyPI install を上書き):

   ```bash
   pipx install --force git+https://github.com/hollySizzle/mozyo_bridge.git
   ```

2. user-global rules を install して状態を確認:

   ```bash
   mozyo-bridge rules install
   mozyo-bridge rules status
   ```

   `rules status` は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/` に展開された user-global 規約 (`src/mozyo_bridge/scaffold/presets/presets.yaml` の preset registry) の状態を表示します。

   commit する docs / router に貼る portable 表記、または local diagnostics 用の resolved path を直接確認したい場合は `mozyo-bridge rules home` を使ってください。default は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` を出力するので docs snippet 用にそのまま貼れます。`--resolved` を付けると環境変数 `MOZYO_BRIDGE_HOME` と `~` を展開した absolute path を表示しますが、こちらは operator の `$HOME` を含み得るので committed docs には貼らないでください。

3. agent skill を install (推奨経路):

   ```bash
   # Claude Code: plugin marketplace 経由 (推奨)
   claude plugin marketplace add hollySizzle/mozyo_bridge
   claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user

   # Codex: ユーザーが Codex 環境で $skill-installer を実行する
   $skill-installer https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent
   ```

   Claude Code 側は `plugins/mozyo-bridge-agent/` (`.claude-plugin/plugin.json`) を marketplace 経由で取得します。Codex 側は canonical `skills/mozyo-bridge-agent/` を、**ユーザー/オペレーターが Codex 環境で** `$skill-installer` により同期します。`plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` は `scripts/sync_plugin_skill.sh` が canonical から mirror した copy で、drift は unit test で検出します。

   bootstrap では curl/script による skill install は禁止です。詳細と precedence の落とし穴 (Claude Code は同名 skill で personal が project を override) は `Agent Skill Install` 節と `vibes/docs/logics/skill-distribution.md` を参照してください。

4. Claude Code / Codex を再起動して、新しい skill と user-global 規約を再読み込みさせます。同 session 内では skill index がキャッシュされるため再起動を省略しないでください。

5. install 直後の前提を確認します。Claude 側は plugin marketplace 経路を、`mozyo-bridge doctor` は CLI / rules / Codex skill / (fallback path を使った場合のみ) Claude skill を見ます。

   Claude plugin marketplace 経路の確認:

   ```bash
   claude plugin marketplace list
   claude plugin list
   ```

   `claude plugin marketplace list` に `mozyo-bridge` が出て、`claude plugin list` に `mozyo-bridge-agent@mozyo-bridge` が出ていれば、Claude Code 側の primary install は成立しています。plugin skill は `~/.claude/plugins/cache/` 配下に展開され、Claude Code 起動時に `mozyo-bridge-agent:mozyo-bridge-agent` namespace で読み込まれます。

   CLI / rules / Codex skill の確認:

   ```bash
   mozyo-bridge doctor
   ```

   このタイミングでは `scaffold` section が `missing` (`-> mozyo-bridge scaffold apply <preset> --target ...`) になりますが、`cli` / `rules` / `codex_skill` の 3 section が ok であることを確認します。`next_action` (`-> ...`) を読み、不足があれば該当 install / set up を再実行してください。

   `claude_skill` section の扱い:

   - primary path (plugin marketplace) でだけ install した場合、`claude_skill: plugin-managed` が出ます。現行 `mozyo-bridge doctor` は legacy directory (`~/.claude/skills/` / `<project>/.claude/skills/`) に加えて plugin cache (`~/.claude/plugins/cache/`) も scan しており、plugin だけ検出した状態は `plugin-managed` という固有 status で healthy として扱われます (`next_action` は空、overall doctor も `ok=true` を維持)。primary path の最終確認は上の `claude plugin list` で行います。
   - fallback path (`scripts/install_claude_skill.sh`) を併用した場合は legacy directory にも skill が入るので `claude_skill: ok` が出ます。同名 skill が plugin と legacy 両方に居る状況は plugin namespace (`mozyo-bridge-agent:mozyo-bridge-agent`) で分離されるため衝突しませんが、`scripts/install_claude_skill.sh` 経由の legacy global Claude skill は新規 install で deprecated です (`vibes/docs/logics/skill-distribution.md` の `## Legacy Global Claude Skill Deprecation` 節参照)。precedence の落とし穴は `Agent Skill Install` 節を見てください。

6. isolated target に対して Asana / Redmine の repo-local scaffold を smoke:

   ```bash
   mkdir -p ./tmp/mb-smoke-asana
   mozyo-bridge scaffold diff asana --target ./tmp/mb-smoke-asana  # 任意: 適用前の差分確認
   mozyo-bridge scaffold apply asana --target ./tmp/mb-smoke-asana
   mozyo-bridge scaffold status --target ./tmp/mb-smoke-asana
   mozyo-bridge doctor --target ./tmp/mb-smoke-asana

   mkdir -p ./tmp/mb-smoke-redmine
   mozyo-bridge scaffold diff redmine --target ./tmp/mb-smoke-redmine
   mozyo-bridge scaffold apply redmine --target ./tmp/mb-smoke-redmine
   mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine
   mozyo-bridge doctor --target ./tmp/mb-smoke-redmine

   mkdir -p ./tmp/mb-smoke-redmine-governed
   mozyo-bridge scaffold diff redmine-governed --target ./tmp/mb-smoke-redmine-governed
   mozyo-bridge scaffold apply redmine-governed --target ./tmp/mb-smoke-redmine-governed
   cp ./tmp/mb-smoke-redmine-governed/.mozyo-bridge/docs/catalog.yaml.example \
      ./tmp/mb-smoke-redmine-governed/.mozyo-bridge/docs/catalog.yaml
   mozyo-bridge docs validate --repo ./tmp/mb-smoke-redmine-governed
   mozyo-bridge docs validate --check-file-coverage --repo ./tmp/mb-smoke-redmine-governed
   mozyo-bridge docs generate-file-conventions --repo ./tmp/mb-smoke-redmine-governed
   mozyo-bridge docs generate-file-conventions --check --repo ./tmp/mb-smoke-redmine-governed
   mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine-governed
   mozyo-bridge doctor --target ./tmp/mb-smoke-redmine-governed

   mkdir -p ./tmp/mb-smoke-redmine-rails
   mozyo-bridge scaffold diff redmine-rails --target ./tmp/mb-smoke-redmine-rails
   mozyo-bridge scaffold apply redmine-rails --target ./tmp/mb-smoke-redmine-rails
   mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine-rails
   mozyo-bridge doctor --target ./tmp/mb-smoke-redmine-rails
   ```

   各 target で `scaffold status` が `result: clean` を返し、`mozyo-bridge doctor --target ...` の `scaffold` section が `ok` であれば、user-global 規約・repo-local routers・manifest が整合しています。主要 preset boundary (Asana / Redmine / Redmine Governed / Redmine Rails) を確認します。片側だけで完了させると preset 間 boundary の検証が落ちます。

7. CI / 機械的な acceptance smoke では `--json` を使います:

   ```bash
   mozyo-bridge doctor --target ./tmp/mb-smoke-asana --json
   ```

   出力は `{"ok": <bool>, "sections": {"cli": {...}, "rules": {...}, "codex_skill": {...}, "claude_skill": {...}, "scaffold": {...}, "tmux": {...}}}` 形式で、`jq '.sections.scaffold.status == "ok"'` 等で gate を組めます。exit code は `ok` が false の時に非ゼロです。primary path (plugin marketplace) でだけ install した CI は `jq '.sections.claude_skill.status'` を `== "missing"` で gate しないでください — 上記 step 5 の通り `plugin-managed` が期待状態 (healthy) です。`jq '.sections.claude_skill.status as $s | $s == "ok" or $s == "plugin-managed"'` のように plugin-managed を含めて healthy 判定するか、`jq '.ok'` で overall gate を組んでください。

`mozyo-bridge rules status` (user-global 規約の install 状態) と `mozyo-bridge scaffold status` (repo-local manifest drift) は別責務です。前者は host 全体、後者は 1 つの scaffold 済 project を見ます。`mozyo-bridge doctor` は両者と CLI / Codex skill / Claude skill (plugin cache + legacy directory) / tmux を 1 command で見る 6-section diagnostic です。Claude 側の primary path (plugin marketplace) は doctor の plugin cache scan 対象に入っており、plugin だけ install した状態は `claude_skill: plugin-managed` として healthy になります。最終 install 確認は `claude plugin list` を併用してください。詳細は次の logic docs を正本にしてください。

- `vibes/docs/logics/skill-distribution.md`
- `vibes/docs/logics/scaffold-rules.md`

PyPI / TestPyPI release 後に、ルートの `AGENTS.md` / `CLAUDE.md` を一旦削除して install 済 package だけで scaffold + 自律 handoff まで復旧できるかを検証する破壊的な acceptance test は別経路です。前提 (clean worktree / git-managed) と手順は `vibes/docs/logics/turnkey-e2e-acceptance.md` を参照してください。本 `Beta Tester Install` section の smoke は `./tmp/mb-smoke-*` で実行し、本 repo の tracked router を壊しません。

## Project Root Resolution

PyPI / pipx などで CLI としてインストールする場合は、インストール先ではなく実行場所から project root を決めます。

優先順位:

1. `--repo /path/to/repo`
2. `MOZYO_REPO=/path/to/repo`
3. 現在のディレクトリから親方向に `.git` / `.tmux.conf` / `pyproject.toml` を探索
4. 見つからない場合は現在のディレクトリ

例:

```bash
mozyo-bridge status --repo /path/to/repo
MOZYO_REPO=/path/to/repo mozyo
```

bare `mozyo` (標準導線) は 1 つの repo-scoped session 内に `claude` / `codex` window を ensure し attach します。
`.tmux.conf` は project root にあればそれを使い、なければ `~/.config/mozyo-bridge/tmux.conf` を見ます。

config が default 解決経路のどちらにも存在しない場合、bare `mozyo` と `notify-*` は config の source を skip して session / pane 起動だけを実行します。一方 `--config-path /path/to/conf` を **明示** 指定した場合は path 不存在を typo 防止のため `tmux config not found` で fail-fast します。`mozyo-bridge tmux-ui-config` (load 専用 command) も従来通り存在しない config は error にします。

## Pane Setup

標準導線は `mozyo` (bare) です。repo root で `mozyo` を実行すると `claude` / `codex` の window が自動で揃い、target resolution はその window 名だけを参照します。

VS Code `tmux: New tmux Terminal` や hand-managed tmux pane など、別の経路で開いた pane を agent target にしたい場合は、その pane の中で `init` を実行して window 名を agent 名に変更します。

```bash
mozyo-bridge init claude   # 現在の pane を含む window を `claude` にリネーム
mozyo-bridge init codex %42  # 特定 pane の window を `codex` にリネーム
```

`init` は pane label (`@agent_name`) を一切触りません。window の rename だけが agent identity を確立する単一経路です。同じ session に既に `<agent>` という名前の window が居る場合は明示 error で止まります (tmux は同名 window の重複を黙認しますが resolver はしません)。

状態確認:

```bash
mozyo-bridge status
```

`status` は引数なしで実行すると、`TMUX_PANE` から現在の tmux session を解決し、無ければ repo basename にフォールバックします。current session 内に `claude` / `codex` window があれば一覧が `WINDOW NAME TARGET ACTIVE PROCESS CWD` 表で出力されます。無ければ `no agent windows in this session` という informational 行と、`mozyo` / `mozyo-bridge init claude|codex` の hint を表示します。`--session NAME` で明示指定もできます。

### Target resolution (window-only model)

`mozyo-bridge message claude ...` / `read claude` / `notify-claude` のように agent label を target に指定したとき、解決経路は 1 本だけです。

1. 現在の tmux session 内で `claude` / `codex` という名前の window を探し、その active pane を target にします。
2. 見つからなければ明示 error で止まります。`%pane_id` を直接渡すか、対象 pane で `mozyo-bridge init claude|codex` を打って window を rename してから再実行してください。
3. cross-session fallback はしません。別 session の同名 window へ解決して mis-route した過去事例 (Asana task 1214743574772820 comment 1214746077864452) を踏まないための fail-closed です。

これまで存在した `@agent_name` label による互換 path は廃止しました。標準 tmux pane を target にしたい場合は `mozyo-bridge init <agent>` で window 名を正規化してください。

### Cross-workspace agent discovery (`agents list`)

複数 workspace を同時に開いている場合、session / window / pane / process / cwd / repo root / agent 種別を一括で取得できる read-only な discovery surface が必要です。これは `mozyo-bridge list` (raw single-session pane table) や `mozyo-bridge status` (current session diagnostics) とは別 namespace に分離してあります。

```bash
mozyo-bridge agents list
mozyo-bridge agents list --session other-repo
mozyo-bridge agents list --agent claude --json
```

- **1 行 = 1 `pane_id`** です (Redmine #11628)。tmux session group で同一 pane が複数 session に所属していても agent としては 1 行に畳まれ、所属 session は `views` 配列 (canonical flag 付き) で保持されます。先頭の `session` 列は正準 view (workspace の canonical session 名と一致する所属を優先、無ければ session 名 sort 順で決定的に選択) です。単一 tmux server 前提で、複数 server 対応時は `(socket, pane_id)` 複合キーへ拡張します。
- `--session NAME` で session 名を完全一致 filter できます。畳まれた agent は所属するどの session 名でも match します (正準 view / grouped view の両方)。
- `--agent claude|codex|unknown` で window-name agent rail に基づく分類で filter できます。`unknown` は agent window でない pane を意味します。
- `--json` で JSON output が出ます (`session`, `window_index`, `window_name`, `pane_id`, `pane_index`, `pane_active`, `agent_kind`, `process`, `cwd`, `repo_root`, `ambiguous`, `views`)。既存 field の意味は従来どおり (値は正準 view のもの) で、`views` が追加です。
- text 出力も同様に畳まれ、末尾に `OTHER_VIEWS` 列 (正準以外の所属 session、無ければ `-`) が付きます。
- `repo_root` は pane の `cwd` から `.git` / `.tmux.conf` / `pyproject.toml` を遡って推定します。markers が見つからない場合は `null` です。情報用 field で、ここを根拠に handoff を拒否したい場合は下記の `--target-repo` を使ってください。
- `ambiguous=true` はいずれかの所属 session 内で同じ window name が複数 window に存在する状態です (resolver 既存 fail-closed と同じ条件)。

### Cross-Workspace Handoff Gate

別 workspace の Claude に直接通知を投げると target workspace の audit boundary (Codex 監査) を bypass してしまいます。これを防ぐため、`mozyo-bridge handoff send` / `reply` には次の制約が CLI レベルで入っています (Redmine #10332)。

- **Cross-session `--to claude` は拒否される**。sender の tmux session と target pane の session が異なるとき `--to claude` で送ろうとすると `blocked` / `cross_session_claude` で止まります。送信側は `--to codex --target <target_session>:codex --target-repo <target_workspace_root>` で target workspace の Codex window 経由に切り替え、target Codex から local Claude handoff を実行してもらいます。
- **Cross-session `--to codex` は gateway path**。Redmine #11301 以降、default の `queue-enter` rail は constrained identity gate を満たす cross-session target を admit します。条件は、明示的な `--target` pane id と、通過する `--target-repo` (target pane の cwd がその workspace / repo root に解決されること) の両方です。この gate を満たせば `--mode` 無しの default rail で gateway 送信が成立します。`--target-repo` 無し / repo 不一致・未解決 / target 暗黙指定の cross-session は引き続き `invalid_args` / `target_repo_mismatch` で拒否し、no-rollback 契約を検証済み workspace に縛り続けます。`--mode standard` / `--mode pending` は fallback として利用可能です (例: `--target-repo` を主張できない場合や、strict landing 観測が必要な場合)。
- **`--target-repo PATH` は repo / workspace identity gate** — `--target-repo /path/to/repo` を渡すと、target pane の cwd から walk-up した root が一致しない場合 `blocked` / `target_repo_mismatch` で止まります。root は `.git` / `.tmux.conf` / `pyproject.toml` を持つ directory、**または** scaffold 済み mozyo workspace marker `.mozyo-bridge/scaffold.json` (Redmine #11301) を持つ directory です (非 git の Google Drive workspace も first-class identity root になります)。同名 session を別 repo で開いている場合の mis-route 防止に加え、cross-session `--to codex` gateway 送信を default queue-enter rail で admit する条件でもあります。
- queue-enter mode の cross-session admission は上記 identity gate (明示 `--target` + 通過する `--target-repo`) を満たす場合のみで、それ以外は `invalid_args` で fail-closed します。same-session の queue-enter 既定挙動・誤送信防止 (window / process / active-pane / claude gateway) は弱めていません。本 gate は strict / pending mode でも cross-session の Claude 直撃を遮断する layer です。

Claude Code の project skill は repo root の `.claude/skills/` から解決されます。
`mozyo-bridge status` / `doctor` が `claude_pane cwd is outside repo root` を出した場合、その pane では `/mozyo-bridge-agent` などの project skill が解決されない可能性があります。
repo root で Claude Code を起動し直してから `mozyo-bridge init claude` を再実行してください。

## Agent Skill Install

### Claude Code (primary: plugin marketplace)

```bash
claude plugin marketplace add hollySizzle/mozyo_bridge
claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user
```

This adds the `mozyo-bridge` marketplace defined in `.claude-plugin/marketplace.json` at the repo root and installs the `mozyo-bridge-agent` plugin from `plugins/mozyo-bridge-agent/`. The plugin ships its own copy of the shared skill body (kept in lockstep with canonical `skills/mozyo-bridge-agent/` by `scripts/sync_plugin_skill.sh` and the drift test). Plugin skills are namespaced as `mozyo-bridge-agent:mozyo-bridge-agent`, so they do not conflict with personal or project skills of the same name.

### Codex (primary: $skill-installer against canonical GitHub skill path)

Canonical path: <https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent>

The user/operator must run Codex `$skill-installer` against the canonical `skills/mozyo-bridge-agent/` directory in `hollySizzle/mozyo_bridge` `main`. The skill body, references, and `agents/openai.yaml` are all there.

### Fallback: curl-based install scripts (legacy, deprecated for new installs)

`scripts/install_codex_skill.sh` and `scripts/install_claude_skill.sh` remain for environments where the recommended paths above are not available (offline mirrors, internal forks, fresh-tester acceptance smoke). They are **deprecated for new installs** — new users should use the plugin marketplace path (Claude) and `$skill-installer` (Codex) above; bootstrap and onboarding instructions should not use them.

In particular, the Claude personal-skill destination (`~/.claude/skills/mozyo-bridge-agent/`) that this script writes to is the **legacy global Claude skill** and is deprecated for new installs (Asana `1214733632421625`). It is not removed from existing user homes automatically; existing users may keep it or delete it manually. New installs should use only the plugin marketplace path so the `mozyo-bridge-agent:mozyo-bridge-agent` namespace prevents the personal-overrides-project precedence gotcha. See `vibes/docs/logics/skill-distribution.md` `## Legacy Global Claude Skill Deprecation` for the policy detail.

Similarly, the tracked project Claude skill at `<repo>/.claude/skills/mozyo-bridge-agent/` (loaded when Claude Code is started from this project root, or when `MOZYO_BRIDGE_CLAUDE_SCOPE=project` is used with the fallback script) is on a **grace-period deprecation** (Asana `1214733817990357`). It is not removed from the repo by this task; the canonical body remains under `skills/mozyo-bridge-agent/`. New installs should use only the plugin marketplace path; the project-scope adapter is preserved until the documented removal criteria are met. See `vibes/docs/logics/skill-distribution.md` `## Legacy Project Claude Skill (.claude/skills/mozyo-bridge-agent/) Grace-Period Deprecation` for the policy detail.

```bash
# Codex skill (fallback)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh

# Claude Code skill (fallback, user-global / personal)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh
```

この script の default scope は `global` です (#12360 で `project` から変更)。env 未設定の bare invocation は legacy personal skill (`~/.claude/skills/`) を書き、project mirror は明示的に `MOZYO_BRIDGE_CLAUDE_SCOPE=project` を渡した時だけ書きます。`VAR=... curl ... | sh` の形は env var が `curl` にしか渡らず script が default scope (`global`) で走るため、`project` など非 default scope を選ぶには pipe の右側で `sh` の直前に env を置く形を使ってください。両方の destination に配布したい場合は、明確な意図のもとで script を二度実行します (`scope=global` と `scope=project` を順に)。

**Claude Code は同名 skill について personal/user skill (`~/.claude/skills/`) を project skill (`<project>/.claude/skills/`) より優先します** (公式 docs: <https://code.claude.com/docs/en/skills>)。多くの開発ツールと逆向きの慣習なので注意してください。Plugin skills は `plugin-name:skill-name` で namespace 分離されるため、personal / project skill とは衝突しません。

Install destinations (fallback scripts):

- `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/` (Codex user-global)
- `${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}/skills/mozyo-bridge-agent/` (Claude user/personal, scope=global — default)
- `${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}/.claude/skills/mozyo-bridge-agent/` (Claude project adapter, scope=project — legacy opt-in)
- `${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}/skills/mozyo-bridge-agent/` (Claude project shared body, scope=project — legacy opt-in)

Both fallback scripts fetch `hollySizzle/mozyo_bridge` `main` by default. Override the source with `MOZYO_BRIDGE_SKILL_REPO`, `MOZYO_BRIDGE_SKILL_REF`, or `MOZYO_BRIDGE_SKILL_ARCHIVE_URL` (the last accepts any tarball URL, including `file:///...` for local-checkout install).

Detailed distribution rules live in `vibes/docs/logics/skill-distribution.md`.

## Agent Rules Scaffold

`mozyo-bridge` can install ticket-system-specific development flow rules and scaffold thin project routers for Claude Code and Codex.

Choose the preset before applying scaffold. The selection order is:

1. durable work system (`asana`, Redmine, or `none`);
2. framework surface (Rails vs non-Rails for Redmine projects);
3. governance depth (lightweight routers vs full governed catalog package).

Use `redmine-governed` / `redmine-rails-governed` only when the project will
maintain Project-Local Additions, `.mozyo-bridge/docs/catalog.yaml`, generated
file conventions, and the corresponding validation checks. Use `redmine` /
`redmine-rails` when thin routers and project-owned local policy are enough.
Use `none` when there is no durable ticket system. The full decision flow lives
in `vibes/docs/logics/bootstrap.md`; preset semantics live in
`vibes/docs/logics/scaffold-rules.md`.

> **Repo-Local Guardrail Autonomous Lane (governed presets only)** — the
> `redmine-governed` and `redmine-rails-governed` presets ship a Codex
> autonomous-edit carve-out for `vibes/docs/rules/**`, `vibes/docs/logics/**`,
> `vibes/docs/specs/**`, and `.mozyo-bridge/docs/catalog.yaml`. Inside the lane
> Codex may edit without a pre-edit `codex_direct_edit` gate journal; instead
> the preset requires a `codex_autonomous_edit` journal recorded with the
> commit (`lane`, `changed_paths`, `intent`, `verification`, `commit_hash`,
> `follow_up_review_required`). Distributed surfaces — `AGENTS.md`,
> `CLAUDE.md`, `.mozyo-bridge/rules/**`, skills / plugins, packaged preset
> templates, `src/**`, `tests/**` — stay under the standard gate. See the
> preset's `### Repo-Local Guardrail Autonomous Lane` section for the policy,
> and `vibes/docs/rules/codex-autonomous-guardrail-lane.md` in this repo for a
> concrete adoption example.

Install the central rules store:

```bash
mozyo-bridge rules install
mozyo-bridge rules status
```

Scaffold project routers:

```bash
mozyo-bridge scaffold apply asana
mozyo-bridge scaffold apply asana --target /path/to/project
mozyo-bridge scaffold apply redmine --target /path/to/project
mozyo-bridge scaffold apply redmine-governed --target /path/to/project
mozyo-bridge scaffold apply redmine-rails --target /path/to/project
mozyo-bridge scaffold apply none --target /path/to/project
```

> v0.3: 旧 `scaffold` の `rules` subcommand は廃止されました。生成は `scaffold apply`、差分確認は `scaffold diff` を使います。互換 alias はありません。

When `--target` or `--repo` is omitted, scaffold writes to the current working directory. Use an explicit target to scaffold a different directory.

This creates:

- `AGENTS.md`
- `CLAUDE.md`
- `.mozyo-bridge/scaffold.json`

The generated routers point to `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md`. They do not copy the full development flow into each repository.

Preview the diff before applying:

```bash
mozyo-bridge scaffold diff asana --target /path/to/project
```

`scaffold diff` prints a unified diff of what `scaffold apply` would write. Exit code `0` when the workspace already matches the rendered output, `1` when at least one file would change. Standard UX is `diff -> apply`.

Existing `AGENTS.md` or `CLAUDE.md` files are not overwritten by default. Use `--dry-run` on `scaffold apply` to print the would-write paths, `--backup` to replace with backups, or `--force` to replace without backups.

### Project-Local Additions (preserved across re-sync)

Scaffold-generated `AGENTS.md` / `CLAUDE.md` ships a marker pair:

```
<!-- mozyo-bridge:project-local-additions:begin -->
... ここに project-local layer を書く ...
<!-- mozyo-bridge:project-local-additions:end -->
```

`mozyo-bridge scaffold apply` と `mozyo-bridge scaffold diff` は、target repo の AGENTS.md / CLAUDE.md にこのマーカー pair が含まれている場合、マーカー間の本文を **rendered template 側に substitute** します。つまり、operator がマーカー間に書いた project-local layer (Rails / Ruby version、Presenter / YAML 慣習、read-only documentation directory、DB / test 必須環境変数、docs catalog / active-doc resolver / nagger ガバナンス、role-boundary override、private internal tooling など) は scaffold re-sync で消えません。マーカーの **外** の内容は scaffold base の正本扱いで、再生成で上書きされます。

re-sync の標準フロー:

1. 初回 `mozyo-bridge scaffold apply <preset> --target /path/to/repo` 後、AGENTS.md / CLAUDE.md の Project-Local Additions マーカー間に project-local layer 本文を書く。
2. 以降の re-sync では `mozyo-bridge scaffold diff <preset> --target /path/to/repo` を走らせる。マーカー間に書いた project-local 追記は rendered 側に保持されるため、diff は scaffold base 側の更新点 (preset version label、generator 行、preset 本文の参照など) だけを表示する。
3. `mozyo-bridge scaffold apply <preset> --target /path/to/repo --backup` で apply する。`--backup` は安全網として古い AGENTS.md / CLAUDE.md を `AGENTS.md.bak.<timestamp>` に退避する。マーカー preservation は同じく適用される。
4. `--force` も marker preservation の対象。バックアップ無しの上書きでも、マーカー *内* の project-local 追記は保持される。マーカー *外* に書いた追記 (古い scaffold で marker pair が無い時代の追記) は上書きされる。
5. legacy scaffold (router にマーカー pair が無い古い AGENTS.md / CLAUDE.md) は preservation 対象外。再生成前にマーカー pair を含む新しい router へ移行し、project-local layer をマーカー *内* に移すと、以降の re-sync は (3)–(4) のとおり機械的に保持される。

`redmine-rails` preset には `Project-Local Layer (do not erase on scaffold apply)` / `Project-Local Layer Apply Discipline` / `Active-Doc Resolver Concept` / `Dangerous DB / Test Command Category` / `Presenter / YAML / Doc-Readonly Category` / `Project Tooling / Local Skill / Role-Boundary Override Category` セクションがあり、project-local layer に何を書くべきかをカテゴリで列挙しています。具体的な path / command / 環境変数は preset 側に焼き込まれないので、別 repo に apply しても誤誘導になりません。

#### `redmine-governed`: non-Rails guardrail governance package

`redmine-governed` preset は `redmine` を extends する full governance 向けの opt-in preset です。Rails 固有の app path、DB command、Presenter / YAML 慣習を含めず、Redmine Issue / Journal gate、docs catalog skeleton、LLM rule authoring、Claude Nagger skeleton、tmux UI artifact を配布します。

```bash
mozyo-bridge scaffold apply redmine-governed --target /path/to/repo
```

非 Rails project で guardrail docs / catalog / runtime guardrail artifact まで導入したい場合は、`redmine-rails-governed` ではなくこちらを使います。

#### `redmine-rails-governed`: full guardrail governance package

`redmine-rails-governed` preset は `redmine-rails` を extends する full governance 向けの opt-in preset です。`redmine-rails` の薄い preset では project-local layer に書くことが推奨されていた強い文言を preset 側で正本にし、scaffold 時に target repo の `.mozyo-bridge/` 配下に repo-local rules / catalog skeleton / runtime guardrail artifacts を配布します。docs catalog tooling は target repo に Python source として置かず、`mozyo-bridge docs ...` CLI として package 側から実行します。

gate schema、agent role、Codex direct edit gate、完了条件は `redmine-rails-governed/agent-workflow.md` 自体を正本にします。AGENTS.md / CLAUDE.md が読む入口と実行契約を分けないことで、LLM が読むべき正本を 1 本に保ちます。

```bash
mozyo-bridge scaffold apply redmine-rails-governed --target /path/to/repo
```

apply すると、router 一式に加えて以下が target repo に書き込まれます。

- `.mozyo-bridge/rules/llm_rule_authoring.md` — LLM 向け規約文書の作成・分離・構造化の正本。
- `.mozyo-bridge/rules/docs_catalog_governance.yaml` — docs catalog、resolver、generator、impact check の統治規約。
- `.mozyo-bridge/docs/catalog.yaml.example` — 初期 catalog skeleton。target 側で `catalog.yaml` にコピーして埋める。
- docs catalog tooling は `mozyo-bridge` package に同梱されており、`mozyo-bridge docs validate / resolve / generate-file-conventions / audit-impact` で呼び出します。target repo には Python source を vendor copy しません。

これらは scaffold preset 側を正本とし、`mozyo-bridge scaffold status` が drift を検出します。target 側で個別に編集したい場合は preset へ upstream し、`mozyo-bridge scaffold apply --backup` で再配布してください。configured catalog (`catalog.yaml`) は scaffold が触らないため、project 固有 docs / file_conventions を埋めても上書きされません。`mozyo-bridge scaffold apply --backup` は shipped artifacts も含めて pre-existing files を `.bak.<timestamp>` に退避します。

catalog には任意 field `coverage_roots` を定義できます。指定した repo-relative path が `mozyo-bridge docs validate --check-file-coverage` の走査対象になり、CLI `--coverage-root` が無い場合の default として使われます。CLI が指定されたときは CLI 側が優先されます。missing root は `notice:` として印字され exit code には影響しません。`scaffold status` の出力では manifest 追跡対象を `tracked files:` セクションで表示します (router 2 件 + governed が追加した repo-local artifacts も同じセクションに並びます)。

scaffold には **Claude Nagger 設定 skeleton** (`.claude-nagger/{config,command_conventions,mcp_conventions}.yaml.example` + `.gitignore`) と **tmux agent window 用の UI snippet** (`.mozyo-bridge/tmux/agent-ui.conf`) も default-on で同梱されます。これらは agent 誤動作を減らすための実行時 guardrail として扱い、`doctor` の `claude_nagger` セクションと `tmux.artifact` 行で導入状態を確認できます。Claude Nagger を運用するには `.claude-nagger/config.yaml.example` を `config.yaml` にコピーして customise してください。tmux UI を有効化したい operator は `source-file <repo>/.mozyo-bridge/tmux/agent-ui.conf` を `~/.tmux.conf` などに追記します (scaffold は host 設定を一切変更しません)。導入したくない project は `scaffold apply --skip-nagger` / `--skip-tmux-ui` で opt-out できます。スキップした category は manifest にも記録されず、`scaffold status` は引き続き clean を返します。

`redmine-rails` を選んだ project が後から full governance に乗り換える場合は、`mozyo-bridge scaffold apply redmine-rails-governed --target . --backup` で切り替えられます。


After upgrading mozyo-bridge (e.g. `pipx upgrade mozyo-bridge && mozyo-bridge rules install`), check each scaffolded project for drift:

```bash
mozyo-bridge scaffold status                       # implicit target = cwd
mozyo-bridge scaffold status --target /path/to/proj
mozyo-bridge scaffold status --target /path/to/proj --json
```

The command compares the project's `.mozyo-bridge/scaffold.json` against the installed central preset (content hash, not only the version label) and the on-disk `AGENTS.md` / `CLAUDE.md`. Exit code is non-zero when central preset content drifted, when a router was modified locally, when the central preset is missing, or when the manifest is missing. Use `mozyo-bridge scaffold apply <preset> --backup` to regenerate routers and accept the new central preset content.

### Dev Container / ephemeral home: repo-local rules mode

Dev Container, Codespace, and similar workspaces do not persist `~/.mozyo_bridge` across container rebuilds, which leaves agents without a guardrail store the first time they start a new session. The repo-local mode keeps the preset inside the target repo so agents can read it without a persistent user home:

```bash
# 1. Install the preset into <repo>/.mozyo-bridge/rules/presets/<preset>/
mozyo-bridge rules install --repo-local /path/to/repo
mozyo-bridge rules status  --repo-local /path/to/repo

# 2. Scaffold routers + manifest in repo-local mode.
mozyo-bridge scaffold apply <preset> --target /path/to/repo --repo-local
mozyo-bridge scaffold diff  <preset> --target /path/to/repo --repo-local

# 3. Status auto-detects the mode from .mozyo-bridge/scaffold.json.
mozyo-bridge scaffold status --target /path/to/repo
```

In repo-local mode the generated routers point at the repo-relative path `.mozyo-bridge/rules/presets/<preset>/agent-workflow.md` (no `${MOZYO_BRIDGE_HOME:...}` expansion needed) and the manifest records `mode: "repo-local"`. `--home` and `--repo-local` are mutually exclusive on every command that accepts both; passing both is rejected before any filesystem work. Default behavior without `--repo-local` is unchanged: central mode under `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` and `mode: "central"` in the manifest. Switching a repo between modes requires re-running both `rules install` and `scaffold apply` under the new mode so the store and the manifest stay aligned.

Detailed scaffold semantics (preset registry, manifest, apply/diff/status behavior) live in `vibes/docs/logics/scaffold-rules.md`.

## Notification Commands

agent 間 handoff / reply の **standard path は高レベル primitive** `mozyo-bridge handoff send` / `mozyo-bridge handoff reply` (上位 alias `mozyo-bridge reply`) です。primitive が receiver pane resolve / deterministic Layer B preflight / marker-prefixed notification の typing / Enter 発行 (`--mode queue-enter` default、`--mode standard` strict fallback、`--mode pending` で typing のみ) をまとめて行います。caller は `mozyo-bridge read` + `mozyo-bridge message` の shell-level 組み立てを行いません。

Asana driven のレビュー依頼:

```bash
mozyo-bridge handoff send \
  --to codex \
  --source asana \
  --task-id 1214760548032221 \
  --comment-id 1214890105221452 \
  --kind review_request \
  --summary "branch X の review 依頼"
```

Asana driven の reply (kind は省略すると `reply`):

```bash
mozyo-bridge handoff reply \
  --to claude \
  --source asana \
  --task-id 1214760548032221 \
  --comment-id 1214890105221452 \
  --summary "audit OK"

# 上位 alias
mozyo-bridge reply \
  --to claude \
  --source asana \
  --task-id 1214760548032221 \
  --comment-id 1214890105221452
```

Redmine driven の review handoff:

```bash
mozyo-bridge handoff send \
  --to codex \
  --source redmine \
  --issue 9020 \
  --journal 46005 \
  --kind review_request \
  --summary "commit f7b0398dc"
```

`notify-*` wrappers は **`handoff send` を内部 routing する Redmine 互換 entrypoint** として残ります。新規 caller は handoff primitive を直接使うほうが durable record に明示的な `--kind` ラベルが残るため推奨ですが、既存運用は引き続き動作します。

```bash
# Redmine 互換 wrapper (内部で handoff primitive を呼ぶ)
mozyo-bridge notify-codex-review \
  --issue 9020 \
  --journal 46005 \
  --commit f7b0398dc

mozyo-bridge notify-claude-review-result \
  --issue 9020 \
  --journal 46007 \
  --commit f7b0398dc

mozyo-bridge notify-codex \
  --issue 9020 \
  --journal 46005 \
  --type design_consultation

mozyo-bridge notify-claude \
  --issue 9020 \
  --journal 46007 \
  --type design_consultation_result
```

`notify-*-legacy-task` (`.agent_handoff/tasks.yaml` queue 経由) は retired-queue cleanup wrapper であり、handoff primitive を経由しません。新規 notification には使いません。

## Safety

- Redmine journal を必ず先に作る。
- `notify-*` には `--issue` と `--journal` を渡す。
- pane message の内容だけで作業開始・完了判断をしない。
- 受信側は通知を見たら Redmine gate を確認してから動く。
- agent pane handoff (`mozyo-bridge handoff send` / `handoff reply` / `notify-*` 標準 variants で Claude / Codex pane を target にする送信) の **v0.4 normative default は `--mode queue-enter`**。Claude / Codex agent pane 限定で、`--force` 不可。typing 前に deterministic preflight が走り、いずれかが false なら `send-keys -l` を発行する前に `blocked` で die する:
  - explicit `--target` を渡す場合は receiver 自身の tmux window 配下のみ許容 (`Reason: invalid_args`)
  - target pane は **sender と同じ tmux session** にあること (= mozyo session 内から実行) (`Reason: invalid_args`)
  - target pane は所属 window の **active split** であること、**または** standard_target_admission (Redmine #12597) を満たすこと。登録済み inactive split が minimal admission contract (live pane / strong role match / `workspace_id` present / unambiguous) を満たせば、rail が `tmux select-pane` で active 化して delivery する (pane selection のみ。raw `send-keys` / `paste-buffer` / low-level `type` / `keys` は recovery に使わない)。admission 不通過 (例: `workspace_id` 無し) または `--no-target-activation` では従来通り `Reason: invalid_args` で fail-closed し strict-rail recovery を返す
  - foreground process が receiver の allowlist にマッチすること (`Reason: target_not_agent`): literal `claude` (receiver=`claude`) / literal `codex` (receiver=`codex`) は **strong identity**、literal `node` および versioned native binary basename は Claude Code / Codex CLI どちらも採るため **weak identity** (両 receiver で admit、cross-binding 防御は window-name binding + operator 規律に retreat)。
  すべて pass した場合、marker 観測ありで `sent` / `ok`、marker 未観測でも Enter を発行して `sent` / `queue_enter` を durable record に残す。default rail の promise は **strong preflight 付き practical queued submission** であり、confirmed landing ではない。受信側は引き続き Asana task comment / Redmine journal を正本として読む。
- **strict explicit fallback** は `mozyo-bridge handoff send --mode standard` (および `mozyo-bridge message --submit` 標準動作)。短い marker を送信文へ付与し、target pane 上で marker を確認できた場合だけ Enter を送る。確認できない場合は入力欄を `C-u` で消し、Enter を送らず `blocked` / `marker_timeout` で失敗する (fail-closed)。strict landing observation が必要な送信 (regression check / brand-new pane で queue-pickup 確率が未確認 / observability test / 厳格な landing evidence が監査要件) または default scope 外 (`mozyo-bridge message` / non-agent pane) のときに明示的に選ぶ。v0.4 で default ではなくなったが contract からは削除しない。挙動は v0.1 以降一切変更しない。
- どちらの rail を使った場合でも durable record (Asana task comment / Redmine journal) が正本。pane notification は pointer。詳細・state machine 全体・例外条件は `vibes/docs/logics/tmux-send-safety-contract.md` の `## Default Delivery Promise (v0.4)` / `## Queue-Enter Default Rail` 節を参照。

## Legacy Queue

通常運用では以下を使いません。

- `read-next --wait`
- Stop hook による handoff queue 待機
- `notify-* --task-id`

`.agent_handoff/tasks.json` は retired queue の棚卸し用であり、standard notification fallback ではありません。退役前 queue の棚卸しだけ、専用コマンドを使います。

```bash
mozyo-bridge notify-codex-legacy-task \
  --issue 9020 \
  --task-id legacy-task \
  --type review_request
```

## Utility Commands

`mozyo-bridge read` / `message` / `type` / `keys` は **operator / debug 用の低レベル primitive** です。standard な agent 間 handoff / reply の代替には使いません (それは上の "Notification Commands" の `mozyo-bridge handoff send/reply` を使います)。`status` / `doctor` の出力は durable Asana / Redmine anchor が利用可能なときに receiver state / task state の推測 source として使いません — anchor を直接読みます。

pane の内容を読む (operator inspection):

```bash
mozyo-bridge read codex 30
```

ad-hoc operator 会話を送る (handoff/reply ではない):

```bash
mozyo-bridge message codex '確認してください'
```

診断:

```bash
mozyo-bridge doctor
```

`message` / `keys` は送信前に `read` が必要です。これは誤送信を減らすためのガードです。これらは正規 handoff/reply 経路ではないため、`mozyo-bridge handoff send` / `handoff reply` のような durable anchor を引数化する structured outcome は emit しません。例外: per-preset Retry Path Checklist で operator/debug fallback として明記された `--no-submit` retry path のみ、`mozyo-bridge read <agent>` + `mozyo-bridge message <agent> "<resubmit text>" --no-submit --attempt N` を operator 経路として使えます (per-preset cap 3 回)。

## Documentation Map

- `README.md`: install/bootstrap entrypoint, core commands, and safety summary. Start here; run `doctor` + `runtime-config check` first.
- `vibes/docs/logics/bootstrap.md`: detailed LLM-first stage-order reference, FAQ, and troubleshooting. Strict stage order from a clean machine through a verified scaffold (install → rules → skill → scaffold → doctor → isolated smoke), plus `runtime-config check` failure recovery. Follow it from the README Quick Start when a step fails or you need the full sequence — it is no longer the first doc to read.
- `vibes/docs/rules/agent-workflow.md`: AI agent work rules for this repository.
- `vibes/docs/specs/project-map.md`: repository structure and source-of-truth routing.
- `vibes/docs/logics/skill-distribution.md`: Claude/Codex skill layout and install logic.
- `vibes/docs/logics/scaffold-rules.md`: scaffold preset registry, manifest contract, and YAML registry governance (CLI surface is `scaffold apply` / `scaffold diff` / `scaffold status`).
- `vibes/docs/logics/release-flow.md`: release and verification gates.
- `vibes/docs/logics/turnkey-e2e-acceptance.md`: final destructive acceptance test using a published TestPyPI / PyPI install. Separate from `Beta Tester Install` smoke and run only on a clean git worktree.
- `skills/mozyo-bridge-agent/references/`: compact runtime references consumed by the shared agent skill.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

実 tmux を使う smoke test:

```bash
python3 smoke/real_tmux_notify_smoke.py
```

smoke test はローカル tmux server に依存するため、通常の unit test には含めません。

Use `vibes/docs/logics/release-flow.md` for the full release verification route.

## Release

Build locally:

```bash
python3 -m pip install build
python3 -m build
```

Publishing is intended to run through GitHub Actions and PyPI Trusted Publishing.
The local `.env` / `.pypirc` path should only be used for temporary release rehearsal, not as the normal production publishing path.

## License

MIT
