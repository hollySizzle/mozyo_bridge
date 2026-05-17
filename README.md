# mozyo-bridge

`mozyo-bridge` は ClaudeCode / Codex の tmux pane に Redmine journal id を通知するための小さな bridge です。

正本は Redmine です。`mozyo-bridge` は通知 transport であり、レビュー依頼・監査結果・完了判断の正本にはなりません。

`mozyo-bridge` is a small CLI that sends Redmine-gated notifications to Claude Code / Codex tmux panes.
It is only a notification transport. Redmine remains the source of truth for review requests, audit results, and completion decisions.

## Quick Start

> First-time install + project bootstrap, executed end-to-end by Claude / Codex agents,
> is documented in `vibes/docs/logics/bootstrap.md`. Read that doc to follow the
> install → rules → skill → scaffold → doctor stages in strict order. The sections
> below are operator-facing reference material for the individual commands.

Install the CLI:

```bash
pipx install mozyo-bridge
```

Alternative install path:

```bash
python3 -m pip install mozyo-bridge
python3 -m mozyo_bridge --help
```

System dependency:

- `tmux` must be installed and available on `PATH`.
- Use `mozyo-bridge doctor` to check CLI, central rules, agent skills, scaffold, and tmux readiness in one command. Add `--target <project>` to also verify a scaffolded project. See `Beta Tester Install (GitHub main)` for the full acceptance smoke.

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
- session 名を repo basename にして、無ければ作る
- 1 つの repo-scoped session の中に `claude` window と `codex` window を ensure (window 別に分離)
- `claude` window を default にしてから attach

Window 分離なので、ある時点で画面に出るのは 1 agent の window だけです。tmux は同じ client 内で複数 window を同時表示する仕組みではないため、agent 同士の切り替えは `prefix + n` / `prefix + p` などの通常の window 操作で行います。

attach せずに session / window だけ用意したい場合:

```bash
mozyo --no-attach
```

session 名が同じでも repo root の下に pane が 1 つも無い場合 (= 別 project の session が同名で居る場合) は、誤 attach を避けるためにエラーで止まります。明示的に session 名を分離する場合は `mozyo --session NAME` で session 名を上書きするか、bare `mozyo --repo /path/to/another` で別 repo root を指定してください。

`open-here` / `tmux-ui-open` / `tmux-ui-setup` / `tmux-ui-ensure-pair` / `tmux-ui-ensure` / `tmux-ui-spawn` の pane-split 系 subcommand は廃止されました。標準導線は bare `mozyo` (1 repo = 1 session, 1 agent = 1 window) です。既存の標準 tmux pane や VS Code `tmux-integrated` pane を agent target にしたい場合は、その pane の中で `mozyo-bridge init <agent>` を実行して window 名を `<agent>` に rename してください。

## Beta Tester Install (GitHub main)

PyPI release 前の beta tester 向け手順です。`Quick Start` の PyPI install とは別経路で、GitHub `main` の最新 commit を直接 install します。`mozyo-bridge --version` が表示する package version 文字列は `pyproject.toml` の値なので、PyPI release と GitHub `main` で同じ string になる場合があります。実体差は新規 sub-command (例: `mozyo-bridge scaffold status --help` / `mozyo-bridge doctor --json`) や、`mozyo-bridge rules install` が配布する preset 内容で確認してください。

PyPI / TestPyPI release の検証手順は本節と同じ acceptance smoke を、GitHub `main` install のかわりに該当 PyPI install で実行してください。release 経路の詳細は `vibes/docs/logics/release-flow.md` を見ます。

### Isolation principle

`mozyo-bridge scaffold rules <preset>` は対象 directory の `AGENTS.md` / `CLAUDE.md` を生成 / 上書きします。本 repository (`mozyo_bridge` 自身) の tracked router を壊さないために、検証は必ず以下のどちらかで行います。

- `./tmp/mb-smoke-asana` / `./tmp/mb-smoke-redmine` のような isolated target を使う (`./tmp/` は `.gitignore` 配下の作業領域)。
- もしくは別 directory で `git clone` した fresh checkout、または任意の `/tmp/...` directory を使う。

本 repo の working tree で `mozyo-bridge scaffold rules <preset>` を `--target` 無しで実行しないでください。tracked `AGENTS.md` / `CLAUDE.md` が上書き候補になり、`scaffold rules` 自体は default で既存ファイルを保護しますが、`--force` / `--backup` を伴うと取り違える可能性があります。

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

   `rules status` は `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/` に展開された user-global 規約 (`asana` / `redmine` / `none`) の状態を表示します。

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

   このタイミングでは `scaffold` section が `missing` (`-> mozyo-bridge scaffold rules <asana|redmine|none> --target ...`) になりますが、`cli` / `rules` / `codex_skill` の 3 section が ok であることを確認します。`next_action` (`-> ...`) を読み、不足があれば該当 install / set up を再実行してください。

   `claude_skill` section の扱い:

   - primary path (plugin marketplace) でだけ install した場合、`claude_skill: missing` が出ます。これは現時点の `mozyo-bridge doctor` が `~/.claude/skills/` と `<project>/.claude/skills/` の legacy directory だけを scan し、plugin cache (`~/.claude/plugins/cache/`) を見ないためです。失敗ではなく期待状態として扱ってください。primary path の確認は上の `claude plugin list` で行います。
   - fallback path (`scripts/install_claude_skill.sh`) を併用した場合は legacy directory にも skill が入るので `claude_skill: ok` が出ます。同名 skill が plugin と legacy 両方に居る状況は precedence の落とし穴 (`Agent Skill Install` 節) で扱います。

6. isolated target に対して Asana / Redmine の repo-local scaffold を smoke:

   ```bash
   mkdir -p ./tmp/mb-smoke-asana
   mozyo-bridge scaffold rules asana --target ./tmp/mb-smoke-asana
   mozyo-bridge scaffold status --target ./tmp/mb-smoke-asana
   mozyo-bridge doctor --target ./tmp/mb-smoke-asana

   mkdir -p ./tmp/mb-smoke-redmine
   mozyo-bridge scaffold rules redmine --target ./tmp/mb-smoke-redmine
   mozyo-bridge scaffold status --target ./tmp/mb-smoke-redmine
   mozyo-bridge doctor --target ./tmp/mb-smoke-redmine
   ```

   各 target で `scaffold status` が `result: clean` を返し、`mozyo-bridge doctor --target ...` の `scaffold` section が `ok` であれば、user-global 規約・repo-local routers・manifest が整合しています。両 preset (Asana と Redmine) を両方確認します。片側だけで完了させると preset 間 boundary の検証が落ちます。

7. CI / 機械的な acceptance smoke では `--json` を使います:

   ```bash
   mozyo-bridge doctor --target ./tmp/mb-smoke-asana --json
   ```

   出力は `{"ok": <bool>, "sections": {"cli": {...}, "rules": {...}, "codex_skill": {...}, "claude_skill": {...}, "scaffold": {...}, "tmux": {...}}}` 形式で、`jq '.sections.scaffold.status == "ok"'` 等で gate を組めます。exit code は `ok` が false の時に非ゼロです。primary path (plugin marketplace) でだけ install した CI は `jq '.sections.claude_skill.status'` で gate を組まないでください (上記 step 5 の通り `missing` が期待状態)。

`mozyo-bridge rules status` (user-global 規約の install 状態) と `mozyo-bridge scaffold status` (repo-local manifest drift) は別責務です。前者は host 全体、後者は 1 つの scaffold 済 project を見ます。`mozyo-bridge doctor` は両者と CLI / Codex skill / (legacy) Claude skill / tmux を 1 command で見る 6-section diagnostic です。Claude 側の primary path (plugin marketplace) は doctor の scan 対象外なので、Claude install の最終確認は `claude plugin list` を使います。詳細は次の logic docs を正本にしてください。

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

### Fallback: curl-based install scripts (legacy)

`scripts/install_codex_skill.sh` and `scripts/install_claude_skill.sh` remain for environments where the recommended paths above are not available (offline mirrors, internal forks, fresh-tester acceptance smoke). They are not the primary path, and bootstrap should not use them.

```bash
# Codex skill (fallback)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh

# Claude Code skill (fallback, user-global / personal)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh
```

`MOZYO_BRIDGE_CLAUDE_SCOPE=global curl ... | sh` の形は env var が `curl` にしか渡らないため、pipe の右側で `sh` の直前に env を置く形を使ってください。両方に配布したい場合は、明確な意図のもとで script を二度実行します (`scope=project` と `scope=global` を順に)。

**Claude Code は同名 skill について personal/user skill (`~/.claude/skills/`) を project skill (`<project>/.claude/skills/`) より優先します** (公式 docs: <https://code.claude.com/docs/en/skills>)。多くの開発ツールと逆向きの慣習なので注意してください。Plugin skills は `plugin-name:skill-name` で namespace 分離されるため、personal / project skill とは衝突しません。

Install destinations (fallback scripts):

- `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/` (Codex user-global)
- `${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}/skills/mozyo-bridge-agent/` (Claude user/personal, scope=global)
- `${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}/.claude/skills/mozyo-bridge-agent/` (Claude project adapter, scope=project)
- `${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}/skills/mozyo-bridge-agent/` (Claude project shared body, scope=project)

Both fallback scripts fetch `hollySizzle/mozyo_bridge` `main` by default. Override the source with `MOZYO_BRIDGE_SKILL_REPO`, `MOZYO_BRIDGE_SKILL_REF`, or `MOZYO_BRIDGE_SKILL_ARCHIVE_URL` (the last accepts any tarball URL, including `file:///...` for local-checkout install).

Detailed distribution rules live in `vibes/docs/logics/skill-distribution.md`.

## Agent Rules Scaffold

`mozyo-bridge` can install ticket-system-specific development flow rules and scaffold thin project routers for Claude Code and Codex.

Install the central rules store:

```bash
mozyo-bridge rules install
mozyo-bridge rules status
```

Scaffold project routers:

```bash
mozyo-bridge scaffold rules asana
mozyo-bridge scaffold rules asana --target /path/to/project
mozyo-bridge scaffold rules redmine --target /path/to/project
mozyo-bridge scaffold rules none --target /path/to/project
```

When `--target` or `--repo` is omitted, scaffold writes to the current working directory. Use an explicit target to scaffold a different directory.

This creates:

- `AGENTS.md`
- `CLAUDE.md`
- `.mozyo-bridge/scaffold.json`

The generated routers point to `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/agent-workflow.md`. They do not copy the full development flow into each repository.

Existing `AGENTS.md` or `CLAUDE.md` files are not overwritten by default. Use `--dry-run` to preview, `--backup` to replace with backups, or `--force` to replace without backups.

After upgrading mozyo-bridge (e.g. `pipx upgrade mozyo-bridge && mozyo-bridge rules install`), check each scaffolded project for drift:

```bash
mozyo-bridge scaffold status                       # implicit target = cwd
mozyo-bridge scaffold status --target /path/to/proj
mozyo-bridge scaffold status --target /path/to/proj --json
```

The command compares the project's `.mozyo-bridge/scaffold.json` against the installed central preset (content hash, not only the version label) and the on-disk `AGENTS.md` / `CLAUDE.md`. Exit code is non-zero when central preset content drifted, when a router was modified locally, when the central preset is missing, or when the manifest is missing. Use `mozyo-bridge scaffold rules <preset> --backup` to regenerate routers and accept the new central preset content.

Detailed scaffold rules live in `vibes/docs/logics/scaffold-rules.md`.

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
  - target pane は所属 window の **active split** であること (`Reason: invalid_args`)
  - foreground process が receiver の allowlist にマッチすること (`Reason: target_not_agent`): literal `claude` (receiver=`claude`) / literal `codex` (receiver=`codex`) は **strong identity**、literal `node` および versioned native binary basename は Claude Code / Codex CLI どちらも採るため **weak identity** (両 receiver で admit、cross-binding 防御は window-name binding + operator 規律に retreat)。
  すべて pass した場合、marker 観測ありで `sent` / `ok`、marker 未観測でも Enter を発行して `sent` / `queue_enter` を durable record に残す。default rail の promise は **strong preflight 付き practical queued submission** であり、confirmed landing ではない。受信側は引き続き Asana task comment / Redmine journal を正本として読む。
- **strict explicit fallback** は `mozyo-bridge handoff send --mode standard` (および `mozyo-bridge message --submit` 標準動作)。短い marker を送信文へ付与し、target pane 上で marker を確認できた場合だけ Enter を送る。確認できない場合は入力欄を `C-u` で消し、Enter を送らず `blocked` / `marker_timeout` で失敗する (fail-closed)。strict landing observation が必要な送信 (regression check / brand-new pane で queue-pickup 確率が未確認 / observability test / 厳格な landing evidence が監査要件) または default scope 外 (`mozyo-bridge message` / non-agent pane) のときに明示的に選ぶ。v0.4 で default ではなくなったが contract からは削除しない。挙動は v0.1 以降一切変更しない。
- どちらの rail を使った場合でも durable record (Asana task comment / Redmine journal) が正本。pane notification は pointer。詳細・transient gap (CLI binary default flip と contract default の整合) は `vibes/docs/logics/tmux-send-safety-contract.md` の `## Default Delivery Promise (v0.4)` / `## Queue-Enter Default Rail` 節を参照。

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

- `README.md`: user-facing install, core commands, and safety summary.
- `vibes/docs/logics/bootstrap.md`: canonical LLM-first bootstrap guide. Strict stage order from a clean machine through a verified scaffold (install → rules → skill → scaffold → doctor → isolated smoke). Read this first for end-to-end setup.
- `vibes/docs/rules/agent-workflow.md`: AI agent work rules for this repository.
- `vibes/docs/specs/project-map.md`: repository structure and source-of-truth routing.
- `vibes/docs/logics/skill-distribution.md`: Claude/Codex skill layout and install logic.
- `vibes/docs/logics/scaffold-rules.md`: scaffold rules presets for Asana, Redmine, and no-ticket projects.
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
