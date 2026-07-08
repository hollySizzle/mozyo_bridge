
## 常時適用規則ダイジェスト (生成)

task 種別と無関係に毎 turn 適用する always 規則の最小 digest。scaffold が正本から生成する (手編集禁止。正本変更時の drift は `mozyo-bridge scaffold canonical --check` で落ちる)。各 entry は pointer であり、本文・例外・境界は pointer 先の正本を読む。

<!-- mozyo-bridge:always-digest:begin -->
- narrative の ticket 参照は `#<id> <短い概要>` で書く (ID 単独で呼ばない)。正本: skill `references/workflow.md` の `### Narrative の issue 参照は` 節。
- ユーザー向け応答は workspace の応答言語 preference に従う。正本: central preset `### 応答言語ポリシー`。
- 迎合せず結論を述べ、review finding には根拠の出所を明示する。正本: central preset `### Review Finding Verdict Obligation (迎合禁止)` / `### 根拠出所分類`。
- 設計・仕様・現状挙動を回答・断定する前に、質問ドメインの cataloged docs を catalog (`.mozyo-bridge/docs/catalog.yaml` / `docs resolve`) で解決して読む。memory / 直近 journal は pointer であり verdict ではない。正本: central preset `### 回答前 Doc 解決 (Answer-Time Resolution)`。
<!-- mozyo-bridge:always-digest:end -->
