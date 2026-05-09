# mozyo-bridge

`mozyo-bridge` は ClaudeCode / Codex の tmux pane に Redmine journal id を通知するための小さな bridge です。

正本は Redmine です。`mozyo-bridge` は通知 transport であり、レビュー依頼・監査結果・完了判断の正本にはなりません。

## 標準コマンド

正式な実行パス:

```bash
mozyo-bridge <command>
```

短い alias:

```bash
mozyo <command>
```

通常運用では正式パスを docs / Redmine に書き、手元の操作では alias を使っても構いません。

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

## 使い方

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

ClaudeCode から Codex へレビュー依頼を通知:

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

## 守ること

- Redmine journal を必ず先に作る。
- `notify-*` には `--issue` と `--journal` を渡す。
- pane message の内容だけで作業開始・完了判断をしない。
- 受信側は通知を見たら Redmine gate を確認してから動く。
- `.agent_handoff/tasks.yaml` は retired queue であり fallback ではない。
- `notify-*` は短い `[mozyo:notify:...]` marker を送信文へ付与し、target pane 上で marker を確認できた場合だけ Enter を送る。
- marker を確認できない場合、mozyo-bridge は入力欄を `C-u` で消し、Enter を送らず失敗する。

## 使わないこと

通常運用では以下を使いません。

- `read-next --wait`
- Stop hook による handoff queue 待機
- `notify-* --task-id`
- `tmux-ui-*` による自動 pane 作成

退役前 queue の棚卸しだけ、専用コマンドを使います。

```bash
mozyo-bridge notify-codex-legacy-task \
  --issue 9020 \
  --task-id legacy-task \
  --type review_request
```

## 補助コマンド

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

## tmux-ui 系

`tmux-ui-open` / `tmux-ui-setup` / `tmux-ui-ensure` / `tmux-ui-spawn` は、tmux UI を直接作る環境向けの補助です。

VS Code `tmux-integrated` を標準運用にしている場合は使いません。pane が見つからない場合は、人間が terminal を開き、対象 agent を起動してから `init` してください。

## テスト

```bash
python3 -m unittest discover -s tests -v
```

実 tmux を使う smoke test:

```bash
python3 smoke/real_tmux_notify_smoke.py
```

smoke test はローカル tmux server に依存するため、通常の unit test には含めません。

## License

MIT
