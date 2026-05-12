# mozyo-bridge

`mozyo-bridge` は ClaudeCode / Codex の tmux pane に Redmine journal id を通知するための小さな bridge です。

正本は Redmine です。`mozyo-bridge` は通知 transport であり、レビュー依頼・監査結果・完了判断の正本にはなりません。

`mozyo-bridge` is a small CLI that sends Redmine-gated notifications to Claude Code / Codex tmux panes.
It is only a notification transport. Redmine remains the source of truth for review requests, audit results, and completion decisions.

## Quick Start

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

   # Codex: $skill-installer で canonical GitHub skill path を指定
   # canonical path: https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent
   ```

   Claude Code 側は `plugins/mozyo-bridge-agent/` (`.claude-plugin/plugin.json`) を marketplace 経由で取得します。Codex 側は canonical `skills/mozyo-bridge-agent/` を `$skill-installer` で同期します。`plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` は `scripts/sync_plugin_skill.sh` が canonical から mirror した copy で、drift は unit test で検出します。

   curl/script による install は fallback / local smoke 用途のみで、primary path にはしません。詳細と precedence の落とし穴 (Claude Code は同名 skill で personal が project を override) は `Agent Skill Install` 節と `vibes/docs/logics/skill-distribution.md` を参照してください。

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
MOZYO_REPO=/path/to/repo mozyo-bridge tmux-ui-open
```

`--cwd` を省略した tmux-ui 系コマンドは、解決した project root を作業ディレクトリとして使います。
`.tmux.conf` は project root にあればそれを使い、なければ `~/.config/mozyo-bridge/tmux.conf` を見ます。

### `open-here` (repo-aware sugar)

`mozyo-bridge open-here` は repo root をそのまま session/cwd に当てる sugar command です。`tmux-ui-open --session <repo名> --cwd <repo root>` を毎回手で打つ運用を 1 行に縮めます。

```bash
cd /path/to/your-repo
mozyo-bridge open-here
```

挙動:

- repo root は `--repo` → `MOZYO_REPO` → `cwd` から `.git` / `.tmux.conf` / `pyproject.toml` を遡って解決します (`Project Root Resolution` と同じ規約)。
- session 名は repo root basename を default にします。
- `--cwd` 省略時は repo root をそのまま使います。
- 同名 session が既に存在し、しかも **その session 内のどの pane も repo root の下にいない** 場合は別 project の session の可能性が高いため、自動 attach せずにエラーで止まります。明示的に `--session <name>` を指定し直してください。
- 既に同名 session があっても少なくとも 1 つの pane が repo root 配下にあれば、その session を `tmux-ui-open` と同じ手順で起動し attach します。

```bash
# Conflict 例: 同じ basename の session が別の repo を指していた場合
$ cd /path/to/my-project
$ mozyo-bridge open-here
session 'my-project' already exists but its panes are outside repo root ... Re-run with an explicit --session to disambiguate; this command will not auto-attach.

# 解消: 明示 session で別名を切る
$ mozyo-bridge open-here --session my-project-2
```

## Pane Setup

まず ClaudeCode / Codex の terminal を VS Code の `tmux: New tmux Terminal` または `tmux: Attach to tmux Window` で開きます。

各 terminal の中で pane に名前を付けます。

```bash
mozyo-bridge init claude
mozyo-bridge init codex
```

状態確認:

```bash
mozyo-bridge status
```

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

Point Codex `$skill-installer` at the canonical `skills/mozyo-bridge-agent/` directory in `hollySizzle/mozyo_bridge` `main`. The skill body, references, and `agents/openai.yaml` are all there.

### Fallback: curl-based install scripts (legacy)

`scripts/install_codex_skill.sh` and `scripts/install_claude_skill.sh` remain for environments where the recommended paths above are not available (offline mirrors, internal forks, fresh-tester acceptance smoke). They are not the primary path.

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

Claude Code から Codex へレビュー依頼を通知:

```bash
mozyo-bridge notify-codex-review \
  --issue 9020 \
  --journal 46005 \
  --commit f7b0398dc
```

Codex から ClaudeCode へ監査結果を通知:

```bash
mozyo-bridge notify-claude-review-result \
  --issue 9020 \
  --journal 46007 \
  --commit f7b0398dc
```

設計相談など、レビュー以外の journal 通知:

```bash
mozyo-bridge notify-codex \
  --issue 9020 \
  --journal 46005 \
  --type design_consultation

mozyo-bridge notify-claude \
  --issue 9020 \
  --journal 46007 \
  --type design_consultation_result
```

## Safety

- Redmine journal を必ず先に作る。
- `notify-*` には `--issue` と `--journal` を渡す。
- pane message の内容だけで作業開始・完了判断をしない。
- 受信側は通知を見たら Redmine gate を確認してから動く。
- `notify-*` は短い `[mozyo:notify:...]` marker を送信文へ付与し、target pane 上で marker を確認できた場合だけ Enter を送る。
- marker を確認できない場合、mozyo-bridge は入力欄を `C-u` で消し、Enter を送らず失敗する。

## Legacy Queue

通常運用では以下を使いません。

- `read-next --wait`
- Stop hook による handoff queue 待機
- `notify-* --task-id`
- `tmux-ui-*` による自動 pane 作成

`.agent_handoff/tasks.json` は retired queue の棚卸し用であり、standard notification fallback ではありません。退役前 queue の棚卸しだけ、専用コマンドを使います。

```bash
mozyo-bridge notify-codex-legacy-task \
  --issue 9020 \
  --task-id legacy-task \
  --type review_request
```

## Utility Commands

pane の内容を読む:

```bash
mozyo-bridge read codex 30
```

明示的な operator 会話を送る:

```bash
mozyo-bridge message codex '確認してください'
```

診断:

```bash
mozyo-bridge doctor
```

`message` / `keys` は送信前に `read` が必要です。これは誤送信を減らすためのガードです。

## tmux-ui Helpers

`tmux-ui-open` / `tmux-ui-setup` / `tmux-ui-ensure` / `tmux-ui-spawn` は、tmux UI を直接作る環境向けの補助です。

VS Code `tmux-integrated` を標準運用にしている場合は使いません。pane が見つからない場合は、人間が terminal を開き、対象 agent を起動してから `init` してください。

## Documentation Map

- `README.md`: user-facing install, core commands, and safety summary.
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
