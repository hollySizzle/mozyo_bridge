# External-client coordinator proxy (Redmine #14546)

**external coordinator client**（attested lane agent ではない operator shell / API caller）が、
**既に durable に確定した** high-level action を live attested default coordinator へ
**一回だけ**委譲するための public rail の contract。設計正本は本 doc、role authority 側の正本は
`spec-herdr-default-lane-workflow-role-authority`。

## 1. 解く問題（observed dead end）

external client から到達できる既存 entrypoint は 2 つとも、effect の**前に**正しく停止する
（#14500 observed facts / #14546 j#89697・j#89712 で live 再現）:

- `mozyo-bridge workflow step` → `lane_unresolved / herdr_sender_identity_unresolved`
- `mozyo-bridge sublane create --execute` → pre-effect blocked（`missing_identity` +
  `sender_attestation`）、mutation は worktree 0 / branch 0 / pair 0 / dispatch 0

この停止自体は**正しい**。caller には launch-time sender identity が本当に無い。問題は
**第三の道が存在しなかった**ことで、その結果残る「前進手段」は次の 2 つだけだった:

1. `MOZYO_*` を手で export する = identity の偽造
2. coordinator の pane へ直接入力する = audit boundary の迂回

どちらも、上記 gate が守っている境界そのものを壊す。本 rail はその第三の道である。

**本 rail は gate を緩めない。** caller に identity を与えるのではなく、**caller が主張できない
もの**だけから authority を毎回導出する。委譲されるのは *decision* であって、*それを実行する
authority* ではない — 実行するのは coordinator 自身の attested runtime であり、`sublane create`
は依然 coordinator 自身の sender attestation を要求する。

## 2. Authority chain（action-time に毎回再導出する）

順序は評価順であり、報告される reason は**最初に壊れた link** である。

| # | link | 出所（caller が主張できないもの） | fail-closed reason |
| --- | --- | --- | --- |
| 1 | action | closed vocabulary との照合 | `proxy_action_unknown` |
| 2 | workspace | repo checkout の registry anchor（`herdr_workspace_segment`） | `proxy_workspace_unresolved` |
| 3 | role | repo-local durable role authority の default-lane binding | `proxy_coordinator_authority_missing` / `proxy_coordinator_authority_blocked` |
| 4 | provider | 当該 role の `provider_binding` | `proxy_provider_unresolved` |
| 5 | target | live inventory の **mzb1 assigned name** decode **＋ generation-bound startup self-attestation の join** | `proxy_target_missing` / `proxy_target_ambiguous` / `proxy_target_locator_missing` / `proxy_target_unattested` |
| 6 | anchor | Redmine 上の decision **×** lane lifecycle authority の live facts（§3） | `proxy_anchor_unverified` / `proxy_anchor_action_mismatch` / `proxy_anchor_decision_incomplete` / `proxy_anchor_lane_unresolved` / `proxy_anchor_scope_mismatch` / `proxy_anchor_generation_stale` / `proxy_anchor_superseded` |
| 7 | fence | dedicated exactly-once store | `proxy_duplicate` / `proxy_stale` / `proxy_fence_reconcile_required` / `proxy_fence_unavailable` |

不変条件:

- **caller env は authority ではない。** `MOZYO_WORKSPACE_ID` / `MOZYO_AGENT_ROLE` /
  `MOZYO_LANE_ID` を authority として読まない。fallback としても読まない。workspace が解決
  できない場合は `proxy_workspace_unresolved` で停止し、caller の主張へ退避しない。
- **cross-workspace は構造的に不可能。** target は agent 自身の assigned name の decode で選ぶ
  ため、foreign workspace の row は「選ばれない」のではなく「候補にならない」。
- **decode は necessary であって sufficient ではない。** assigned name はその slot が「何として
  launch されたか」を表すにすぎない。実際にその identity で boot し、いまその live locator を
  占有していることを attest するのは generation-bound な startup self-attestation record だけで
  ある。よって単一候補は既存 read-side policy `evaluate_attestation`（adopt classifier / doctor と
  共有。二重実装しない）で join し、record が absent / stale（別 process generation）/ conflict /
  missing なら `proxy_target_unattested` で zero-send する。**attestation store が読めない場合も
  `unattested`** であり、name 一致へ decay しない。
- **duplicate identity は ambiguity であって選択肢ではない。** 同一 (workspace, provider,
  default lane) に 2+ live agent がある場合、どちらへ送っても推測になるため zero-send。
- **fence は最後に評価する。** target 不正 / anchor superseded で拒否される委譲は generation を
  消費しない（修正後に同じ decision を委譲できる）。
- **順序は美観ではない。** 最初に壊れた link を報告することと、拒否が何を消費するかは同じ順序
  で決まる。

## 3. Anchor 検証 — authority の単位は **(action, journal, lane generation)**

action を closed vocabulary で検証し、journal を marker 集合で検証しても、**両者を突き合わせない
限り「決定」を検証したことにはならない**。初版はこの join を欠いており、任意の in-vocabulary
action が任意の gate journal に乗れた（`implementation_done` が `dispatch_next` を authorize でき
た）。さらに token と journal が一致しても、その決定が **どの lane のどの generation** を
authorize したかを照合しなければ scope は未検証のままである（review j#89918 F2）。authority の
単位はこの三つ組であり、どれか一つずつではない。

- **action → 決定 token の closed map**（`ACTION_DECISION_TOKENS`）と **action → scope の closed
  map**（`ACTION_SCOPES`）を持つ。`dispatch_next` = `lane_scoped`、`bootstrap_lane` = `issue_scoped`。
- **読むのは invocation が名指した journal 1 件だけである。** issue 履歴の token scan は廃止した
  (Design Answer j#90329 契約 5)。履歴 scan は 2 つの失敗の根だった: issue 上のどこかにある**引用**が
  candidate になり、その対策として入れた「2 件以上は ambiguity」が今度は issue を**恒久的に使用不能**
  にした。名指し journal だけを見れば、どちらも起こらない。
- **canonical grammar** (producer: `render_bootstrap_decision_marker()`):
  - **canonical な行だけを scan する。** canonical decision とは coordinator が**自分の声で**書いた
    指示であり、Markdown が「引用」「逐語」として描画するものは定義上それではない。規則の**正本は
    共有 domain authority `domain/canonical_note_scan.py`** であり、この rail と Redmine journal
    reader (`domain/redmine_journal_source.py`) の**両方がそれを呼ぶ** (#14585)。以下は同 authority
    が全行に同順で適用する規則である:
    - **A. fenced code** — ` ``` ` / `~~~` の opener から closer まで (fence 行を含む)。閉じていない
      fence は以降を全部飲む (半開の引用も引用である = fail-closed)。
    - **B. blockquote** — 先頭の非空白文字が `>`。nest (`> >`) と leading whitespace に加え、
      **そこから lazy に継続する paragraph** を含む (`> quoted` の次行に blank line 無しで続く行は
      同じ blockquote の中である。CommonMark 0.31.2 §5.1)。
    - **C. indented code** — 4 **column** 以上の indent。tab は 4-column tab stop へ展開する (§2.2)。
      ただし **indented code は paragraph を interrupt できない** (§4.4)。開いている paragraph の中の
      4-column 行は hanging indent であり、**paragraph を切らない** (切ると span state が失われ、
      delimiter 間の marker が解放される)。当該行自体は従来どおり blank 化する。
    - **D. inline code** — backtick span。**同一 paragraph 内であれば opener と closer は別の行に
      あってよい** (§6.1 は span の line ending を space へ正規化する)。
    - **E. raw HTML** — **tokenize しない。** 他の規則が blank した後に残った text に escape されて
      いない markup 起動 (`<` + letter / `!` / `?` / `/`) が現れたら、**その行以降 note 末尾まで**を
      拒否する。tag 集合も nesting depth も attribute も comment も持たない。
      ★★★ここは 3 世代「HTML を少しずつ model する」設計で失敗した (#14584 j#91406 F3 = tag 未対応、
      j#91593 F2 = block type 2-5 / 属性 / comment 未対応、j#91593 F3 = nesting と attribute 内の
      close-like text)。**部分実装の HTML parser を authority 判定に置くこと自体が defect** であり、
      次の token 種で同じ finding が出る。よって「どこで markup が終わるか」を答えるのをやめ、
      「markup が始まるか」だけを見る。**判定は最後に行う** — code span や fence の中の `<tag>` は
      既に blank 済みなので費用ゼロである。
      なお **marker が comment / 属性値の中にある場合、描画結果に一切現れない** (`pandoc -t plain`
      で消える)。「引用」ですらなく**不可視の文字列**が gate authority になっていた。
    - **F. link 構文** — destination と title `](…)`、reference label `][…]`、reference definition の
      末尾 `]: …`、image の alt text `![…]` の起動位置から **paragraph 末尾まで**。marker をここに
      書くと URL / attribute になり、**散文としては描画されない**。
      ★★★**E と同様 tokenize しない。** 一度 tokenize する版を書いたが、reference definition を
      **物理行末**で閉じ、`(` / `)` を1つの depth に数え、**quoted title と angle destination を
      知らなかった** — この3つの近道が**それぞれ別に** marker を解放した (#14584 j#91682 F1)。
      §4.7 は destination / title が**次行から始まる**ことを許し title は複数行に渡るので、
      行単位の規則は原理的に正しくなり得ない。
      ★★★**拒否の scope と実装方式は独立である。** paragraph 止まりにしたのは、note 末尾までの
      拒否を live journal で実測して **実 gate event を7件落とした**からであって (`[P1][documented_rule …]`
      は review 散文)、有界にしたいことは **parse を始めてよい理由にならなかった**。paragraph 境界は
      既に block 構造として決まっており、tokenizer を必要としない。
    - **§2.4 backslash escape** — escape された delimiter は literal であり delimiter ではない。
      escaped backtick を run に数えると、その run が後続の**本物の opener と対になり**、実際に span を
      形成していた 2 delimiter の間が blank されなくなる (j#91593 F1)。`\<` も同様に markup を起動しない。
    - **字類と line ending。** block 構造上の空白は **U+0020 と U+0009 のみ** (§2.1)。Python の `\s`
      は Unicode 空白全部に一致するため、blank line 判定・fence closer・interrupter のいずれでも
      NBSP / EM SPACE / form feed を空白と誤認し、開いている引用を閉じてしまう。**line ending は
      先に正規化する** (Redmine は CRLF を返す。正規化しないと厳格化した字類の下で全 fence closer が
      閉じなくなる)。**CRLF でない単独 `\r` は note 全体を拒否する**: spec は line ending と定め、
      pandoc は分割しない。どちらの読みにも他方が拒否する形があり、**構造が renderer 依存 = 著者性を
      確定できない**。
    ★★★**B / C / D はいずれも「1 行だけを見て決まる性質」ではない。** 行に自分自身を尋ねる実装は
    毎回、尋ねなかった形を漏らした (#14584 j#91194 F1-F3): indent を文字数で数えると Markdown が
    4 column と読む ` \t` を 2 と読み、`>` 行だけを見る blockquote 判定は次の 1 行を解放し、行末で
    閉じる span 判定は delimiter 間の全行を解放する。よって scan は **block 構造を先に確定し、D は
    paragraph 単位で後から適用する**。
    ★★★**A と D は delimiter の規則であり、delimiter は「一致したときだけ」delimiter である**
    (#14584 j#91152 F1)。fence を単一 boolean で toggle し、span を「任意の backtick 2 個の間」と
    読む実装は、renderer が逐語として描画した region を canonical text として返す。CommonMark
    0.31.2 §4.5 / §6.1 に従い delimiter identity を保持する:
    - fence の closer は opener と**同じ文字**かつ**opener 以上の長さ**で、後続は空白のみ。info
      string を持つ行は closer ではなく content である (` ``` ` 行は ` ```` ` block を閉じない)。
    - backtick fence の opener の info string は backtick を**含めない**。この規則が無いと
      ` ```a`b ` が opener として読まれ、後続の**本物の opener が closer として作用**して、
      fence 内の marker が canonical text として解放される。
    - code span は backtick string から**ちょうど同じ長さ**の次の backtick string までであり、
      間にある別長の run は span の content である。
    - **対応しない backtick string は無視せず、その paragraph の残りを拒否する。** CommonMark は
      逐語 text として描画するが、引用が閉じていない paragraph はこの scan が著者性を確定できない
      text であり、拒否は復旧可能な向きである (下記「代償」と同じ)。
    ★**A と D だけを覆った初版は live acceptance で破れた** (#14577 j#90392)。journal に `>` で
    grammar を引用しただけの note が `links.anchor=verified` を返し、zero-send になったのは後段の
    別 link がたまたま壊れていたからにすぎない。**引用の形は 1 つではないので、報告された形だけを
    塞ぐと次の形が残る。** B と C は同じ class として同時に塞ぐ。
    ★★さらに、**この規則を持っていたのはこの rail だけだった** (#14577 j#90416 F1 / #14585)。同じ
    grammar を読む sibling parser `redmine_journal_source` — `workflow watch` / callback discovery /
    `workflow step` の anchor gate が通る read boundary — は raw note を scan したままで、そこでは
    引用 marker が durable gate authority になった。**同じ grammar に対して「引用とは何か」の定義が
    2 つあるのは drift 生成器である。** 規則は共有 authority に 1 箇所だけ置き、reader は policy
    (どの channel / gate を受理するか) だけを各自が持つ。
    ★★★**列挙者を自分に置く限りこの漏れは反復する** (#14584 j#91194)。同じ面で手作業の列挙が
    3 round 連続で漏れた — B/C 漏れ (#14577 j#90392)、delimiter 漏れ (j#91152)、block 構造漏れ
    (j#91194)。**「規則の側から掃いた」も、掃く規則を自分で列挙している限り同じ失敗である。**
    以後この面の検証は **実 CommonMark 実装を differential oracle として使う**。renderer は**検証時の
    instrument であって runtime 依存ではない** (package に足さない)。
    ★★★**oracle の述語を間違えると、corpus をいくら回しても差分は出ない** (#14584 j#91593 F2)。
    「`<code>`/`<pre>`/`<blockquote>` の内側でないこと」を canonical の定義にしていたため、comment や
    属性値の中の marker は「引用されていない」と判定され、651 shape を通しても検出できなかった。
    正しい述語は **「marker が可視の散文テキストとして描画されること」** である。可視性は **HTML の
    text node** で判定する — tag と comment を除去した残りに marker が現れること、かつ位置が
    verbatim / quotation element の内側でないこと。
    ★★★**「plain text 出力に残るか」は proxy であり、それも誤りだった** (#14584 j#91682 F2)。pandoc は
    image の `alt` を本文へ昇格するため、属性の中の marker が「可視」と判定された。**text node かを
    問えば `title` / `alt` / `href` を個別に列挙する必要がない** — 属性は定義上 text node ではない。
    **proxy で述語を書くと、proxy が壊れる形を別途列挙する羽目になる。**
    ★★★**述語は 1 箇所にしか置かない。** corpus harness が旧述語のコピーを持ったままだったため、
    oracle を直した後も corpus は旧規則で採点し続けていた (#14584 R5 自己検出)。**scanner に対して
    「同じ grammar の定義が 2 つあるのは drift 生成器」と書いたのと同じ誤りを、検証側で犯した。**
    ★★★**「描画結果に現れない」を「判定不能」として skip しない。** harness は marker が render 出力に
    無い場合を skip していたが、それは**最も強い「書き手の声ではない」証拠**である。skip を廃止したら
    link destination / title 内 marker の under-blank が 6 件出た。
    ★★★**oracle を入れても、corpus の生成軸を自分で決めている限り同じ漏れが残る** (#14584 j#91406)。
    R3 は oracle を導入した上でなお字類・hanging indent・raw HTML の 3 軸を落とし、しかも
    「HTML 軸未実施」と自分の review_request に書いたまま head を出した。よって **corpus の生成軸は
    CommonMark の目次から起こす** — container (blockquote / list / HTML block)、leaf (fence /
    indented / ATX / setext / thematic break / paragraph)、inline (code span / raw HTML)、字類
    (Unicode 空白各種)、line ending (LF / CRLF / CR)。**未確認の軸が残っていると自分で書ける状態で
    review_request を出さない。**
  - 代償として **decision は top-level に書く**必要がある (4 column 以上 indent した marker、
    blockquote を lazy 継続する行の marker、対応しない backtick run を含む paragraph の marker、
    **markup 起動 `<` 以降の全行**、単独 `\r` を含む note は拒否される)。この向きの失敗は coordinator が
    blank line を挟んで column 0 に書き直す、または tag を backtick で囲めば済む。逆向きの失敗
    (引用に authority を渡す) は復旧できない。**実 journal への影響は live 実測で確認する** —
    20 issue で R3→R4 は 154 marker、R4→R5 は 166 marker、いずれも**集合が完全一致**した
    (硬化で落ちた実 marker は 0)。
  - ★★★**狭い拒否の pass は、広い拒否の pass がまだ読んでいないものを消してはならない** (#14584 j#91735)。
    E は note 末尾まで、tail 系 (未対応 backtick run / link 構文) と hanging-indent blank は paragraph /
    行までしか拒否しない。狭い方を先に走らせると、**E が note 末尾まで拒否する根拠だった `<code>` 自体を
    消してしまい**、その下の marker が authority に戻る。指摘は link tail についてだったが、実測したら
    未対応 backtick tail / image tail / hanging indent の**3 経路も同型**だった。
    → 順序を **「renderer が隠すものを隠す (A-C と *閉じた* code span)」→ 「E を読む」→ 「renderer より
    多く隠す (tail 系・hanging indent)」→ 「E を適用」** に固定する。
    **pass を足すたびに順序を考えるのではなく、広い拒否の観測を先に固定する。**
    ★★★**ただし E は link 自身の `<…>` を markup と読んではならない** (#14584 j#91761)。早く読むと
    destination / title がまだ text 上にあるため、`[docs](<https://example.com>)` だけで note 全体が
    拒否され、**実在する gate event と work anchor が消えた**。よって E は **各 link の「あり得る最小の
    hidden 領域」を mask した写しを読む**。
    ★★★**この近似が許されるのは、それが「E が*追加で拒否するか*」しか決めないから**である。
    j#91682 で link の parse を禁じたのは、その parse が **何を release するか**を決めていたからで、
    向きが逆である。**領域を小さく見積もれば over-blank (復旧可能)、大きく見積もれば under-blank
    (復旧不能)** なので、閉じ記号が見つからないときは **mask を空**にする。
    **同じ「近似」でも、release を決めるのか refusal を決めるのかで安全性の向きが反転する。**
  - **scan は行単位で行う。** marker body の grammar は `[^\]]*` で改行を跨ぐため、blank 化した note
    を 1 文字列として scan すると、canonical 行の閉じていない `[mozyo:` が引用行を越えて後続の `]`
    で閉じ、**どの 1 行にも存在しない marker** が成立しうる。
  - canonical 行上の workflow-event marker が**ちょうど 1 件**であること。0 件 / 2 件以上は fixed
    reason で拒否する。
  - marker は `proxy_action` field で**どの action を authorize するか**を明示する。欠落は
    `action_not_declared` で拒否。lane-scoped の場合は `lane` / `lane_generation` も持つ。
  - journal id は marker を持つ **entry 自身の id** を使う (marker の自己申告は使わない)。
- 他 journal の引用は **authority にも ambiguity poison にもならない。**
- 分類:
  - canonical decision が読めない (0 件 / 引用のみ / 読取不能) → `unverified`
  - 2 件以上 → `decision_ambiguous`
  - `proxy_action` が当該 action でない → `action_mismatch`
  - **lane-scoped**: lane / 数値 generation 必須 (`decision_incomplete`)、live lifecycle facts と
    exact-match (`lane_unresolved` / `scope_mismatch` / `generation_stale`)
  - **issue-scoped**: lane を名乗ってはならず、issue が active lane を持たないこと (`scope_mismatch`)

## 3b. Delivery terminality — ack は authority ではない (Design Answer j#90329)

**proxy の責務は「既に durable に確定した decision を live attested default coordinator へ
exactly-once で配送する」ところまでで終わる。** coordinator が action を実行したこと・完了したことは
証明しない。

- **positive delivery を CAS で記録できた `delivered` が、その durable decision に対する terminal
  success である。** 同一 `(issue, journal)` は generation の状態に関わらず**永久 duplicate**。
  strictly newer な canonical decision だけが次 generation を mint する。
- `delivered` は「coordinator へ配送した」だけを意味し、action 実行・処理・成功を含意しない
  (`logic-ack-completion-receiver-state` の delivery ACK / completion 分離)。
- **`proxy-ack` は authority から除外した。** command は compatibility のため残すが
  **deprecated read-only no-op** で、fence state も次 decision admission も進めず非ゼロ終了する。
- **caller env / action id 所持 / bare Redmine ack marker / Redmine author のいずれも completion
  authority にしない。** 本US内で authority を env → durable record と 2 度移したが、いずれも同じ
  actor class が生成できるとして否定された。現行 transport は writer identity を運ばず、Redmine
  user → runtime role の写像も無い。成立しない issuer 証明を実装する代わりに **authority 境界を
  縮めた**。
- 既存の `completed` row は legacy terminal として読めるまま残すが、新規の authority 判断には
  使わない。
- **named journal は durable work intent であり、coordinator runtime issuer の証明ではない。**
  この非対称は意図的である。

## 4. Exactly-once fence（`core/state/coordinator_proxy_fence.py`）

route key = `(workspace_id, lane_id, role, action)`。target の live assigned name は action-time
attestation であり key に含めない（target rename が generation を進めない）。row は委譲した
**durable decision**（`issue` + `journal`）を保持する。これが sibling fence との決定的な差で、
理由は caller の retry 形態にある — caller には runtime が無いので「もう一度 command を叩く」が
通常の retry である。

- **decision は一度だけ委譲される。** 同一 `(issue, journal)` は generation がどの状態
  （`delivered` / `abandoned` / legacy `completed` / `uncertain` / `reserved`）に達していても
  **永久 duplicate**。この判定は state 分岐より**前**に行う。terminal state が、それを生んだ
  decision 自身を再開できてはならない。
- **supersede しない journal は stale。** Redmine journal id は整数なので比較は**数値**で行う
  （文字列比較では `"9" > "10"` となり古い決定が新しく見える）。非数値 journal は fail-closed。
- state 集合: `reserved` / `uncertain` を **active**（次 decision を通さない）、`delivered` /
  `abandoned` / legacy `completed` を **terminal**（strictly newer decision が次 generation を
  mint できる）とする。`delivered` が terminal 側に居ることが §3b の contract そのものである。
- crash window（未解決の reserve）は `uncertain` へ遷移し、blind retry しない。
- **send が例外を投げた場合も typed uncertain に閉じる**（review j#90250 F3）。例外が escape すると
  outcome write ごと飛ばして generation が `reserved` のまま残り、これは何も自動解決せず安全に
  再送もできない状態になる。effect boundary 不明はまさに `uncertain` の意味なので、そう記録して
  typed 非 delivery を返す。
- store identity は DB-external `store_nonce` sidecar。**execution path は store を
  auto-bootstrap しない** — 損失後の silent re-create は、既に delivered な委譲の再送を許す。
  init / recovery は `workflow proxy-fence --bootstrap` / `--recover` のみ。

## 4b. Reconcile — operator が**確定させた事実**だけを適用する (Design Answer j#90329 contract 4)

`uncertain` は「send が着いたかどうか不明」という唯一の未解決状態である。ack を authority から
外した以上（§3b）、この状態を product 側の自動判断で抜けることはできない。旧 reconcile は
`reserved` を `uncertain` へ落とすだけで、`uncertain` 自体は出口が無く route を恒久保持していた。

`mozyo-bridge workflow proxy-reconcile --action <token> --proxy-action-id <id> --issue <id>
--journal <id> --disposition <d> [--evidence <text>]`（既定 dry-run、`--execute` で適用）。

- `confirmed-delivered` — 着地を確認した。generation は `delivered`（terminal success）へ。
- `proven-not-sent` — send が出ていないことを確認した。generation は `abandoned` へ進み、route を
  **次の decision** に対して解放する。abandon した decision 自体を再送するわけではない（decision は
  一度だけ委譲される）。coordinator が次の canonical decision を出すのが正しい前進経路である。
- `unknown` — 何も確定していない。`reserved` を `uncertain` へ落として operator 待ちを可視化する
  だけで、terminal を主張しない。

fence 側の適用条件:

- 遷移は `route + proxy_action_id + 保存済み issue + 保存済み journal` に join する。異なる anchor を
  名乗った disposition は**何も変えない**（`proxy_reconcile_anchor_mismatch`）。
- terminal を主張する 2 disposition は `--evidence` 必須。何を確定させたかを述べない主張は受けない。
- `confirmed-delivered` / `proven-not-sent` は `uncertain` からのみ進む。着地済み `delivered` を
  `abandoned` へ戻すことはできない。

## 5. Delivery — 「送った」と「着いた」を混同しない

`custom` kind の通常の anchored `handoff send` を、解決済み locator + explicit target lane /
target repo へ 1 回だけ行う。preflight・receiver binding・landing gate はすべて通常どおり通す
（proxy であることが何かを緩めることはない）。新しい handoff kind token は作らない（kind
vocabulary は closed であり、委譲は implementation request でも review request でも consultation
でもない）。summary に action / durable anchor / opaque `proxy_action_id` を載せ、coordinator 側
の記録が何を渡されたかを相関できるようにする。summary は **ack を要求しない** — 求める応答動作が
無いことを明示する（§3b）。

**outcome write の CAS 結果は必ず観測する。** `mark_delivered` / `mark_uncertain` は generation が
まだ `reserved` の場合にだけ成功する CAS である。競合 retry が送信中に reserve へ再入すると row は
`uncertain` へ遷移し、その後 positive delivery が戻っても CAS は False になる。この結果を無視すると
caller には rc 0 を返しながら store は `uncertain` という、本 rail が防ぐべき唯一の失敗形になる
（review j#90032 F2）。**store が記録しなかった delivery は delivery ではない。** CAS False は
success にせず `proxy_delivery_uncertain` + 非ゼロ終了へ落とし、blind retry しない（前進は §4b）。

send が **発火した**ことは、着地した証拠ではない。positive delivery でない場合:

- generation は `uncertain` を保持し、blind retry しない（reconcile が先）。
- 結果は **delivery ではない**。`sent=False` / `reason=proxy_delivery_uncertain` を返し、CLI は
  **非ゼロ終了**する。caller は自前 runtime を持たず exit code で分岐するため、着かなかった委譲を
  成功として script させてはならない。rc 0 は positive delivery のときだけである。

## 5b. Executable leg wiring

resolver が `execution=ready` + direction 別 primitive を返しても、**CLI の executable-leg
classifier がその primitive を認めなければ何も発火しない**。初版は classifier が既存 2 token を
手書き列挙していたため、`herdr_forward_managed_gateway` は解決可能かつ発火不能で、`workflow step`
は rc 0 / `execution: ready` を返して何も送らなかった。本US の中核 acceptance がコード上到達不能
だった（review j#90032 F1）。

- classifier の membership は **route matrix から導出**する（`FORWARD_PRIMITIVES`）。手書き列挙は
  しない。direction を追加して executor を置き去りにすることが構造的に起こらないようにする。
- forward leg は専用 fence と専用 executor に乗るため、generic `WorkflowStepOutcome.executable`
  集合には**入れない**（そちらは tmux primitive rail）。両者の関係は coherence test で固定する。
- 非 dry-run で leg 1 回 / dry-run で 0 回 / leg の rc 伝播を **top-level CLI** で回帰化する。

## 6. Surface

- CLI: `mozyo-bridge workflow proxy --action <token> --source redmine --issue <id> --journal <id>`
  （既定 dry-run、`--execute` で 1 回配送）/ `mozyo-bridge workflow proxy-reconcile`（§4b）/
  `mozyo-bridge workflow proxy-fence`。`workflow proxy-ack` は **deprecated read-only no-op**
  として残るのみで、store に触れず非ゼロ終了する（§3b）。
- 実装: pure matrix `...f_140_delegated_coordinator_nested_handoff/domain/coordinator_proxy.py`、
  adapter `...application/coordinator_proxy_send.py`、CLI `...application/cli_workflow_proxy.py`、
  fence `core/state/coordinator_proxy_fence.py`。
- action vocabulary は closed: `bootstrap_lane` / `dispatch_next`。決定 token を tie できる action
  だけを置く（§3）。proxy が仕事を発明することはない。

## 7. 非 goal

- caller への identity 付与、`sender_attestation` の緩和、raw pane / raw Herdr の代替提供。
- **coordinator が委譲された action を実行した／完了したことの証明**（§3b）。proxy は delivery まで
  で終わり、実行は coordinator の attested runtime が自分の gate を通して行う。
- domain / design 判断、Redmine anchor の新規作成、Review Gate、owner approval、release、
  credential 操作の自動承認。
