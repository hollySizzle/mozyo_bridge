# ADR-0013: ユーザーに pane / 内部操作を意識させない UI (UX 要件)

- status: active
- date: 2026-08-20
- related: #15631 j#109074 (逐語引用元) / j#109068 (要約先行記録) / j#109065 (発端事象: cold-restore 復旧で owner 手動 pane close が必要になった)、`vibes/docs/logics/herdr-plugin-presentation-consumer-boundary.md` (#14631、presentation consumer 境界)、`vibes/docs/logics/cockpit-web-ui.md`

## 決定 (規約行)

運用 UI は、ユーザーが pane や内部 lifecycle 操作 (close / relaunch / heal / retire / rebind 等) を意識せずに済む状態を提供することを要件とする。ユーザーの直接 pane 操作 (サイズ変更等) は禁止しないが、直接操作を前提とする運用手順を標準にしない。

## 背景

2026-08-20 の herdr server 一本化 (#15795) で、cold-restore された coordinator pair の復旧に owner の手動 pane close が必要となり (#15631 j#109065)、同日の運用全体でも「人間または agent が正しい操作経路を知識で選ぶ」ことに起因する手順ミスが複数発生した。owner はこれを受け、操作の UI 化を UI 拡張要件として宣言し、その趣旨を「禁止規則ではなく、ユーザーに意識させない UX 要件」として確定した。

本決定は #14631 の presentation consumer 境界 (UI は governed rail の投影に徹し、identity / routing / approval の正本を作らない) を変更しない。UI が提供するのは rail の呼び出しと状態の可視化であり、rail の完備 (#15745 / #15792 等) が前提工程である。

## 根拠 (逐語引用)

owner 発言 (2026-08-20、#15631 j#109074 に前後文脈つきで逐語固定):

> なるほどね、理解した。まあ、ただUIは必要だろうね。あんまりペインを直接触らせるというか、せいぜい大きさの変更ぐらいで、それ以外全部UIでやらせるっていうのが、UI拡張の要件の1つになるだろうね。

ADR 化承認と文言修正 (同 journal):

> 両方やろ。直接操作原則禁止というよりは、ユーザーに対して意識させないようにするっていうUX要件だね。

## 帰結

- 運用手順・runbook・復旧手順の設計は「ユーザーの手動 pane 操作を要求しない」ことを目標状態とする。手動操作が残る箇所は既知の gap として記録し、rail / UI 側の改善対象にする (例: default pair の close+relaunch rail 欠落 → #15745 / #15792 へ引継ぎ済み)。
- UI (herdr plugin / WebViewer / private cockpit) の action は governed rail の呼び出しに限定し、pane への直接入力書込みは capability policy どおり禁止のまま。
- レビューは、新規運用手順が本 UX 要件の目標状態へ向かっているか (手動 pane 操作の追加を既定にしていないか) を照合観点に含める。
