# Redmine Issue 記載粒度と Version 運用 Reference

LLM が迷わず ticket を起票し dispatch 候補を選べるようにするための、Epic / Feature / UserStory / leaf issue の記載粒度と Redmine Version 運用の判断ロジック (Redmine #13024)。本 reference は portable な authoring / planning guideline を持つ。gate 語彙・必須 journal field・close 条件は central preset、subject / description の機構は `references/workflow.md` `### Issue の subject / description 分離` が正本であり、重なる箇所は再掲せず pointer にする。

## 階層粒度の判断表

tracker は「作業の大きさの感覚」ではなく「その record が何のためにあるか」で選ぶ:

| 階層 | 何であるか | 完了の意味 | 起票する場面 |
| --- | --- | --- | --- |
| **Epic** | product portfolio 上の長寿命な投資領域 | 作業完了単位ではない。通常は年単位で open のまま | 恒久的な product / governance 領域に portfolio node が必要なとき |
| **Feature** | Epic 配下の継続的な機能カテゴリ | 作業完了単位ではない。通常 open のまま | Epic 内のある能力に UserStory が継続的に積まれていくとき |
| **UserStory** | 標準の作業・受け入れ単位 — 概ね `1 US = 1 branch / 1 worktree / 1 PR 相当` | review / owner close approval / Close Gate で close (central preset `## Completion`) | 作業を計画・dispatch・受け入れするとき |
| **Task / Test / Bug (leaf)** | US scope **内部** の内訳・検証・不具合対応 | replayable な implementation_done journal + commit record で close (task_close) | US に監査可能な sub-record が必要なとき、または preset の task-level 例外に該当するとき |

- **Epic / Feature はカタログであり、queue ではない。** 「この領域はまだ product の一部である」ことを表す。直近の UserStory が完了したことを理由に close せず、explicit な owner / operator decision なしに実装単位として dispatch しない (governed preset が配布する work-unit granularity 契約を参照)。
- **UserStory が標準の作業単位** (`1US = 1作業単位`)。1 実装者が配下の Task / Test / Bug を 1 lane で一気通貫できる形 — branch / worktree / PR 相当 1 つ分の scope — に収まるよう計画する。収まらない US は umbrella 化せず複数 US に分割する。
- **leaf issue は従属物。** US を構造化するために作り、独立した作業として起票しない。leaf 単体の dispatch は preset の task-level 例外であり、該当理由を dispatch decision journal に残す。
- **順序 prefix convention (推奨)。** Epic と Feature の番号 prefix はそれぞれ独立した系列とし、10 刻みの prefix (`110`, `120`, `130`, ...) は workflow 上の読み順 — agent がカタログを走査すべき順序 — を表す。優先度や進捗ではない。刻みの隙間は後から領域を挿入しても振り直さないための余白である。採用 project は別の順序 convention を使ってよいが、いずれにせよ表示・読み順に留め、identity や routing の key にしない。

## 原文要点・経緯・Normalized Intent

owner との会話から生まれる ticket の description は、1 つに溶かした要約ではなく、分離した節を保つ:

- **原文要点 (owner utterance digest)** — owner が実際に言ったことの軽い要約記録。要約はよいが、思想・方針・懸念を落とすことは不可。後の作業が ticket と矛盾して見えるとき、仲裁が読むのはこの節である。**発話を置く節であり、出来事を置く節ではない。**
- **経緯 (trigger and lineage)** — このチケットが存在する理由の最小記録: 契機となった出来事 (1〜3 bullet) と、系譜の durable anchor (親 / 先行 issue id、起点となった調査・決定 journal id)。**narrative ではなく pointer で書く** — journal に既にある経緯を description へ物語として複製すると、チケット単位の二重管理 drift を再生産する。詳細を読みたい読者は anchor を辿る。
- **Normalized intent** — 実行可能な言い換え: 実装者が実行できる scope の言葉で「何をする作業か」を述べる。

Scope / Close conditions / Non-goals は normalized intent から導く。後からの再解釈に合わせて原文要点を書き換えない。追記で対応する。

**発話のない派生チケット** (調査 follow-up、親 US からの分割、機械的な連作 slice など) では、原文要点を擬似発話で埋めない — 欄を埋めるための創作は監査の錨を壊す。代わりに derivation source (派生元の親 US / 調査・決定 journal) を経緯に記録し、原文要点は「なし (派生元: #NNNN j#NNNNN)」としてよい。

## UserStory の Close Conditions と Acceptance Notes

会話駆動の運用でも owner に聞き直さず検証できるよう、close condition は次の形で書く:

- 各 close condition は repo / docs / durable record について**観測可能な文** (「X が Y に記載されている」「Z が pass する」) で書く。感想 (「うまく動く」) にしない。
- リストは短く保つ (目安 3〜6 項目)。それを超えるなら、その US はおそらく 2 つの US である。目安は上限側の分割サインであり、下限は padding の理由にならない — 1 項目で十分に観測可能な小さな US に体裁のため項目を水増ししない。
- **Non-goals も受け入れの一部。** 意図的にやらないことを明記し、audit が「やっていない部分」を gap と誤読しないようにする。
- 重複より境界参照。兄弟 US が扱う話題は再説明せず「配置は #NNNN の scope」のように名指しで参照する。

## Version 運用

### 語彙 (Redmine Version の役割の書き分け)

Redmine Version の役割は 2 語で書き分ける。

- **候補範囲 (candidate grouping)** — dispatch 候補の探索・planning・readiness window の入れ物。「current Version 内の ready US を優先的に見る」というときの、その見る範囲がこれである。
- **authority ではない** — dispatch / hold / 直列化 / 並列化の判断は durable-record gate と dispatch decision が持つ。Version 所属それ自体は判断の根拠にならない。

本 doc および過去記述で使う「lane-inventory bucket」「Version-as-inventory」「(roadmap / milestone / acceptance の) grouping surface」等の表現は、すべて **候補範囲** の言い換えである (規範は同一で呼称のみ異なる)。歴史記述・既存 journal を遡及的に書き換えない。

**裸の「バージョン」を使わない。** workflow docs / journal / narrative では **Redmine Version** / **release tag** / **package version** のいずれかを常に修飾して書く。裸の「バージョン」「Version」単独は Redmine Version・package release 番号・tag のいずれとも読めて誤読を生むため、散文では使わない (code 識別子・CLI 名・field 名・既存 journal の引用は literal のまま維持する)。

Redmine Version は **planning / release-readiness / lane-inventory bucket (= 候補範囲)** — リリース計画、スプリント相当の grouping、readiness window、lane dispatch の主候補範囲 — である。**package version の正本ではない**: 出荷される version 番号の正本は tag / package metadata / release notes / release journal であり、Version 名に将来の package 番号を先入れしない。

サイズと lifecycle:

- **1 Version は概ね 10〜20 UserStory を目安とする。** 小さすぎる Version は sublane のチケット在庫を枯らし、大きすぎる Version は readiness window として機能しなくなる。その兆候で分割・統合する。
- **Version は候補がある状態で作り、先回りして作らない。** 空の Version を placeholder として先行作成しない。埋めるべき候補 US が存在するときに作成・選択する。
- **follow-up は関連する既存 Version を優先する。** follow-up US はテーマを継続する Version に入れる。follow-up の波ごとに新規 Version を乱造しない。

dispatch 候補の選定:

1. **current Version 内の ready UserStory を優先する** — Version-as-inventory model の目的そのものである。
2. current Version の ready 在庫が枯れたときのみ、関連 Feature 配下の US または隣接する (テーマ継続の) Version から補充し、**補充理由を dispatch decision journal に残す**。
3. Version 所属は durable-record gate を上書きしない: US が dispatch 可能なのは record が ready だからであり、どの bucket にいるかではない。Version が同じ / 違うこと自体は直列化・並列化いずれの理由にもならない。

## 境界

- gate 名、必須 field、review / close の意味論: central preset (本 doc は gate 語彙を追加しない)。
- subject / description の authoring 機構と explicit-subject-on-create rule: `references/workflow.md` `### Issue の subject / description 分離`。
- 本 guideline 自体の配置 (配布 body か採用 repo の local docs か): `references/workflow.md` `## Workflow docs の正本境界` (Redmine #13025)。repo 固有の Version 名、具体の Epic / Feature カタログ、workspace の番号 prefix 採用事実は repo-local の事実であり、本配布 body には置かない。
- operator 固有 policy を OSS default に入れない: 10〜20 の目安を超える具体の在庫しきい値、private な優先順位付け、業務ドメイン名は public / private boundary rule に従い operator の runbook 側に置く。
