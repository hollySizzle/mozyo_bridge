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
- Use `mozyo-bridge doctor` to check the local tmux environment.

Use the full command in docs and durable task records:

```bash
mozyo-bridge <command>
```

The short alias is available for local interactive use:

```bash
mozyo <command>
```

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

Codex skill は Codex home に同期します。

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh
```

Claude Code skill は project skill として対象 project に同期します。別の Mac や別 project に配布する場合は、対象 project path を明示してください。

```bash
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_PROJECT_DIR=/path/to/project \
  sh /tmp/install_mozyo_bridge_claude_skill.sh
```

Install destinations:

- `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/`
- `${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}/.claude/skills/mozyo-bridge-agent/`
- `${MOZYO_BRIDGE_CLAUDE_PROJECT_DIR:-$PWD}/skills/mozyo-bridge-agent/`

現在の project root で実行している場合だけ、`MOZYO_BRIDGE_CLAUDE_PROJECT_DIR` を省略できます。

```bash
sh scripts/install_claude_skill.sh
```

Both scripts fetch `hollySizzle/mozyo_bridge` `main` by default. Override the source with `MOZYO_BRIDGE_SKILL_REPO` and `MOZYO_BRIDGE_SKILL_REF`.
Claude Code must be started from the target project directory for `.claude/skills/` project skills to resolve.

Detailed distribution rules live in `vibes/docs/logics/skill-distribution.md`.

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
- `vibes/docs/logics/release-flow.md`: release and verification gates.
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
