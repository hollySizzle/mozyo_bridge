# コピー経路 Runbook (Claude / Codex / tmux)

Redmine #11884 (親 US #11640)。tmux + Claude / Codex 出力のコピーで起きる「3 行選んだのに 1 行しか取れない」「ウィンドウ幅由来の折り返し改行・ガターが混入する」問題に対し、**どの経路で何が取れて何が取れないか**を operator が迷わず判断できるようにするための runbook。事実関係は #11640 journals #56103 / #56109 / #56118 / #56123 の owner 実機調査 (2026-06-11) を正本とする。

## 結論サマリ (取りたいもの → 経路)

| 取りたいもの | Claude Code | Codex | 実装非依存フォールバック |
| --- | --- | --- | --- |
| 回答全体 | `/copy` (`/copy N` で N 番目) | `/copy` (Ctrl+O) | エージェントに「pbcopy して」と依頼 |
| 回答の一部 | `/copy` 後にダンプファイルをエディタで開いて通常選択 | 同左 | エージェントに「ファイルに書いて」と依頼 |
| 会話全体 | `/export` | `~/.codex/sessions/` の JSONL | エージェントに「ファイルに書いて」と依頼 |
| shell pane の生出力 | `tmux capture-pane -J` (共通) | 同左 | 同左 |
| パス / URL / 単語の高速ヤンク | extrakto / tmux-thumbs (導入は任意) | 同左 | 同左 |
| 自然なドラッグ選択がどうしても欲しい | claude-devtools (任意・Claude 専用) | — | — |
| 画面の見た目どおりの切り取り | ターミナル内ドラッグ (改行・ガター混入は仕様) | 同左 | 同左 |

要点: **TUI 描画後のテキストをクリーンに取るには TUI のアプリ層機能 (`/copy` / `/export`) を使う。** ターミナル側 (iTerm2 / tmux / ドラッグ / `capture-pane -J`) でエージェント出力の折り返しを復元することはできない。

## 折り返しの 2 種類 — 直せる側と直せない側

折り返し改行には対処可能性の異なる 2 種類がある (#56103)。

- **(a) 端末ソフトラップ** — shell が幅で折り返した表示上の改行。tmux は `wrapped` とマークするため `tmux capture-pane -J` で結合できる。**直せる側。**
- **(b) TUI 焼き込み改行** — Claude Code / Codex の TUI が描画時に各視覚行を個別に発行する実改行。tmux は `wrapped` とマークしないため `capture-pane -J` でも結合されない。全行頭に TUI の 2 スペースガターも混入する。**端末側では復元不能 (直せない側)。**

owner 実機 (通常 attach ウィンドウ, pane 幅 95) で Claude Code 出力をドラッグコピーすると単語途中で改行 + ガター混入を再現し、`capture-pane -J` で結合されないことを実測確認した (#56103)。つまりこの汚染は iTerm2 設定 / tmux 設定 / `-CC` か通常 attach かの別 / `-J` のいずれでも解決できない。

### 上流方針 (確定)

症状 (b) と同一の根本原因 (TUI が実改行を発行し `GRID_LINE_WRAPPED` を保存しない) は公式 issue `anthropics/claude-code#42296` として起票されたが、**closed as not planned** で確定した (#56123)。Claude Code はソフトラップ化オプションを採用しない方針のため、「ドラッグコピーがいつか自然に直る」可能性は消滅した。watch 不要 (#56109 の watch 記載を #56123 が上書き)。

## 経路ごとの推奨操作と限界

### `/copy` (Claude Code / Codex 共通)

- Claude Code は v2.1.77 で `/copy` を追加 (owner 環境の CC は 2.1.168+ で利用可能, #56109)。最新回答をクリップボードへ直接コピーし、`/copy N` で N 番目を指定できる。**描画前のテキストを取るため改行汚染・ガター無し。**
- Codex CLI にも `/copy` が存在し、Ctrl+O ショートカットで最新完了出力をコピーできる (#56118)。両 CLI は同じ動詞に収斂済み。
- Claude Code の `/copy` は**クリップボードに加えて毎回ファイルにも書き出す** (owner 実機確認: ユーザーごとの temp ディレクトリ配下、例 `/tmp/claude-$UID/response.md`)。**回答の一部だけ欲しい場合**は、このファイルをエディタで開いて通常選択する経路が追加インストールゼロで成立する (#56123)。

### `/export` (Claude Code) / `~/.codex/sessions/` JSONL (Codex)

- 会話全体をまとめて取りたい場合は Claude Code の `/export` を使う。
- Codex 側は `~/.codex/sessions/` 配下の JSONL が会話記録の実体であり、ここから取る。

### `tmux capture-pane -J`

- **shell pane の生出力にのみ有効。** ソフトラップ (上記 (a)) を結合してクリーンに取れる。
- TUI 出力 (Claude / Codex の回答本文) には無効。(b) の焼き込み改行は結合されない。

### ターミナル内ドラッグコピー

- 通常 attach ウィンドウでは `mouse on` のドラッグを tmux copy-mode が処理し、tmux buffer → `set-clipboard external` (OSC52) → システムクリップボード、という経路で動く (owner 実機で正常動作を確認, #56103)。
- ただし TUI 出力に対しては (b) の改行・ガター混入が避けられない。**「画面の見た目どおりに切り取る」用途と割り切る。** クリーンなテキストが必要なら `/copy` 等のアプリ層機能へ退避する。

### 補助ツール (導入は任意)

- **extrakto** / **tmux-thumbs**: fzf やヒント文字でパス / URL / 単語 / 行を高速にヤンク (OSC52 対応)。段落の再結合はしないが、パス・URL・コマンド用途を高速化する (#56109)。導入要否は operator の好み次第。
- **claude-devtools** (community 製 Electron transcript viewer): 視覚レイアウトに従う本物のドラッグ選択・ブロック単位コピー・export を提供する (Claude 専用)。JSONL パースを community が保守するため、形式追従コストを外部化できる (#56123)。導入は任意。

## 症状A (3 行選んで 1 行しか取れない) — 未再現・仮説

#56103 時点で再現できておらず、仮説が 2 本ある。

1. OSC52 の最終段が iTerm2 の clipboard アクセス許可設定でブロックされると**無言で破棄**され、クリップボードに前回内容が残る (「コピーしたつもりが古い 1 行」と整合)。許可設定は iTerm2 profile 依存。
2. TUI 側のマウスイベント捕捉との干渉 (ドラッグが copy-mode に入らないケース)。

次回再現時に「どのウィンドウ (`-CC` か通常 attach か) / どの操作 (ドラッグ / Option+ドラッグ / copy-mode)」を記録して確定する。owner のクライアントは `-CC` (control mode) と通常 attach が混在しており、コピー挙動はモードで異なる点に注意する (#56103)。

## mozyo-bridge 側の helper を作らない判断

**`mozyo-bridge` 側に transcript parser / unified copy helper を作らない。** 根拠 (#56118 / #56123):

- 統一 copy helper が parse する対象 (`~/.claude/projects/*.jsonl` / `~/.codex/sessions/rollout-*.jsonl` 等の内部 transcript ファイル形式) は slash command より変わりやすい。hooks を採用しないのと同じ理由で、内部ファイル形式への依存は劣後する。
- 当初 #11640 にあった「`capture-pane -J` ベースの copy helper」案は、**transcript には無効** (TUI 焼き込み改行は `-J` で結合できない) と実測で判明した (#56103)。helper を作るなら対象を shell 出力に限定するか、アプリ層 export の wrapper に方針変更する必要があるが、いずれも採らない。
- 対話 UX 機能 (slash command) への依存は許容する: 壊れても人間が即気づいてドラッグに退避するだけで、爆発半径が人間 1 人の手間に収まる。hooks のように system が暗黙に成立する依存ではない (#56118)。
- 実装非依存の汎用経路は、エージェントへの自然言語依頼 (「pbcopy して」「ファイルに書いて」) で既に確保されている (#56118)。
- transcript の視覚的コピーが必要なら、形式追従を community が保守する claude-devtools のような外部ツールに委ねる方が、自前 transcript パーサより整合的である (#56123)。

## 残作業

- 症状A (3 行 → 1 行) の再現待ち (上記仮説 2 本)。再現できたら本 runbook に確定経路を追記する。
- extrakto 等の補助ツール導入は operator 判断。導入した場合は配線手順を本 runbook に追記する。
