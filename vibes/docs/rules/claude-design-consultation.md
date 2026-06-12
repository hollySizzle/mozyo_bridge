# Claude Design Consultation 発火条件 (project-local 運用ルール)

Redmine #11702 / #11703。Codex / owner の設計議論に対し、実装担当 Claude へ **Design Consultation** を投げる運用を再現可能にするための project-local rule。本 doc は `vibes/docs/rules/` の作業規約であり、**central preset / `AGENTS.md` / `CLAUDE.md` を変更しない** (それらは scope 外)。base の Redmine gate lifecycle (`Design Consultation` / `Design Consultation Answer` gate) を再定義せず、その **発火判断を project でいつ行うかの運用指針** を足すだけである。

## 位置づけ: high-signal gate であり毎回必須ではない

- Claude design consultation は **high-signal gate** である。**全 issue で必須化しない。** 通常の実装は handoff → Implementation Done → (US-level) Review の標準フローで進める。
- これは base preset の `Design Consultation Gate` (後戻りしにくい判断の前に作る) の project 適用ガイドであり、新しい gate 名・transport kind を作らない。記録は既存の `design_consultation` / `design_consultation`(answer) journal を使う。
- 発火させすぎると overhead が増え、発火させなさすぎると後戻りコストを払う。下記の発火 / 非発火条件で線を引く。

## 発火する条件 (いずれか該当で検討)

1. **正本境界が変わる** — どの store / surface が真実の正本かが動く (例: registry / event log / projection / runtime の役割再定義)。
2. **責務境界が動く** — runtime tmux / SQLite / home registry / Redmine / OTel のいずれかの責務が再配置される。
3. **security / credential / handoff / pane targeting に影響する** — 送信先境界、loopback、token、cross-session、target 解決など事故が高コストな領域。
4. **owner の意図が抽象的で、実装上の制約と衝突しそう** — 望ましい姿は示されたが、実装の不変条件 (best-effort / fail-closed / identity 安定性等) と齟齬が出る可能性がある。
5. **Codex が設計判断を出せるが、実装者の反証が有益** — 設計は成立するが、実装読解からの破綻指摘 / 見落とし検出に価値がある。
6. **後から直すと高コストな方向転換** — schema / API / 配布物 / 安全境界など、後戻りに reopen 連鎖や migration を伴う変更。

## 発火しない条件 (いずれか該当なら通常フロー)

1. **既存設計の範囲内** — 確立済みの正本境界・パターンに収まる。
2. **変更が局所的** — 単一 module / 単一 surface に閉じ、波及が小さい。
3. **失敗しても容易に戻せる** — revert で原状回復でき、外部副作用がない。
4. **Claude の実装知識が判断にほぼ不要** — owner / Codex だけで妥当性を判断できる。
5. **Codex review だけで十分** — 設計分岐がなく、実装後の監査で品質を担保できる。

判断に迷う場合は「後戻りコスト × 実装者反証の有益性」で見る。両方高ければ発火、どちらも低ければ通常フロー。

## Claude への相談 payload 要素

Design Consultation を Claude へ渡すとき、journal と handoff summary に次を明示する:

- **これは実装依頼ではない。** Claude は実装せず、設計に答える。
- Claude に求めるもの: **反証 (技術的破綻・見落とし) / 懸念 / 推奨モデル / 最小 task 分割案**。
- 回答形式: **Redmine `Design Consultation Answer` journal** として返す (日本語で可)。pane 通知は pointer のみ。判断の正本は Redmine journal。
- 参照すべき durable anchor (related issue / 設計 doc / 既存不変条件) を列挙する。
- owner 判断が必要な事項 (business / 運用方針) と技術判断を分けるよう促す (base の `判断の routing` を継承)。

### handoff コマンド形 (参考)

```
mozyo-bridge handoff send --to claude --source redmine --issue <id> --journal <consultation_journal_id> \
  --kind design_consultation --target <claude_pane> --target-repo <repo_root> --mode queue-enter \
  --summary '設計相談: <topic>。実装はまだ不要。#<id> journal #<jid> を読み、反証・懸念・推奨モデル・最小Task分割案を Design Consultation Answer として返してください。'
```

`--kind design_consultation` は base の transport kind をそのまま使う。新 kind を作らない。

## Redmine gate lifecycle との整合

- 本 rule は base の `Design Consultation Gate` / `Design Consultation Answer Gate` の **適用タイミング指針** であり、gate の意味・順序・必須 field を変えない。
- **Design Consultation は全 issue / US の必須 close 条件ではない** (= 毎回発火させる gate ではない)。ただし **発火した場合は、その `Design Consultation Answer` と採用/却下の disposition を Review / Close Gate で照合する**。これは base の `Review Quality Hierarchy` (review 観点に Design Consultation Answer 整合を含む)、`Completion` (requested work を design consultation answer に照合)、`Close Gate Checklist` (owner / design role 判断の選択肢・採用案・却下案・理由が journal に残っていること) を継承するものであり、発火済み consultation の回答・採用判断を close 時に無視してよいという意味ではない。
- 高 signal な設計確認の結論を受けて、owner / Codex が Start / Implementation Request gate を切る。
- Consultation で出た方向転換 (re-parent / 分割 / scope 変更) は手戻り扱いしない (base の `Direct Request Triage (governed)` と同思想)。

## 禁止 / scope 外

- `AGENTS.md` / `CLAUDE.md` に本文を増やさない (router は薄く保つ)。本 rule は `vibes/docs/rules/` に置き、router からは catalog 経由で解決する。
- central preset (`.mozyo-bridge/rules/presets/**`) を本 rule で変更しない。preset へ昇格させたい場合は別途 preset 変更 task を立てる。
- Design Consultation を全 issue で必須化しない。
- Codex direct edit で本運用を進めない。設計 doc も Claude 実装 → Codex review の標準フローに乗せる (本 doc 自身がその例)。

## 参照

- base: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md` の `Redmine Gate Lifecycle` / `判断の routing` / `Direct Request Triage`。
- 実例: #11639 (#56299 Consultation / #56318 Claude Answer / #56330 Codex synthesis)、#11695 / #11698 の派生 US。
