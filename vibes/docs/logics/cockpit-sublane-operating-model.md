# Cockpit サブレーン運用モデル

## Purpose

この文書は Redmine #11850 の multi-lane cockpit PoC から出てきた運用哲学を
記録する。これは repo-local な logic 文書であり、private な社内運用規約
そのものではない。

目的は、mozyo-bridge の portable primitive を濁さずに、実運用で発生した
圧力と判断を後から検証できる形で残すことである。

## 観測された前提

cockpit は、同じ workspace の複数 checkout を別 lane として扱えるように
なった。現在の PoC では、次の形で運用している。

- main `mozyo_bridge` lane は coordinator lane である。
- 追加 worktree は sublane として append する。
- 各 lane には Codex pane と Claude pane がある。
- Redmine journal が durable source of truth である。
- pane message は durable anchor への pointer にすぎない。

このモデルが必要になったのは、実際に dogfooding して初めて見える問題が
複数あったためである。

- same workspace / multiple checkout identity には `lane_id` が必要である。
- cockpit と通常の `mozyo` session は、window name だけではなく role
  resolver で扱う必要がある。
- sublane 側で作業が完了しても、結果が coordinator lane に戻らなければ
  cockpit 上では停止して見える。
- identity が正しくても append 後の表示幅が偏ることがある。
- 開発中は installed CLI が repo-local source より遅れることがある。
- 関連する複数 project は近くに見える必要があるが、routing identity まで
  混ぜてはいけない。

## 中核の分離

cockpit model では、次の 4 つを混同してはいけない。

- **Identity**: workspace / lane / role / pane の durable な事実。
- **Routing**: どの agent が handoff を受け取り、行動してよいか。
- **Display**: pane、window、tab、iTerm/tmux view の見せ方。
- **Governance**: どの Redmine gate が実行や close を承認しているか。

window layout は人間が関連作業を見やすくするためには有用だが、routing の
source of truth ではない。隣に pane が見えていることは、lane 境界や
project 境界を越えた direct send の承認にはならない。

## Lane の役割

### Main Coordinator Lane

main Codex pane は coordinator、auditor、owner-facing window である。主に
次を扱う。

- owner への質問と close approval の回収。
- Redmine gate の解釈。
- review conclusion。
- release / push / CI の coordination。
- sublane の作成と退役。
- PoC findings の Redmine または repo-local docs への記録。

main Codex lane は direct edit に慎重であるべきである。project rule が許す
repo-local guardrail autonomous lane は使ってよいが、通常の実装や配布対象の
workflow surface は project の role boundary に従う。

### Sublane Codex

sublane Codex pane はその lane の gateway である。主に次を行う。

- まず durable Redmine anchor を読む。
- request が自 lane に属することを確認する。
- local Claude に実装させるべきか判断する。
- durable journal anchor 付きで local Claude へ route する。
- blocked / review-ready / owner-action-needed の状態を coordinator lane へ
  返す。

sublane Codex は、project が明示的に昇格させない限り、第二の owner-facing
coordinator ではない。

### Sublane Claude

sublane Claude pane は implementation worker である。主に次を行う。

- pane scrollback だけではなく Redmine journal から実装する。
- implementation_done と review_request gate を記録する。
- verification と residual risk を再現可能に残す。
- owner close approval を回収しない。

### Main Claude

main Claude pane は有用だが、parallel coordinator にしてはいけない。

安全な使い方は次である。

- scratch analysis。
- 長い出力や journal の要約。
- candidate extraction。
- draft wording。
- option の非権威的な比較。
- work が適切な Redmine-gated lane に移された後の implementation。

main Claude に任せるべきではないものは次である。

- owner questions。
- close approval collection。
- Review Gate conclusions。
- durable routing decisions。
- protected workflow、skill、source、test surface への silent edit。

main lane の Claude output は input であり、evidence ではない。coordinator
Codex は、それを decision に変換する前に source file、Redmine journal、
command output を確認しなければならない。

## Cross-Lane Routing Rule

複数 pane が同じ物理 tmux session にいても、lane boundary は governance
boundary である。

request が lane を越える場合は、まず target lane の Codex pane に route
する。その Codex が durable anchor を読み、implementation が適切であれば
local Claude へ route する。

Claude への direct delivery は same-lane addressing に限定する。これは
cross-session Claude direct-send prohibition と同じ原則を守るためである。

## Cockpit Groups

関連 project は同時に見える必要がある。portable rule は次である。

- named cockpit session を cockpit group として使う。
- group 内でも `workspace_id` / `lane_id` / role / pane identity は維持する。
- iTerm window、tab、tmux window は display grouping としてのみ扱う。
- cross-project consultation には Codex gateway handoff を使う。

無関係な project policy を OSS default に入れてはいけない。private cockpit
composition は private operating policy の領域であり、portable mozyo-bridge
default に混ぜない。

## Dogfooding Version Boundary

開発中は installed `mozyo-bridge` CLI が repo-local source より遅れることが
ある。workflow が landed 直後の command に依存する場合は、repo-local
invocation を使う。

```bash
PYTHONPATH=src python3 -m mozyo_bridge ...
```

これは dogfooding rule であり、public install contract ではない。public docs
では release 後の installed command を説明する。

## Coordinator への報告

sublane は handoff-worthy な state transition を Redmine と短い pane pointer
で coordinator lane へ返す。例は次である。

- blocked / needs clarification。
- implementation_done。
- review_request。
- review result。
- commit recorded。
- owner close approval requested。

これにより、sublane では完了しているのに cockpit coordinator view では停止
して見える状態を避ける。

## Ticket 化するもの

この PoC では、運用上の friction を意図的に child issue 化する。finding が
具体的で、再発しやすく、独立して修正可能なら ticket にする。

すでに観測された例は次である。

- cockpit append width rebalance。
- dogfooding 中の stale installed CLI。
- Redmine task 作成時の subject / description separation。
- Claude pane launch permission mode。
- main unit Claude role boundary。

#11850 は integration record として保つ。独立した fix path が必要な問題を、
構造のない dump として #11850 に積まない。

## Revision Principle

この文書は、観測された workflow risk と現時点の operating judgment を記録
するものである。特定 model の品質に関する恒久的な主張ではない。

Claude と Codex の挙動は時間とともに変わる。tool が変わったらこの文書も
見直す。ただし、より安全で単純な model が存在する証拠がない限り、次の
core separation は維持する。

- durable state は Redmine に置く。
- identity は workspace / lane / pane level で扱う。
- boundary を越える routing は Codex gateway を通す。
- implementation は bounded lane で行う。
- owner-facing decision は coordinator lane で行う。
