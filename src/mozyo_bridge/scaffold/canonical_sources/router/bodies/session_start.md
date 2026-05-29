
## セッション開始

1. 現在の working directory がこの project root またはその配下であることを確認する。
2. mozyo-bridge の central preset rules を読む:
   - committed docs では portable 表記 `${rule_path}` を使う。
   - runtime で実ファイルを読む際は `mozyo-bridge rules home --resolved` の出力に `/rules/presets/${preset}/agent-workflow.md` を連結した絶対 path を読む。`--resolved` 出力は debug / runtime 用で、committed docs に貼らない。
   - resolved path や central preset を読めない場合は、読んだふりをせず停止し、`mozyo-bridge rules install` 等の復旧を operator に求める。
