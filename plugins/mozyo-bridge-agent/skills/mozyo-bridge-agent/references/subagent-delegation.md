# Subagent / Background 委譲 Guideline Reference

agent (特に coordinator 役) が main context を塞がないための subagent / background 委譲の判断基準 (Redmine #13147)。本 reference は portable な委譲 guideline の正本である。最重要論点は **hidden-worker 禁止規約との境界** — subagent を使う基準と、可視 lane / durable anchor を前提とする既存規約とが現場で衝突しないよう、両者の関係を明文化する。

本 reference は既存の hidden-worker 禁止規約を **緩めない**。既存規約の正本は次を読む:

- `references/workflow.md` の見出し **`coordinator_assistant` の安全使用境界** (`implementation_worker` との違いを含む): 可視 lane の実装 worker と、出力が input-not-evidence の provider-neutral assistant を分ける境界。
- `references/workflow.md` `## Sublane の coordinator callback` / `## Workflow docs の正本境界`: 可視 lane / durable anchor 前提と hidden-worker 禁止の位置づけ。
- 採用 repo の sublane 定義 (可視 checkout lane / pane identity を持つ実運用 lane のみを sublane とし、内部 `multi_agent` / hidden worker / forked subagent は sublane として扱わない) は repo-local rule に置かれる。`mozyo_bridge` では `vibes/docs/rules/agent-workflow.md` がそれを持つ。

本 reference はこれらの本文を再掲せず、subagent / background という *道具* をこの境界の *内側で* どう使うかだけを扱う。

## 委譲基準 matrix

判断原則は **「main agent は常に次の判断に使える状態を保つ」**。main context を長時間塞ぐ作業は切り出し、判断・記録・対話は手元に残す。

| main context に残す | subagent / background へ切り出す |
| --- | --- |
| 設計判断・scope 判断・trade-off の裁定 | 長時間の検証 suite 実行 (test / lint / coverage の全量走行) |
| gate journal の記録 (start / implementation_done / review / close) | install / build / smoke などの環境依存で時間のかかる手順 |
| handoff 送信・cross-lane routing | 大量の ticket journal 読解、repo 横断調査、log / diff の要約 |
| owner 対話 (質問・承認収集) | well-specified で受け入れ条件が固定された実装 (下の境界節の条件付き) |
| 短い状態確認・軽量な事実 lookup | 反復的で機械的な広探索 (grep fan-out、候補抽出) |

- 切り出す判断材料は「main context の budget を守れるか」と「その作業が権威的な判断を含まないか」の 2 つである。budget を食うだけの読解・実行・要約は迷わず委譲する。
- 逆に、短い状態確認や 1 fact の lookup を委譲すると往復コストが委譲益を上回る。手元で済むものは手元で済ませる。

## Hidden-worker 禁止との境界 (最重要)

既存規約 (可視 lane / durable anchor 前提、内部 worker は sublane ではない) は **変更しない**。その上で subagent / background の許容範囲を次の 3 分類で固定する。

### (a) read-only 調査・検証実行・要約の委譲は常時許容

read-only 調査、検証の実行、log / diff / journal の要約を subagent / background に委譲することは、それが **dispatch authority も実装 diff も持たない** ため常時許容される。これは `references/workflow.md` の **`coordinator_assistant` の安全使用境界** / **許可される用途**にある assistant タスク (要約・候補抽出・scratch 調査・draft) と同じ性質であり、subagent はその budget を親から借りる実行手段にすぎない。委譲した結論は input であって evidence ではなく、durable record に載せる前に委譲元が正本と突き合わせて確認する。

### (b) 実装 shaped work の subagent 委譲は条件付き

実装 diff を生む形の作業を subagent に委譲することは、**explicit な owner / operator exception が durable record にある場合に限り** 許容される。この exception は hidden-worker 禁止の緩和ではなく、次の 4 条件を **すべて** 満たす構成でのみ成立する:

1. **実装責任者は可視 lane の agent のまま**である。subagent は責任を負わない実行手段であり、implementation_done / review_request を記録する主体でも、lane の gate を進める主体でもない。
2. **subagent の diff は実装責任者が全量レビューし、issue Scope 全項目の実装有無を照合してから commit / push する**。未照合の diff をそのまま取り込まない。
3. **通常の review gate (cross-agent audit) は免除されない**。subagent が実装したことは、別 agent による review / audit を省く理由にならない。
4. **start gate に実装体制と例外 anchor を記録する**。誰が実装責任者で、どの owner / operator exception journal を根拠に subagent を使うかを durable record に残す。

これらのどれか 1 つでも欠けると、それは監査不能な hidden worker になり、既存規約が禁じる状態に落ちる。

### (c) subagent は可視 lane の責務を代行しない

subagent は **cross-lane handoff の送信・owner 対話・gate 記録を行わない**。これらは可視 lane の agent (main-unit / sublane の責任 actor) の責務であり、subagent に委譲した瞬間に durable record と pane identity の監査可能性が崩れる。subagent の出力はあくまで委譲元へ返り、委譲元が可視 lane の actor としてそれらの action を所有する。

## 活動単位の使い分け (sublane / subagent)

sublane と subagent の使い分けは US 単位ではなく **活動単位** で判断する。同一 US の中でも、調査 → 実装 → 記録と活動が切り替わるたびに routing を判断し直す。

```yaml
activity_routing:
  governed_activity:
    対象: 実装 diff を生む作業 / gate journal 記録 / cross-lane handoff・dispatch
    routing: mozyo-bridge sublane (可視 lane)
    理由: durable anchor・pane identity・cross-agent audit を前提とする活動は可視 lane の actor が所有する
  read_only_activity:
    対象: 調査・探索・要約・検証実行・draft
    routing: subagent / background 委譲
    理由: dispatch authority も実装 diff も持たない (上記 (a) のとおり常時許容)
```

本 rule は target-state である。tooling の一時的な状態は routing 判断の入力にならない。governed activity を subagent へ流す唯一の例外経路は上記 (b) の explicit exception (4 条件すべて) であり、それ以外の代替経路を作らない。

## Model 選択基準

- **実装 shaped lane は実装用 model を明示指定する**。起動パラメータで model を明示し、既定の親継承に依存しない。実装の質は model 選択に依存するため、暗黙の継承で意図しない model に落ちることを避ける。
- **判断の重い調査・検証は親 model を継承する**。要約・広探索・検証実行など、親の判断品質と揃えるべき委譲は明示指定せず親継承でよい。
- **事後検証手順**: 委譲に使われた実 model は session transcript (`~/.claude/projects/**/*.jsonl`) と subagent transcript の `"model"` field を grep すれば実測確認できる。委譲した事実と使用 model は推測で durable record に書かず、この実測 field で裏取りする。

## 並走時の統合コスト governor

- **lane は file surface で事前区分する**。委譲・並走させる lane が触る file surface を先に区分し、重なるものは直列化する。同一 file を並走で編集させると統合時に衝突コストが跳ねる。
- **統合コストが高くなったらレーン数を減らす**。並列度は throughput のためだが、統合 (rebase / merge / baseline 照合) のコストが並列益を食い始めたら、走らせる lane 数を絞る。並列度は目的ではなく手段である。

## 記録規律

- **根拠出所の明示**: subagent の結論を durable record に載せる際は、根拠出所を `agent_judgment` として明示する。委譲元が正本と突き合わせて確認するまで、それは evidence ではなく agent の判断である。
- **委譲事実と model の記録**: 何を subagent / background に委譲したか、どの model を使ったかは、start gate / implementation_done の durable record に記す。委譲は pane chat の一過性の事実ではなく、監査可能な durable record 上の事実として残す。

## 境界

- hidden-worker 禁止・可視 lane / durable anchor 前提の本文: `references/workflow.md` の **`coordinator_assistant` の安全使用境界** / `## Sublane の coordinator callback` / `## Workflow docs の正本境界` が正本。本 reference は再掲しない。
- gate 語彙・必須 journal field・review / close の意味論: central preset が正本。本 reference は gate を追加しない。
- 採用 repo 固有の sublane 定義・lane 数・cockpit 構成・具体 model 名: repo-local rule と operator runbook に置く。本配布 body には operator 固有の数値・path・pane id・具体 model 名を置かない。portable な部分は *委譲基準・hidden-worker 境界・model 選択の判断ロジック・統合 governor・記録規律* であり、具体値は operator のものである。
