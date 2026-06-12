# 役員ターミナル セットアップ Runbook (iTerm2 + tmux)

Redmine #11638 / #11670。役員 2 名の iTerm2 + tmux (mozyo workspace) 環境を再現可能に立ち上げるための runbook。Git×Drive ADR §3 の再現可能性原則に基づき、口伝ではなく本書を正本とする。事実関係は #11638 journal #56074 の実機訂正 (2026-06-11) を反映済み。

## 前提

- macOS + iTerm2 + Homebrew tmux。
- **tmux >= 3.2** が必須 (`tmux -V` で確認)。Shift+Enter 透過 (`extended-keys`) の要件。
- 対象 workspace は `mozyo-bridge scaffold apply <preset>` 済みで、`.mozyo-bridge/tmux/agent-ui.conf` (preset version 2026.06.11.2 以降) が配布されていること。

## セットアップ手順

1. **tmux UI snippet を host に配線する** (workspace root で):

   ```bash
   mozyo-bridge tmux-ui install
   mozyo-bridge tmux-ui status   # state: installed を確認
   ```

   `~/.tmux.conf` に managed block が入り、`.mozyo-bridge/tmux/agent-ui.conf` が source される。既存 tmux server には `tmux source-file ~/.tmux.conf` か server 再起動で反映する。

2. **iTerm2 のキー設定** (Option/Cmd+Backspace で単語 / 行削除):

   - iTerm2 → Settings → Profiles → Keys → Key Mappings → Presets... → **Natural Text Editing** を適用する。
   - 既存のカスタム key mapping がある profile では Presets 適用が上書きになる点に注意 (役員初期設定では通常問題ない)。

3. **反映確認** (本書末尾の確認表)。

## Shift+Enter (Claude Code の改行) について

実機訂正 (#56074) に基づく事実関係:

- **iTerm2 は Shift+Enter を native support している**。Claude Code の `/terminal-setup` による端末設定は iTerm2 では不要 (setup 対象外)。
- `/terminal-setup` は **tmux 内からは実行できない** (`Terminal setup cannot be run from tmux`)。tmux 外の対応端末では 1 回実行すれば永続するが、iTerm2 では前項のとおり不要。
- iTerm2 + tmux で Shift+Enter が効かない原因は端末ではなく、**tmux が拡張キーシーケンスを透過していない**こと。対応は tmux の `extended-keys` であり、`agent-ui.conf` (2026.06.11.2+) が設定する:

  ```tmux
  set -s extended-keys on
  set -as terminal-features 'xterm*:extkeys'
  ```

- 暫定回避 (tmux < 3.2 や未反映 server): `\` + Enter で改行できる (Claude Code 標準)。

## QoL 機能 (agent-ui.conf 2026.06.11.2+)

- **OS window title**: tmux session 名 (`mozyo-bridge session name` の導出 / 登録名) が iTerm2 の window title に出る (`set-titles on`)。複数 workspace を開いても「全部 tmux」表示にならない。
  - **`tmux -CC` (iTerm2 control mode) の注意**: control mode では iTerm2 が window を native 管理するため、title 反映は iTerm2 側挙動に依存する。反映可否は実機で確認し、結果を該当 Redmine issue に記録する (headless 検証不能、#11671)。
- **Finder ジャンプ**: `<prefix>` `f` で現在 pane の作業 directory を Finder で開く。default の `find-window` を上書きしているため、window 検索が必要な場合は `<prefix>` `:` から `find-window` を使う。

## iTerm2 control mode 起動 (`mozyo --cc`, #11729)

- `mozyo --cc` は通常の `mozyo` と同じく repo session / `claude`・`codex` window を ensure したうえで、attach を `tmux attach` ではなく `tmux -CC attach -t <session>` に置き換える。iTerm2 が tmux window/pane を native window/pane として管理する。
- これまで必要だった `mozyo --no-attach` + 手組みの `tmux -CC attach -t <導出名>` が不要になる。owner 運用の `mozyo` 一発に乗る。
- ensure 系挙動 (session 導出 / legacy collision guard / env 注入 / workspace identity / window 構成) は通常 `mozyo` と不変。`--cc` は attach 形だけを変える。
- **フラグの優先順位**: `--no-attach` と `--json` は `--cc` より優先する。どちらかが付くと exec せず ensure のみで、表示 / JSON の attach コマンドが `-CC` 版を指すだけになる (silent attach はしない)。`--json` の payload には `control_mode` (bool) と `attach` (`tmux -CC attach -t <session>`) が含まれ、launcher が control-mode 起動コマンドをそのまま採れる。
- **title 反映は実機確認が必要**: control mode では iTerm2 が window を native 管理するため、`set-titles` による OS window title 反映は iTerm2 側挙動に依存する (上記 QoL の注意と同じ)。`mozyo --cc` 実装後の title 反映可否は headless 検証不能なため、実機で観測し結果を #11729 / #11638 に記録する。
- iTerm2 launcher プロファイル (人形使い `iterm2-iac/`, #11729 journal #56427) の Command を `--cc` 変種へ切り替えるのは operator 側作業。

## 確認表

| 確認項目 | コマンド / 操作 | 期待値 |
| --- | --- | --- |
| tmux version | `tmux -V` | 3.2 以上 |
| snippet 配線 | `mozyo-bridge tmux-ui status` | `state: installed` |
| extended-keys | `tmux show-options -s extended-keys` | `extended-keys on` |
| title 設定 | `tmux show-options -g set-titles` | `set-titles on` |
| Shift+Enter | Claude Code 入力中に Shift+Enter | 送信されず改行される |
| 単語削除 | iTerm2 で Option+Backspace | 直前の単語が消える |
| Finder | `<prefix>` `f` | pane cwd が Finder で開く |
| control mode 起動 | `mozyo --cc --no-attach` | `attach: tmux -CC attach -t <session>` が表示される |
| control mode title | `mozyo --cc` で attach 後、iTerm2 window title | 実機観測し #11729 に記録 (iTerm2 依存) |

## 禁止事項

- 本 runbook・関連 issue・設定ファイルに credential / token / 個人情報を書かない。
- `.mozyo-bridge/tmux/agent-ui.conf` を workspace 側で手編集しない (正本は scaffold preset。変更は preset へ upstream して `scaffold apply --backup` で再配布)。
