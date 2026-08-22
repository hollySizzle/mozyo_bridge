# ADR — Owner Decision Records (Redmine #15536)

owner の意思決定を、実装・レビュー・運用のどの工程からも「最重要文書」として参照できる形で
固定するための記録。この repo の ADR は **owner が下した決定** だけを収める。設計メモ・手順・
実装解説は入れない (それらは logics / rules / specs の管轄)。

## なぜあるか (実害の記録)

owner が繰り返し指示した送信リトライ方針 (#12580 で承認) が、後続レビュー 4 往復の安全側指摘の
積み重ねで段階的に狭められ、owner の優先順位が一度も再確認されないまま実運用の停止を生んだ
(#15202 の経緯、2026-08-16 に 1 日 4 回の配送停止として顕在化)。決定は Redmine に記録されて
いたが、**レビューが決定を参照する義務が無かった**。ADR はその義務の対象物である。

## 書式

1 決定 1 ファイル。ファイル名は `adr-NNNN-<slug>.md`。必須構成:

```markdown
# ADR-NNNN: <題名>

- status: active | superseded (by ADR-MMMM)   # この 2 状態のみ。承認前の ADR file は作らない
- date: YYYY-MM-DD
- related: <Redmine issue/journal 参照>

## 決定 (規約行)

<1〜2 行。エージェントが standing context に注入できる密度で書く>

## 背景

<何が起きてこの決定に至ったか>

## 根拠 (逐語引用)

<owner 発言・承認記録を逐語引用する。要約で置き換えない — 引用が改変検知と
 藁人形防止の実体。出所は exact な Redmine anchor (`#<issue> j#<journal>`、
 または issue description) を必須とする。chat が初出の決定は、先に exact text と
 前後文脈を durable journal へ記録し、ADR はその journal を引用元にする。
 「日付+chat」単独の出所は invalid であり、その引用を持つ ADR は active にできない>

## 影響

<この決定が拘束する実装・レビュー・運用の範囲>
```

## 運用規則 (pointer)

判断の正本は [ADR-0001](adr-0001-adr-practice.md)、エージェントの実行契約 (trigger / 必須 field /
fail-closed 動作) は `vibes/docs/rules/agent-workflow.md` の `adr_conflict_gate`。本 README は
書式と索引のみを持ち、規則本文を重複させない。

## 索引

| ID | 題名 | status |
| --- | --- | --- |
| [ADR-0001](adr-0001-adr-practice.md) | owner 決定は ADR として記録し、レビューは黙って上書きできない | active |
| [ADR-0002](adr-0002-enter-resend-priority.md) | 受信側が busy でも Enter を押す (停止は二重送信より害が大きい) | active |
| [ADR-0003](adr-0003-three-tier-granularity.md) | 3 階層粒度 (release / version / US) とレビューの単位は US | active |
| [ADR-0004](adr-0004-review-depth-tiers.md) | レビュー深度は変更クラスで段階化し、途中再分類できる | active |
| [ADR-0005](adr-0005-adversarial-mode-convergence.md) | adversarial review mode は宣言脅威モデルで収束させ、圏外指摘は保留記録する | active |
| [ADR-0011](adr-0011-three-layer-responsibility-division.md) | 3階層は責務の所在の分担であり多重レビューではない (サブレーン=実装+自己完結監査 / プロジェクトコーディネーター=drain 最重要+dispatch+統合 / コーディネーター=構成+リリース+Redmine ベース ADR 確認) | active |
| [ADR-0012](adr-0012-supported-platforms.md) | 保守対応 platform は macOS と Linux (Windows 対象外) | active |
| [ADR-0013](adr-0013-ui-hides-pane-operations.md) | ユーザーに pane / 内部操作を意識させない UI (UX 要件) | active |
| [ADR-0014](adr-0014-dead-unit-proxy-recovery.md) | 死んだユニットの代理回収 — 上位層 (相方役) は「事実」を回収するが「完了」を推測しない | active |
