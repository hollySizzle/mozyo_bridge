# ADR-0009: 小規模の既定運用モデルは「1 階層 / 2 役割」とし、実装者とレビューアの分離は常時保つ

- status: proposed (owner ratify 待ち。owner は本 ADR の *file 化* を承認済み (#15578 j#107064 proceed (b)) だが、本文の exact text はまだ ratify していない。active 化は owner の文面裁定 anchor 成立後)
- date: 2026-08-17
- related: Redmine #15578 j#107053 (coordinator decision-capture、item ①) / j#107064 (owner ratification)、#15601 (役割 identity を MCP 応答へ埋める US)、#15152 (退行の実証根拠)、#15594 / ADR-0007 (v2.0/v2.1 の versioning context)、ADR-0001 (owner 決定は ADR、レビューは黙って上書きできない)

## 決定 (規約行)

小規模 / 上級モデルの作業の**既定運用モデルは「1 階層 / 2 役割」**とする — 実装者 1 と、モデル
系統を跨ぐレビューア (cross-family reviewer) 1 の 2 役割・1 階層。サブレーンも、独立した
project-coordinator 層も置かない。並列・スケールが要る場合に限り multi-layer の委任コーディ
ネーション (3 階層) へ**上げる**。ただし**実装者とレビューアの分離は 1 階層でも常時必須**とし、
規模を理由に統合しない (根拠は #15152 の退行実例、下記)。

## 背景

上級モデル / 小規模作業では、3 階層 (project-coordinator + サブレーン) のオーバーヘッドが純コスト
になりやすい。現状構成は claude 1 ペイン + codex 1 ペインであり、これが既に 1 階層 / 2 役割の
実体である。この構成を「小規模の既定・階層はスケールで切替える」と明文化するのが本 ADR の主眼で
ある。owner は 2026-08-17 の chat で harness / 運用基盤の作業を最優先事項と位置づけており
(#15578 の起票文脈)、運用モデルの明文化はその優先度に沿う。

本 ADR は成功だけを記録する rosy な文書ではない。1 階層は次の 3 つの正直な条件・緊張の上でのみ
成立する。

1. **持続的な役割 identity signal が前提**。1 階層は「1 つの pane が実装、別の pane がレビュー」で
   あって、pane が自分の役割を取り違えれば分離は崩れる。2026-08-17、coordinator lane が一時的に
   自役割を implementer と誤認する事象が起きた (#15152 j#107023 → 訂正 j#107026 / j#107028)。
   これを構造的に塞ぐため「役割 identity を MCP 応答へ毎回埋める (computed / fail-loud /
   reminder-not-auth)」を US #15601 として起票済み。1 階層の既定化は、この identity signal の
   常設と**セット**で初めて安全になる。
2. **「coordinator lane が実装も行う」process gap**。この repo の役割規則は実装を専用レーンへ
   dispatch させたい (central preset `### 既定役割` / project-local role boundary)。1 階層では
   coordinator pane が実装も担うため、この規則と緊張する。本 ADR はこれを**既知の緊張として記録**
   する — 1 階層に対して明示的に許容するか、専用実装レーンへ dispatch する形で解消するかは、
   本 ADR の active 化時に owner が決める open な論点であり、黙って許容済みとはしない。
3. **実装者 / レビューア分離は load-bearing であって儀式ではない**。#15152 の review 往復
   R7 → R8 → R9 で、実装者 (claude) は欠陥を繰り返し持ち込み、それを毎回捕えたのは別系統の
   cross-family reviewer (codex) だった。具体的には「型注釈を runtime の保証と誤認」した欠陥
   (j#107053 item 4、R7 で 3 件混入 → codex が全件捕捉 → R8 commit 55f79634 で修正)、および
   同じ #15152 span で続いた bool の真偽値依存 (bool-truthiness)、caller-echo binding の取り違え。
   これらは実装者の self-review では素通りし、系統を跨ぐ敵対的レビューだけが止めた。分離は
   ceremony ではなく、実際に欠陥を止めている実働境界である。

## 根拠 (逐語引用)

- owner ratification、#15578 j#107064 (2026-08-17 chat 逐語):「OK、OK。じゃあやっていこうぜ。
  承認するよ。」
  - context (同 journal): coordinator が「ratify 待ち」として (a) always-rule 文面、(b) 運用モデル
    ADR の file 化、(c) モデル配置 ADR の identity 確認、(d) v2.1 再割当、を列挙した直後の blanket
    承認。coordinator は同 journal でその scope を「proceed: (b) 運用モデル ADR を
    `vibes/docs/adr/` へ file」「hold: (c) モデル配置 ADR の Fable5 特定部分 (identity 未検証)」と
    解釈している。したがって owner が承認したのは *file 化を進めること* であり、本 ADR の exact
    text ではない。よって本 ADR の status は proposed であり、文面の active 化は owner の別途裁定を
    要する。
- 運用モデル決定の捕捉、#15578 j#107053 item ① (coordinator decision-capture、2026-08-17。owner
  discussion を coordinator が durable 化したもので、owner の逐語ではない):「運用モデル:
  1階層/2役割 を小規模の既定にする … 実装者 + レビューア(別モデル系統)の2役割・1階層。
  サブレーン/プロジェクトコーディネーター無し。… 規模で 1↔3階層を切替。… 1階層でも
  実装者/レビューア分離は必須」。
- モデル配置の open item、#15578 j#107053 item ②:「open item: `.mozyo-bridge/config.yaml` の
  coordination.launch_argv は `claude-fable-5` に pin されているが、本 lane の runtime は
  `claude-opus-4-8` を報告。実体モデルの確認が必要 … 確認まで本項の ADR 化は保留。」

## 影響

- **既定と escalation**: 小規模 / 上級モデル作業は 1 階層 / 2 役割を既定に開始する。並列・スケール
  要求が生じたときにのみ 3 階層 (委任コーディネーション) へ上げる。3 階層⇔1 階層の切替は規模の
  関数であり、既定ではない。
- **分離不変**: 実装者とレビューアの分離は 1 階層でも外さない。規模縮小を理由に「1 pane が実装と
  レビューを兼ねる」構成へ退化させない (#15152 の退行がこの分離の実効性を示す)。cross-family
  (Claude 系 + GPT 系) を跨ぐことで盲点も分散させる。
- **モデル配置の方向 (identity は open item)**: 配置の**方向**を次のとおり記録する — coordinator は
  Claude 系統のモデル、reviewer / auditor は codex (GPT 系統。系統を跨ぐ敵対的多様性と規則遵守の
  ため)。**Opus5 は coordinator 席に置かない**。ただし coordinator の**具体モデル identity は本 ADR
  で未確定**とする。committed config `.mozyo-bridge/config.yaml` の `coordination.launch_argv` は
  `claude-fable-5` に pin されているが、当 lane の live runtime は `claude-opus-4-8` を報告しており、
  どちらが実体かは未検証である。本 ADR は「Fable5 が coordinator である」を確定事実として主張
  しない。identity の確定には owner が pane の起動設定 (どのモデルで起動したか) を確認する必要が
  あり、確認後に addendum として固定する (#15578 j#107064 hold (c))。
- **前提となる identity signal**: 1 階層の安全な運用は「役割 identity を MCP 応答へ毎回埋める」
  (#15601) の常設を前提とする。identity signal 無しに 1 階層を既定化すると、role 誤認 (#15152
  j#107023 の実例) を構造的に塞げない。
- **未解消の process gap**: 「coordinator lane が実装も担う」点は既知の緊張として open。active 化
  時に owner が「1 階層では許容」または「専用実装レーンへ dispatch」を裁定する。それまでは黙って
  解決済みと扱わない。
- **status と gate**: 本 ADR は proposed。owner の文面裁定 anchor が成立するまで active な ADR とは
  扱わない。active 化後は、本 ADR と矛盾する「1 階層でも実装者/レビューア分離を外す」変更、および
  「モデル identity を未検証のまま確定事実として固定する」変更は `adr_conflict_gate` の対象となる。
- **適用範囲**: 本 repo (repo-local 宣言)。中央 preset / OSS 配布物は本 ADR では変更しない。
