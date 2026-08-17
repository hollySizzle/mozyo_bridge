# ADR-0007: v2.0.0 は herdr 既定化の破壊的リリースで切り、MCP 標準入口は 2.1 へ回す

- status: active
- date: 2026-08-17
- related: Redmine #15594 j#106992 (owner 決定の逐語記録)、#15531 (herdr 既定反転、完了)、#15148 (MCP 標準入口 Feature)、#15150 / #15152 / #15153 / #15154 / #15268 (MCP 標準入口の配下)、ADR-0006 (MCP 認証境界)

## 決定 (規約行)

v2.0.0 は **herdr を既定 terminal backend にする破壊的変更** (#15531、統合済み) で切る。
「LLM 操作は MCP 標準入口」という theme (#15148 配下: #15150 / #15152 / #15153 / #15154 /
#15268) は **v2.1** へ回す。2.0 を MCP 標準入口の完成で gate しない。

## 背景

v2.0.0 planning container (Redmine version 327) の theme は当初「LLM 操作は MCP / 既定 backend は
herdr / CLI・tmux は debug 用途」の 3 点だった。実装状況を事実で確認すると:

- herdr 既定反転 (#15531、破壊的) — 完了・main 統合済み。
- 3 階層 admission 修正 (#15146) — close 済み。
- MCP 標準入口 (#15148) — **半分**。読み取り系 (#15151) 完了、変更操作 (#15152) レビュー中だが、
  **managed LLM の入口を実際に MCP へ切り替える #15150 と段階導入 #15153 が未着手**。#15150 が
  無い限り「LLM 操作は MCP」は看板だけで、LLM は依然 CLI/handoff を使う。

加えて #15152 のレビューで、MCP の in-process 変更操作は外部 client に対して caller 認証を安全化
できず、信頼境界を runtime perimeter (attested pane) と明示する形で deferred にした (ADR-0006)。
MCP を「標準入口」として広く売り出すには、この認証境界の制約が取れる (#15579) まで時期尚早である。

## 根拠 (逐語引用)

出所: #15594 j#106992 (owner 決定の逐語記録、2026-08-17 chat)。

- 「OK。ハードル (=herdr) 固定化にして、標準入り口を 2.1 にしようか。決定した意図としては、
  リリースは早くやった方が良いと考えています。早くやると、その分 Dokku フーディング
  (=dogfooding) で色々なバグがチェックできるから、細かく切っても全然 OK だと思います。」

## 影響

- **2.0 release gate の acceptance から MCP 標準入口を外す。** 2.0 の必須は herdr 既定反転
  (#15531 完了) と、その破壊的変更に伴う移行・回帰・doc であり、#15150 / #15152 / #15153 /
  #15154 / #15268 は 2.0 の blocker ではない。
- **MCP 標準入口の配下 issue は v2.1 container へ再割当する** (roadmap metadata 作業)。現在
  version 327 (v2.0.0) を持つ #15152 等は 2.1 へ移す。v2.1 version の新規作成と再割当は本 ADR の
  執行として行う (owner 承認済み scope 判断の機械的反映)。
- **2.0 の release note は herdr 既定反転を主題とし**、MCP tool は「利用可能だが標準入口化は 2.1」
  と位置づける。ADR-0006 の「MCP 変更操作の信頼境界 = attested pane」制約も release note の既知
  制約として明示する。
- 本 ADR と矛盾する「MCP 標準入口の完成 (#15150 等) を 2.0 の gate にする」判断は
  `adr_conflict_gate` の対象 (owner 裁定なしに 2.0 を gate し直さない)。
