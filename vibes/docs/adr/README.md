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

- status: active | superseded (by ADR-MMMM)
- date: YYYY-MM-DD
- related: <Redmine issue/journal 参照>

## 決定 (規約行)

<1〜2 行。エージェントが standing context に注入できる密度で書く>

## 背景

<何が起きてこの決定に至ったか>

## 根拠 (逐語引用)

<owner 発言・承認記録を、出所 (issue/journal または日付+chat) 付きで逐語引用する。
 要約で置き換えない — 引用が改変検知と藁人形防止の実体>

## 影響

<この決定が拘束する実装・レビュー・運用の範囲>
```

## 運用規則 (正本: ADR-0001)

- ADR の新規作成・supersede は owner の決定があったときのみ。エージェントが自発的に起草する場合は
  draft と明示し、owner 承認の記録 (journal または chat 引用) が入るまで status: active にしない。
- active な ADR と矛盾する review finding は、その ADR を **名指し** した上で挑戦する。owner の
  明示承認なしに ADR と矛盾する変更を採用してはならない (黙った上書きの禁止)。
- 決定が変わったら新しい ADR を作り、旧 ADR を `superseded (by ...)` にする。旧 ADR は消さない。

## 索引

| ID | 題名 | status |
| --- | --- | --- |
| [ADR-0001](adr-0001-adr-practice.md) | owner 決定は ADR として記録し、レビューは黙って上書きできない | active |
| [ADR-0002](adr-0002-enter-resend-priority.md) | 受信側が busy でも Enter を押す (停止は二重送信より害が大きい) | active |
| [ADR-0003](adr-0003-three-tier-granularity.md) | 3 階層粒度 (release / version / US) とレビューの単位は US | active |
