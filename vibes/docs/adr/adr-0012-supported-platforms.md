# ADR-0012: 保守対応 platform は macOS と Linux (Windows 対象外)

- status: active
- date: 2026-08-20
- related: #15631 j#109074 (逐語引用元) / j#108997 (要約先行記録)、#15792 (server 常駐化の二 backend 適用先)、#15192 (OS scheduler common contract の先行実装)

## 決定 (規約行)

mozyo_bridge / herdr 運用基盤の保守対応 platform は macOS と Linux (Ubuntu) の 2 つとする。Windows は保守対象外であり、host 依存機能は macOS / Linux の両対応 (それ以外は typed refusal) を実装・レビューの要件とする。

## 背景

herdr server の常駐化 (OS 標準 service manager への一本化、#15795 / #15792) を設計する際、Linux は systemd、macOS は launchd と host 実現が分かれるため、保守対応 platform の明示が必要になった。#15192「OS scheduler common contract」は既に「単一の operator 向け契約 + macOS launchd / Linux systemd の二 adapter + 非対応 host は typed refusal (`service_backend_unsupported_platform`)」の pattern を確立しており、本決定はこの pattern を platform 方針として一般化する。

## 根拠 (逐語引用)

owner 発言 (2026-08-20、#15631 j#109074 に前後文脈つきで逐語固定):

> めちゃくちゃいいけど、Macでできるかどうかが気になるな。MacとUbuntu、まあLinuxだね、を保守対応とする。まあWindowsは知らん。

ADR 化承認 (同 journal):

> 両方やろ。

## 帰結

- host 依存機能 (service 常駐化、scheduler、pane/terminal 統合等) の新規実装は、macOS / Linux 双方の adapter を持つか、片方のみ実装する場合は他方を typed refusal で明示する。silent no-op は不可。
- review は「もう片方の platform で成立するか」を照合観点に含める。Windows 対応の欠如は指摘事項にならない。
- Windows 対応の要望が生じた場合は本 ADR の supersede を要する (なし崩しの部分対応をしない)。
