# Claude Code Router

Claude Code セッションの tool-specific 入口。Claude Code は本ファイルを native に読む。共通の central preset rules は `${rule_path}` を正本とし、router 本文には複製しない。AGENTS.md (Codex tool-specific) を import しない。

## セッション開始

1. 現在の working directory がこの project root またはその配下であることを確認する。
2. mozyo-bridge の central preset rules を読む:
   - committed docs では portable 表記 `${rule_path}` を使う。
   - runtime で実ファイルを読む際も `${rule_path}` を読む。repo-local store (`.mozyo-bridge/rules/...`) の path は repo root からの相対でそのまま読める。central store の home prefix は `mozyo-bridge rules home --resolved` の出力で解決する (`--resolved` 出力は debug / runtime 用で、committed docs に貼らない)。
   - resolved path や central preset を読めない場合は、読んだふりをせず停止し、`mozyo-bridge rules install` 等の復旧を operator に求める。
3. 非自明な作業を始める前に active な `${ticket_anchor_label}` を確認する。

`${rule_path}` が存在しない場合は、読んだふりをせず停止し、operator に `mozyo-bridge rules install` を依頼する。

## ClaudeCode 起動時の最小 reminder

- 迎合せず事実に基づいて結論を述べる。意見の不一致は `${rule_path}` が指定する durable record に残す。
- implementation done / implementation_done は completion ではない。review / audit / close 条件は `${rule_path}` に従う。
- pane 通知は通知でしかない。判断の正本は `${rule_path}` と active な `${ticket_anchor_label}` を読む。
- handoff を送る場合は `${rule_path}` の handoff startup decision / receive-method rule に従い、受領方法を durable record に残す。
- `mozyo-bridge status` / `mozyo-bridge doctor` / pane scrollback は operator/debug 用。durable anchor が利用可能なときに、それらから receiver state や ticket state を推測しない。
- handoff chat は state + durable anchor の最小ポインタにとどめる。受領方法・retry 計画・試行コマンドは durable record 側に置き、chat に貼り直さない。
- 詳細・例外・gate templates は `${rule_path}` を読む。router に重複させない。

## サブレーン開発フロー (opt-in profile)

- 本 project は `scaffold apply <preset> --with-sublane-flow` でサブレーン開発フローを runtime-active な参照として有効化している。default scaffold では本節は生成されない。
- 配布された opt-in entrypoint doc `vibes/docs/profiles/sublane-flow-runtime-profile.md` を読み、そこから `mozyo-bridge-agent` skill workflow reference の sublane sections へ辿る。router 本文に workflow 詳細を複製しない。
- lane 数・cockpit 構成・絶対 path・session 命名などの private operating policy は本 profile に含まれない。adopter は自身の operating profile を別途定義する。

## Project-Local Additions

<!-- mozyo-bridge:project-local-additions:begin -->
<!--
このマーカー間は `mozyo-bridge scaffold apply` / `scaffold diff` で機械的に保持されます。
ClaudeCode 起動時に project-local で必ず思い出してほしい reminder (危険 command、
Doc-readonly 領域、project 固有 role boundary override 等) をここに追記してください。
マーカー外の内容は scaffold 再生成で上書きされます。
-->
<!-- mozyo-bridge:project-local-additions:end -->
