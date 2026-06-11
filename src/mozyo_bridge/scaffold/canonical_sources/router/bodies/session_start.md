
## セッション開始

1. 現在の working directory がこの project root またはその配下であることを確認する。
2. mozyo-bridge の central preset rules を読む:
   - committed docs では portable 表記 `${rule_path}` を使う。
   - runtime で実ファイルを読む際も `${rule_path}` を読む。repo-local store (`.mozyo-bridge/rules/...`) の path は repo root からの相対でそのまま読める。central store の home prefix は `mozyo-bridge rules home --resolved` の出力で解決する (`--resolved` 出力は debug / runtime 用で、committed docs に貼らない)。
   - resolved path や central preset を読めない場合は、読んだふりをせず停止し、`mozyo-bridge rules install` 等の復旧を operator に求める。
