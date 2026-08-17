# ADR-0005: adversarial review mode は宣言脅威モデルで収束させ、圏外指摘は保留記録する

- status: active
- date: 2026-08-17
- related: Redmine #15553 (導入 US、owner 決定の逐語 anchor は同 issue description)、#15537 (12 round 化の実例)、ADR-0001 (レビューは owner 決定を上書きできない)、ADR-0004 (review depth tiers)

## 決定 (規約行)

full_surface_adversarial mode の審査対象は review request が**宣言した脅威モデル**で区切る。
脅威モデル内の反証は従来どおり material (changes_requested 根拠)。脅威モデル外の指摘は
差し戻し根拠にせず **deferred finding** として記録し (今はやらない判断を明示、wontfix-by-policy
も正当な出口)、脅威モデル自体への挑戦は implementer / reviewer の往復で裁定せず owner へ回す。
テスト基盤のみを対象とする Medium 以下の finding が 2 round 連続したら、coordinator 経由の
owner 判断を**必須**とする (任意の気づきではなく規則)。escalation の入口条件は変更しない。

## 背景

#15537 (busy Enter 再送) は R12 まで 12 往復を要した。escalation gate (Late-Finding
Full-Surface Adversarial Sweep) の入口は正しく機能し、R6 で派生投影層の High
(finding_busyprojection — 正規 busy 配送が front door / callback で失敗へ戻る実害) を発見した。
一方で mode には出口条件が無く、R8-R11 の 4 round は検査対象が product → 契約文 → guard test →
guard の抽出器 → 抽出器の docstring と望遠鏡状に後退する「guard の bait 耐性」攻防に費やされた。
各指摘は技術的に正しいが、脅威モデル (将来の保守者による偶発的な契約巻き戻し) の外にある
「repo 内の故意の bait 工作への防御」は、committer が我々自身であるこの repo では費用対効果が
恒常的に薄い。収束は規則ではなく reviewer のネタ切れ (R12 approved) で起きた。また implementer
は R10 で懸念を chat に表明しながら de-escalation を起動せず続行した — LLM agent の既定動作は
従順な続行であり、判断ポイントは規則として強制しなければ機能しないことの実例である。

## 根拠 (逐語引用)

- #15553 issue description (owner 決定の逐語記録、2026-08-17): 「プロセス設計か。まあ、あなたの
  ミスをCodexが指摘したっていうのは、もうこのシステムの核心というか、存在意義だから、かなり
  いいだろう。ただし、再現のないエスカレーションっていうのは、ちょっとどうしたもんかなって
  いう感じかな。」(文脈上「際限のないエスカレーション」への問題意識)
- #15553 issue description (同、draft 提案への承認と記録方針): 「OK、ではそうしよっか。また、
  指摘自体も別に記録しておいてはいいと思うんだよね。いずれ直してもいいとは思うからさ。ただ、
  限界費用、効用が低い、あるいは効用が低いという費用対効果の薄いものについては、今はやらない
  とか、そういう判断をするべきで、残しておくことに価値はあると思う。」

## 影響

- **入口は不変**: central preset `### Late-Finding Full-Surface Adversarial Sweep Escalation` の
  deterministic trigger (late authority finding の反復 → full_surface_adversarial 昇格) は
  そのまま。#15537 R6 の High はこの昇格が発見した実績であり、弱めない。
- **脅威モデル宣言**: guard / 検証機構を成果物として主張する review request は、その脅威モデルを
  明示する (例:「偶発的な契約巻き戻しの検出が対象。故意の回避工作は対象外」)。宣言を欠く request
  への guard 指摘は従来どおり全て material (fail は深い側)。実行契約は
  `vibes/docs/rules/agent-workflow.md` の `adversarial_convergence` が正本。
- **内外の裁定**: 宣言脅威モデル内の反証 (言い換え残存で green 等) は material。モデル外の指摘は
  changes_requested の根拠にせず deferred finding として review journal に記録する。reviewer が
  「脅威モデル自体が甘い」と主張する場合、それは policy 論点であり、owner-question bypass 禁止の
  正規導線 (coordinator 経由) で owner 裁定へ回す — ADR-0001 (レビューは owner 決定を黙って
  上書きできない) の延長として、モデルの甘さの裁定者は往復の当事者ではなく owner である。
- **必須 de-escalation trigger**: テスト基盤 (tests/** の guard・検証 tooling) のみを対象とする
  severity Medium 以下の finding が 2 round 連続したら、implementer は続行せず coordinator 経由で
  owner 判断を仰ぐ (懸念の chat 表明では足りない。#15537 R10 の実例)。
- **deferred finding の記録**: 置き場は review result journal (個別 issue の乱発はしない)。
  再評価トリガーは時間ではなく事象条件 (当該 surface の次回変更時 / 脅威モデルへの再挑戦時)。
  後日の再評価で wontfix-by-policy と結論することも正当な出口 — 記録の価値は判断の追跡可能性で
  あり、TODO の約束ではない。
- 本 ADR と矛盾する「収束規則を外して無期限に adversarial round を続ける」変更、および
  「deferred 記録を省略して指摘を黙って落とす」変更は `adr_conflict_gate` の対象。
