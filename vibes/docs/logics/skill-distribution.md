# Skill Distribution Logic

## 方針

Claude / Codex 両対応は、共通 skill 本体と tool-specific adapter / packaging を分けて扱う。

- 共通本体 (canonical): `skills/mozyo-bridge-agent/`
- Claude Code project adapter: `.claude/skills/mozyo-bridge-agent/SKILL.md`
- Codex metadata: `skills/mozyo-bridge-agent/agents/openai.yaml`
- Claude plugin marketplace packaging: `.claude-plugin/marketplace.json` (repo root) と `plugins/mozyo-bridge-agent/.claude-plugin/plugin.json` + `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` (shared body の mirror)

## 配布経路

Claude Code 用の primary install は plugin marketplace 経由とし、Codex は canonical GitHub skill path に対する `$skill-installer`、CLI / rules は pipx + `mozyo-bridge rules install` を使う。curl/script による install は **legacy fallback** であり、新規 install では推奨しない。

- Claude Code (primary): `claude plugin marketplace add hollySizzle/mozyo_bridge` → `claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user`
- Codex (primary): `$skill-installer` に canonical path `https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent` を渡す
- CLI / rules: `pipx install mozyo-bridge` + `mozyo-bridge rules install`
- Legacy fallback (deprecated for new installs): `scripts/install_codex_skill.sh`, `scripts/install_claude_skill.sh`

## Legacy Project Claude Skill (`.claude/skills/mozyo-bridge-agent/`) Grace-Period Deprecation

repo にコミット済みの `.claude/skills/mozyo-bridge-agent/` 配下 (`SKILL.md` adapter stub + `references/{project-map,release,safety,workflow}.md` partial mirror; `git ls-files .claude/skills/` で 5 file 確認) は、`MOZYO_BRIDGE_CLAUDE_SCOPE=project` での legacy install / project root から起動した Claude Code が直接 load する経路を support する。これを **grace-period deprecate** に置き、即時 `git rm` 削除は行わない (Asana audit `1214732699548536` / `1214733817990357`)。

選定理由 (keep / remove / grace-period deprecate のうち grace-period deprecate を選定):

- 直近 commit `802a88243` (Asana task `1214779823377861`) が project-scope mirror に `references/safety.md` を意図的に追加したばかりで、その意図 (project root から起動する Claude Code セッションで shared skill body の partial mirror を提供する) を 1 release 以内に逆転させると churn が発生する。
- plugin marketplace path (`mozyo-bridge-agent:mozyo-bridge-agent` namespace) が新規 install の primary であることは確定 (`scripts/install_claude_skill.sh` 自体は `1214733632421625` で deprecation 通知済み) だが、既存の project-scope flow を中断する hard remove は audit recommendation R3 の "medium priority" の射程外。
- `keep` を選ばない理由: project skill と personal skill (`~/.claude/skills/`) の precedence gotcha は `1214732699548536` で audit 済み。plugin namespace が長期的に正しい解で、project-scope は段階的に縮小すべきという結論は audit verdict と整合する。
- `remove` を選ばない理由: 直近 commit を即時打ち消す churn + 既存 fallback 利用者の挙動変化 + tests/scaffold が project-scope install を前提とする箇所の同時改変が必要で、本 task の単発スコープを超える。

Grace period 中の運用:

- `.claude/skills/mozyo-bridge-agent/` 配下の tracked file (SKILL.md, references/project-map.md, references/release.md, references/safety.md, references/workflow.md) は当面残す。canonical 本体は `skills/mozyo-bridge-agent/` のまま。
- 新規 install では plugin marketplace 経由を推奨し、project-scope install は legacy fallback として扱う (本文書の `## Legacy Global Claude Skill Deprecation` 節と同じ位置づけ)。
- `scripts/install_claude_skill.sh` の default scope は `global` に移し、`MOZYO_BRIDGE_CLAUDE_SCOPE=project` は **明示 opt-in** とする (#12360)。bare invocation (env 未設定) は project scope を選ばなくなり、project mirror を書くのは明示的に `MOZYO_BRIDGE_CLAUDE_SCOPE=project` を渡した場合だけになる。project scope 動作自体は **当面残置** し、script header の DEPRECATED block + project 分岐の非致命的 stderr note で legacy/offline/internal opt-in であることを案内する。即時の warn-and-exit / 失敗化は行わない。
- `references/safety.md` 等の project-scope mirror が canonical (`skills/mozyo-bridge-agent/references/safety.md`) からドリフトしないよう、canonical を先に編集し、その content を mirror へ写す (`PluginMarketplaceTest` が plugin mirror のドリフトを検出するのと同じ思想)。この partial mirror の drift は `LegacyProjectSkillMirrorTest` (`tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py`, Redmine #13483) が自動検出する: mirror する `references/{project-map,release,safety,workflow}.md` が canonical と byte 一致であること、partial set が pin されていること (canonical の `redmine-issue-authoring.md` / `subagent-delegation.md` は意図的に非 mirror)、`SKILL.md` は Claude adapter stub として意図的に canonical と非一致であることを固定する。
- **写す作業は `scripts/sync_legacy_project_skill.sh` で行う (Redmine #14580)。** 本節は当初「plugin mirror と異なり専用 sync script は無く、canonical を先に編集して mirror へ写す運用を維持する」と決めていたが、この決定は実測で否定された: commit `7ca3380f`「Pin coordinator work-unit resolution」は canonical と plugin mirror を更新し、legacy mirror だけを落とした。plugin 側には script と `release check drift` gate があり、legacy 側には散文の運用規約しか無かったことが差を生んだ。#13483 の detection は正しく動いたが、検出したのは full suite 実行時であり、focused pre-commit lane は `skills/**` の変更に対して full 推奨を出しつつ実行しない設計 (`vibes/docs/logics/pre-commit-focused-verification.md`) のため、commit 前には効かなかった。
- `scripts/sync_legacy_project_skill.sh` は plugin 用の `rsync -a --delete` を**再利用しない**。この mirror は partial かつ adapter が意図的に非一致であり、full mirror sync は (a) `SKILL.md` adapter stub を canonical `SKILL.md` で上書きし、(b) 意図的な非 mirror file を持ち込むため、契約を 2 か所で壊す。pin した reference 集合だけを置換し、`SKILL.md` には触れない。
- `--check` mode は書き込まずに exit 1 を返す fail-closed gate で、`mozyo-bridge release check drift` の 3 番目の sub-check として実行される。判定規則と recovery の正本は下記 `### Mirror Contract` を読む。

### Mirror Contract (Python authority が正本)

legacy partial mirror の規則は **Python authority** が正本である (Redmine #14580 design consultation j#90400 → answer j#90402)。

- 契約語彙・pinned set・violation 分類・recovery 導出: `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/domain/legacy_mirror_contract.py` (pure、filesystem を触らない)
- observation と write: 同 Feature の `application/legacy_mirror_sync.py`
- CLI adapter: 同 `application/cli_legacy_mirror_sync.py`
- `scripts/sync_legacy_project_skill.sh` は **薄い互換 wrapper**。repo root と `src` を解決して Python module へ `exec` するだけで、pinned set・audit・copy・cleanup logic を**持たない**。operator command / docs / `release check drift` の subprocess 契約を維持するために残している。
- `f_160_release_version_governance/release_drift.py` は gate を束ねる **caller** に留まり、mirror 規則を複製しない。

6 規則を 1 つの契約として両 mode に適用する。規則を増やすときは domain module の同一 taxonomy に足す。

| 規則 | 内容 |
|---|---|
| A. source topology | canonical references path の全 component が実在する非 symlink directory |
| B. source entries | pin 済み name が **非 symlink の regular file** |
| C. dest topology | mirror path の**実在する** component が非 symlink directory。不在は sync が作る (別 class) |
| D. dest entry set | mirror references の**全 direct entry** (hidden / 拡張子不問 / 種別不問) が pinned set に属する |
| E. dest entry types | 実在する pin 済み entry が非 symlink の regular file |
| F. content parity | pin 済み entry が canonical と byte 一致 |

`--check` は A-F を typed result へ集約して報告し、何も書かない。sync は **A-E** を書き込み前に評価し、1 件でも違反があれば **write zero**。**F と「mirror 不在」は書き込みを阻まない**——content drift の修復と directory 作成こそが sync の仕事であり、これを blocker にすると command が自分の仕事を拒否する。A/B 不成立時は F を評価せず、source invalid と content drift を合成しない。recovery は**完全な audit result から導出**し、実行して収束する action だけを表示する。

実測した実害 (すべて修正前は exit 0 + 成功表示。fail-open は round ごとに別の軸から入った):

- **dangling symlink**: `-e` は symlink を辿るので false になり、glob no-match と同じ枝で skip された。
- **pin 済み symlink / hardlink**: content parity は link を辿って通過し、`cp` が link 先の inode へ書き込んで無関係 file を canonical 本文で上書きした。**hardlink は regular file なので type 判定では原理的に捕まらない**。
- **pin 済み directory / FIFO**: 前者は `safety.md/safety.md` を作り、後者は `cp` が open で無限停止した。type 判定は `lstat` の mode 分類で行い、拒否対象を open しない。
- **dest path component の symlink**: `-d` が辿るため mirror は健全に見え、外部 directory を書き換えた。component ごとに follow しない判定を行う。
- **filename domain**: audit を `*.md` に限ると `unpinned.txt` / hidden entry / residue を素通りさせた (shell glob は hidden も除外する)。
- **filename serialization**: shell の `$(...)` 経由で entry 名を列に直すと word splitting と pathname expansion を受け、`project-map.md release.md` という 1 entry が 2 語に割れて両方 pinned に一致した。literal `*` の entry は再 glob 展開され無関係 path を誤報した。**`os.scandir` の entry name を exact 比較し、直列化しない**。診断は `repr` で control character を escape し、1 filename が複数 log 行へ偽装しないようにする。
- **canonical source の alias**: `-d "$src"` / `-f "$src/$name"` も symlink を辿る。aliased source は repo が tracking しない byte を publish するので、**内容として取り込まず正本 path の復旧を案内する**。
- **非 directory ancestor**: `.claude/skills` を regular file にすると ENOTDIR で `-e` が false になり「mirror missing → rerun」と案内するが、その sync は `mkdir -p` で失敗した。「実在するが directory でない」と「不在」を**別 class**にし、rerun 案内は収束する class にだけ出す。

#### Action-time authority (R7 で load-bearing になった不変条件)

規則 A-F の**判定**に加えて、**判定した対象へ実際に I/O が届く**ことを保証する層がある。preflight で観測した path は次の syscall で別物になり得る (TOCTOU) ため、以下は contract の一部である (Redmine #14580 review j#90418 R6-F1 / j#90450 R7-F1〜F4)。

- **dir-fd bind**: path の全 component を `O_DIRECTORY|O_NOFOLLOW` で open して descriptor を保持し、**以後 multi-component path を再解決しない**。stat / read / create / rename / unlink はすべて bound descriptor 相対で行う。component を後から差し替えても I/O の着地点は変わらない。「post-audit で検出する」は防止ではない——親 component を alias にすると、検出前に外部へ書き込まれる (実測)。
- **leaf の type validation は open 前に効かせる**: leaf は `O_NOFOLLOW|O_NONBLOCK` で open し、返った fd の `fstat` で regular file 以外を拒否する。`O_NONBLOCK` が無いと **FIFO を掴んだ open 自体が writer を待って停止**する (実測: `--check` が停止し kill が必要)。「拒否対象を open しない」ではなく「**拒否対象で停止しない**」が正しい表現。
- **action-time に見つかった type failure は、rule E と同じ重み**で扱う。後から見つかったからといって rule F (非 write-blocking) に落とすと、recovery が「resync せよ」と言う一方で sync の preflight が同じ tree を拒否し、案内が収束しない。
- **staging の identity**: temp は destination dir fd に対する `O_CREAT|O_EXCL|O_NOFOLLOW`、mode は `fchmod(fd)`、swap は `os.replace(..., src_dir_fd=, dst_dir_fd=)`。**`os.replace` は NAME が指すものを移す**ため、create 時 `fstat` の `(st_dev, st_ino)` を swap 直前に再検証する。不一致なら abort し、**自分のものでない entry は unlink しない**。
- **残る窓 (residual) は 2 か所ある。効果が異なるので個別に記す。**
  - **verify → `os.replace`**: identity 検証と rename の間。verify 直後に staging name を canonical と同内容の外部 hardlink へ差し替えると sync は exit 0 となり、mirror entry がその inode を指し得る (**foreign inode の install**)。現行 A-F は hardlink を regular file + byte parity として許すため mirror 外 write も mode 変更も起きず、内容が異なる場合や symlink は post-audit が nonzero にする。
  - **cleanup の identity check → `unlink`**: `_release_staging` が identity 一致を確認してから unlink するまでの間。check 後に foreign entry へ差し替わると、**その foreign entry を削除し得る** (**foreign entry の deletion**)。
  - したがって「**自分のものでない entry は unlink しない**」は **identity check 時点の保証**であり、無条件の invariant ではない。2 つの窓は作用が逆 (install と deletion) なので「同じ形」で片付けず、それぞれの効果を明示する。
  - どちらも **mirror directory を変更できる actor は entry を直接変更できる**という脅威モデルの内側にあり、現行 contract では non-blocking residual とする。output inode ownership までを contract 化するなら **directory-level exclusion / lock** を含む設計変更が要り、再検査の追加では閉じない (Redmine #14580 j#90450 / j#90472 R10-F3)。
- **観測不能は独立 class**: observation / open / scandir / read / write / chmod / stat / replace / cleanup の `OSError` は typed violation へ変換し、専用 recovery を持たせる。traceback で抜けさせない (`release check drift` の「sub-check の disposition に従え」が空振りする)。診断の subject は repo-relative に保ち、errno / host 絶対 path を machine contract に載せない。
- **platform fail-closed**: no-follow / dir-fd primitive が無い host では 0 へ弱化せず `rule P` で拒否する。**capability manifest は実際の call surface を漏れなく列挙する**——代表的に見える別 primitive を検査しても、実際に使う primitive が欠けた host は preflight を通り抜ける (実測: `os.stat` を見て `os.lstat` を見ていなかった)。
- 単一 `os.write` は全 byte 書込を保証しないため write-all loop を使う。

**write は既存 entry へ書き込まず replace する。** destination directory 内に `tempfile.mkstemp` 相当の **exclusive fd** で temp を作り、mode `0644` を固定して `os.replace` で directory entry を差し替える。旧 inode とその別名は無傷で、中断時も半端な reference が残らない。source は open 時に no-follow + regular file を fd で再確認し、preflight 後の alias / type swap を fail-closed にする。成功表示の前に A-F を再監査し、途中の race や partial state を success として報告しない。

**temp の ownership は prefix ではなく fd で持つ。** 現在 run が保持する exact path だけを `finally` で cleanup し、他 run の temp には触れない。prefix 一致を ownership の証明に使うと、(a) prefix を共有するだけの無関係 file を自動削除し、(b) **並行 run の active temp を削除**する (両方とも実測)。kill 等で残った residue は次回の規則 D で**通常の unpinned entry として block** し reviewed disposition を要求する——自分の crash residue と「誰かが残したい file」を実行時に区別できないため、「stale だから sync が消す」とは案内しない。

**teardown の失敗経路は 3 channel を分けて扱う。** unwind 中の cleanup (staging descriptor の close / staging entry の release) は `owned_descriptors._teardown_during` の 1 経路に集約し、call site ごとに規則を決め直さない (Redmine #14580 review j#90477 R11-F1 / j#90482 R12-F2 / j#90487 R13-F1〜F3 / j#90492 R14-F1・F2)。

- **returned failure** (`close()` の `False` / release の violation tuple) は例外ではない。返り値契約を自分で設計しておきながら teardown で捨てると、**typed cleanup failure が notes 空のまま消え residue だけ残る**。
- **ordinary `Exception`** は note として記録する。実際に operation を巻き戻した例外を置き換えない。
- **control-flow `BaseException`** (`KeyboardInterrupt` / `SystemExit` / `GeneratorExit`) は primary より優先し、caller が raise する。note へ降格しない。最初の 1 件を surface させ、後続は primary へ記録する——**片方の channel だけ「記録しない」例外を作らない**。
- 上記のどれが起きても、また**記録処理自体が中断されても、残る teardown action は必ず独立に走る**。記録を rail の外で行うと、interrupt が loop ごと抜けて release が走らず residue が残った (実測)。
- **記録 (retention) と表示 (presentation) を分ける。** failure **object** を、`str()` / `add_note` より前に infallible な構造 (list への append) へ保持する。note は ledger の rendering であって ledger ではない。`add_note` を台帳にすると (a) 記録中の interrupt が**記録しようとしていた failure ごと**失わせ、`CLEANUP_FAILED` を報告した cleanup の residue が disk に残ったまま exception graph から到達不能になり、(b) secondary の `__str__` が例外を投げるだけで best-effort として黙って消える (両方とも実測)。returned failure は violation tuple のまま保持し、note 文字列へ潰さない。
- **台帳の carrier は primary の型にも caller の namespace にも依存させない。** 実測で棄却した順に: (1) `setattr` と `__context__` 代入は**どちらも型の `__setattr__` を経由**するので属性代入を拒否する型に両方同時に無効 (第二 carrier を書いてまさにその型で失敗することを確認)、(2) `object.__getattribute__(exc, "__dict__")` は **subclass が定義した `__dict__` data descriptor を dispatch する**ので、その property が例外を投げれば retention が消え、control-flow を投げれば rail ごと抜けて未実行の action が生まれる、(3) **難読な文字列 key も attribute name である** —— `setattr` / `getattr` は綴りに関係なく任意の文字列を受けるので、caller の binding を置換し得る。採用形は `BaseException` 自身の descriptor を bind して instance dictionary を取り (subclass へ dispatch し得ない)、**identity key (`object()`)** に **実装が生成した module-private 型**だけを置く。**衝突を「起きにくく」するのではなく起こり得なくする。** 代償は明記する: instance dict は attribute name で復元されるため、**台帳を持つ exception は `pickle.loads` で `TypeError` になる**。`dumps` がそこまで届くのは **保持 entry 自身が pickle 可能なときだけ**で、`__reduce__` が投げる failure object を保持していれば `dumps` から落ちる (台帳は rendering ではなく object を持つため)。台帳は元々 pickle を越えないので、caller binding の非破壊と引き換えにはしない。**「制約を書いた」と「制約を正確に書いた」は別**。
- **carrier が受け取れなかった occurrence は失わない。** retention は「control-flow を返す」だけでなく **append が完了したかを伝える**。未完了分は unwind 内に queue し、次の retention 機会と teardown 終了時に retry する。carrier interrupt 自体も occurrence として queue する。**note は残るが ledger が空、という状態を作らない**——note は rendering であって authority ではない。carrier が最後まで回復しなければ記録は到達不能になる (foreign binding を上書きしないのと同じ境界)。
- **「append が返った後」は順序であって commit acknowledgement ではない。** control-flow は bytecode 境界で到来するので、append と「queue から外す」を別 statement にすると、その隙間の interrupt が (a) queue と ledger 双方に残して **retry で重複**させ、(b) guard 外の statement なら **rail を脱出して未実行の cleanup を飛ばす**。正: **commit step を持たない**。queue からは取り除かず、**ledger への membership 自体を「取り込み済み」の記録**とし、次の pass が skip する。判定は **実装が生成した occurrence object の identity** で行い、failure object identity とは独立させる (同一 object の複数 occurrence は別物、同一 occurrence の retry は同じもの)。queue と ledger に触れる命令は**すべて guard の内側**に置く。retry は bounded にする。
- **state machine の入口も durable にする。** commit 境界を直しても、**その authority に入る手前の arrival が local 変数だけ**にあれば全体は lossless にならない。single slot に持つと ordinary exception で捨て、control-flow occurrence で**上書き**し、retry 上限で取りこぼす。正: **未 admit の arrival は list で保持**し、interrupt occurrence は追加であって置換ではない。admission は identity で idempotent にして何度でも再試行できる形にし、**ordinary failure でも retry 上限でも、抜ける前に必ず admit を試みる**。残る不可避点 (queue へ append する命令そのものへの signal) は 1 箇所に閉じ込め、実装が守れる境界だけを書く。
- **teardown 終了時の flush も retention channel の一部。** その control-flow を捨てると、note へ降格させるより悪く **完全に消える**。first-control-flow priority rail へ統合する。
- **read path と create path を分ける。** 「何が失敗したか」を問い合わせる accessor が exception を書き換えてはならない。failure が 1 件も無くても binding を置換していた。
- **既存値が carrier の場所を占めていたら、置換せず retention を諦める。** 記録は他人の data より価値が低い。
- **台帳は occurrence を数える。** object identity で dedupe すると、独立した action が返した同一 object が 1 件へ潰れる。`False` は singleton なので **returned failure channel では常に潰れていた** (note は 2 件、ledger は 1 件)。各 occurrence の retention 点を 1 箇所に固定し、防御的な二重 record を構造的に不要にする。
- **carrier の取得・保持自体の control-flow も rail 内 channel**。retention は raise せず値で返す。安全網の一部が raise できると、それが「残る action は必ず走る」を破る最後の穴になる。
- **priority と retention は別の問い**。最初の control-flow を raise することと、後続を台帳に残すことは独立に決める。表示だけが bounded (note を作る過程の interrupt は raise しない) で、object は必ず残る。
- descriptor の ownership は **close syscall の前に**手放す。close 自身が unwind し得るため、後で sentinel を立てると `finally` が同じ fd **番号**を再度閉じる。番号は即再利用されるので、これは他人の descriptor を閉じる (実測)。

test 側も同じ契約に従う。規則・violation 組合せ・recovery precedence は `test_legacy_mirror_contract.py` が **rule 単位の pure unit test** で押さえ、adversarial case は `LegacyMirrorSyncServiceTest` が Python authority に対して、CLI 契約 (default sync / `--check` / `--help` / unknown argument = exit 64 / repo-root 実行) は `LegacyMirrorWrapperCliTest` が wrapper に対して押さえる。tracked tree の exact-set / regular-file 判定は `is_file()` filter と `*.md` glob の**どちらも使わず**全 direct entry で計算する (どちらも symlink を辿る / hidden を落とすため)。**pinned set の定義は domain module の 1 箇所だけ**であり、shell 側の複製と cross-check test は撤去した。

Removal criteria (本 grace period を解除する条件):

- 次の release line で project-scope install を相談される事例が無くなったとき。
- かつ `scripts/install_claude_skill.sh` の caller / smoke で `MOZYO_BRIDGE_CLAUDE_SCOPE=project` を使う経路が無いことが確認できたとき (`grep -rn "MOZYO_BRIDGE_CLAUDE_SCOPE=project"` がない、または fresh-tester acceptance smoke でも plugin path のみ使用に切り替わったとき)。#12360 で default scope を `global` に移したため、残る `MOZYO_BRIDGE_CLAUDE_SCOPE=project` 出現はこの policy / scope 説明と明示 opt-in の案内文だけになり、active な install caller の project default は消えた。turnkey acceptance は plugin marketplace path を primary に据えつつ、release-ref pin が必要な場合だけ legacy script を `MOZYO_BRIDGE_CLAUDE_SCOPE=global` で明示利用する (project scope は使わない)。
- 上記が満たされたら、別 task として `.claude/skills/mozyo-bridge-agent/` の `git rm` + scaffold/test/doc の整合更新を実施する。本 task ではその follow-up を Open task として残す。

## Legacy Global Claude Skill Deprecation

`~/.claude/skills/mozyo-bridge-agent/` 配下に `scripts/install_claude_skill.sh` で配置する Claude personal skill (legacy global Claude skill) は、**新規 install において deprecated** とする (Asana audit `1214732699548536` / `1214733632421625`)。新規 install は Claude plugin marketplace 経由 (`claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user`) のみを推奨する。

- 既存 install の取り扱い: 既に `~/.claude/skills/mozyo-bridge-agent/` を持つ user の home directory を、本 repository から自動削除しない / 強制 cleanup しない。cleanup の判断は user に委ねる。`mozyo-bridge doctor` は legacy directory を引き続き scan するが、`claude_skill: plugin-managed` (plugin が検出された場合) を期待状態として扱う。
- `scripts/install_claude_skill.sh` の存続条件: 以下のいずれかに当てはまる環境のためにのみ残す: (a) plugin marketplace を使えないオフライン環境、(b) 内部 mirror / 内部 fork からの install、(c) fresh-tester acceptance smoke の検証。これらの条件に当てはまらない通常 install は、本 script を使わず plugin marketplace 経由で行う。
- 配置 precedence の落とし穴: 同名 skill では personal (`~/.claude/skills/`) が project (`.claude/skills/`) を override する。新規 install が plugin marketplace path のみを使えば、`mozyo-bridge-agent:mozyo-bridge-agent` の namespace 分離が効くため、この precedence gotcha を踏まない。legacy global Claude skill を残したまま plugin path と共存させると、plugin install 後も personal copy が残り続け、user が後で contents drift を気にする必要が出る (本節が deprecated として推奨しない理由)。
- 廃止 timeline: hard removal は本 task の scope 外。`scripts/install_claude_skill.sh` の即時削除 / 即時 break-only stub 化は禁止 (deprecation 通知 + 推奨経路の切替に留める)。実際の install / cleanup 動作の変更は別 Asana task で取り扱う。

## Source of Truth と drift 対策

canonical な skill 本体は `skills/mozyo-bridge-agent/` に置き、Claude plugin marketplace が配布する `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` はその mirror として扱う。

- canonical を変更したら必ず `scripts/sync_plugin_skill.sh` を実行して mirror を更新する。
- CI / pre-commit gate には `scripts/sync_plugin_skill.sh --check` を使う。`--check` は dry-run (`diff -r` による content + file-set 比較) で、何も書き込まずに drift があれば exit 1 を返し、recovery command (`scripts/sync_plugin_skill.sh`、no `--check`) を stderr で案内する。書き込みを伴わないので CI で worktree を汚さない。mtime だけを比較する rsync `--itemize-changes` は checkout 由来の timestamp-only 差 (`>f..t......` / `.d..t......`) を byte-identical でも drift と誤判定し、runner の時刻に依存する非決定 gate になったため採用しない (Redmine #13580)。`diff -r` は mtime を無視し content / 欠落 / 余剰のみを path 付きで報告する。実際の sync path (`--check` なし) は従来どおり `rsync -a --delete` で mirror を書き換える。
- 両者の drift は `tests/test_plugin_marketplace.py::PluginMarketplaceTest` が二つの経路で検証する: (a) Python の sha256 walker による file list + content hash 比較 (`test_plugin_skill_mirror_matches_canonical`)、(b) `sync_plugin_skill.sh --check` の exit code と stderr の動作 pin (`test_sync_script_check_mode_*` 群)。両者が同じ drift を独立に検出する。
- workflow body の semantic drift (canonical と mirror を同期して両方から重要 section を抜き落とすケース) は `SkillCrossWorkspaceGuidanceTest` と `SkillWorkflowSemanticAnchorsTest` が pin する。前者は Redmine #10332 cross-workspace / `--mode standard` guidance を、後者は handoff lifecycle、role boundary、Codex direct-edit gate、Repo-Local Guardrail Autonomous Lane、audit-owned commit authority、workflow change verification の代表 phrase / section heading を verbatim で要求する。byte 一致だけでは捕まらない governance regression をここで止める。
- skill / plugin mirror に対して canonical renderer (`mozyo-bridge scaffold canonical [--check]`) は採用しない。両者は **pure byte mirror** であり、conditional rendering が必要な router pair / governed preset workflow と性質が異なるため、`sync_plugin_skill.sh --check` の content + file-set (`diff -r`) gate + 上記 test 群を正本 mechanism とする (mirror を書き換える sync path は `rsync -a --delete`)。
- plugin の install 時 Claude Code は plugin directory を cache にコピーするため、plugin root の外を参照する symlink (例: `../../../skills/mozyo-bridge-agent`) は使えない。docs: <https://code.claude.com/docs/en/plugins-reference#plugin-caching-and-file-resolution>
- mirror が手動編集された場合も drift test で落ちる。`plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` を直接編集せず、canonical を編集してから sync する。

### Install Command Drift (Redmine #10699)

operator-facing install command snippet (`claude plugin marketplace add hollySizzle/mozyo_bridge` / `claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user` / `pipx install mozyo-bridge` / `mozyo-bridge rules install` / Codex `$skill-installer https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent`) は README.md / 本 file / `vibes/docs/logics/bootstrap.md` / `vibes/docs/logics/scaffold-rules.md` の複数箇所に verbatim で出現する。これは exact-string copy であり audience-specific variant ではないため、1 箇所だけ更新されると user が doc 間で異なる copy-paste recipe を得る drift 実害がある。

owner decision で README / ReleaseDocs 全体 canonical 化は対象外、また install 手順は user-facing readability を最重視するため canonical render / 共有 include は採用しない。代わりに最軽量機構として `tests/test_bootstrap_install_docs.py::InstallCommandConsistencyTest` が正本 install command 列を **per-doc 出現回数** で pin する。`PINNED_INSTALL_OCCURRENCES` 表に各 (command, doc) → expected count を列挙し、`body.count(command)` が expected 値と一致することを assert する。intentional な audience variant (`pipx install --force git+https://...` Beta Tester form) も `INTENTIONAL_VARIANT_OCCURRENCES` で同じ count gate に乗せ、PyPI 形式と git-main 形式が誤って同型化される regression を止める。

#### 単数 occurrence drift の検出

初版 (#10699 commit `2014e1a`) は `assertIn` ベースで「各 doc に少なくとも 1 回出現する」ことだけを pin していた。Codex correction review #51114 が unsound と指摘: 1 つの doc に N occurrences ある command で、その内 1 occurrence だけ drift しても残りの (N-1) occurrences が `assertIn` を満たし、test は pass してしまう (single-occurrence drift escape)。

correction (#10699 commit `<this commit>`) で per-doc 出現回数を verbatim pin する形に書き換えた:

- 1 occurrence drift → count が expected → expected - 1 にずれる → equality check が落ちる。
- `test_count_gate_catches_single_occurrence_drift` は README の `claude plugin marketplace add hollySizzle/mozyo_bridge` (count = 2) の 1 occurrence だけ mutate した body を作り、count gate が落ちる一方で旧 `assertIn` gate は通っていたことを明示的に pin する meta-test。

#### 運用

- 新規 doc に install command を追加する場合は `PINNED_INSTALL_OCCURRENCES` の doc map に新 entry を追加し、初期 count を pin する。
- 既存 doc の install command を増減する (例: 別 section に説明を追加して `mozyo-bridge rules install` 回数が 5 → 6 になる) 場合は同 test の expected count を同 commit で更新する。
- 命令文字列を更新する (例: marketplace name 変更、scope flag 変更) 場合は同 test の command 文字列と全 doc を同 commit で更新する。
- 共有 include / canonical render / 新規 logic doc は **意図的に追加しない**。drift 検出は unit test 層に集約する (`SkillCrossWorkspaceGuidanceTest` / `SkillWorkflowSemanticAnchorsTest` precedent と同じ)。

## Marketplace / plugin metadata

- `.claude-plugin/marketplace.json` は `name`, `owner`, `plugins` を持ち、`mozyo-bridge-agent` plugin を `./plugins/mozyo-bridge-agent` の explicit path で参照する (marketplace root から resolve)。
- **Caveat (verified 2026-05-12, commit 542edad)**: `metadata.pluginRoot` は使わない。Claude plugin docs L181 (<https://code.claude.com/docs/en/plugin-marketplaces>) は `pluginRoot` が relative source path に prepend されると記載するが、現行 Claude CLI の (a) validator schema は source に `./` prefix を強制 (L233 "Must start with `./`") し、(b) installer は `./`-prefixed source を marketplace root から resolve する (`pluginRoot` を prepend しない)。結果として `pluginRoot: "./plugins"` + `source: "./mozyo-bridge-agent"` は `claude plugin validate .` を通っても GitHub marketplace 経由の install 時に `Source path does not exist` で失敗する。verification log: Asana 1214730609356621 comment 1214731507813769。
- `plugins/mozyo-bridge-agent/.claude-plugin/plugin.json` は `name`, `description`, `repository`, `license`, `keywords`, `author` を持つ。`version` は意図的に省略し、git commit SHA を version として使う (docs: <https://code.claude.com/docs/en/plugin-marketplaces#version-resolution-and-release-channels>)。これにより `pyproject.toml` の package version と plugin version を別管理できる。
- marketplace name (`mozyo-bridge`) は Anthropic 公式 reserved name (`anthropic-marketplace`, `claude-code-marketplace` など) と衝突しない kebab-case。

## 理由

- Claude Code は project skill (`<project>/.claude/skills/<name>/SKILL.md`) と user/personal skill (`~/.claude/skills/<name>/SKILL.md`) の両方を officially 読み込む。precedence は Enterprise → Personal (`~/.claude/skills/`) → Project (`.claude/skills/`) → Plugin で、矢印の順で earlier が override する。同名 skill が複数 scope に存在する場合、公式 docs は次の通り定めている (verbatim):

  > "When skills share the same name across levels, enterprise overrides personal, and personal overrides project. Plugin skills use a `plugin-name:skill-name` namespace, so they cannot conflict with other levels."

  source: <https://code.claude.com/docs/en/skills> (`Where skills live` セクション)。
- つまり同名 skill では **personal (`~/.claude/skills/`) が project (`.claude/skills/`) より優先される**。多くの開発ツールは project が user を上書きする慣習だが、Claude Code の skill 解決はその逆である点に注意する。
- Plugin skills は `plugin-name:skill-name` で namespace 分離されるため、他 scope と衝突しない。
- Codex skill は `${CODEX_HOME:-$HOME/.codex}/skills/<name>/` に user-global で配置し、`SKILL.md` を中心に必要に応じて `agents/openai.yaml`, `references/`, `scripts/`, `assets/` を持つ。
- `name` / `description` frontmatter と supporting files の考え方は近いが、配置と tool-specific metadata は同一ではない。

## Claude install scope

`scripts/install_claude_skill.sh` は `MOZYO_BRIDGE_CLAUDE_SCOPE` で配布範囲を切り替える。値は `global` (default) または `project` (legacy opt-in)。両方に配布したい場合は二度実行する。#12360 で default を `project` から `global` に移した: bare invocation は legacy personal skill (global) を書き、project mirror は明示的に `MOZYO_BRIDGE_CLAUDE_SCOPE=project` を渡した場合だけ書く。

- `global` (default): shared body を `${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}/skills/mozyo-bridge-agent/` に同期する。Claude Code の personal skill として全 session で有効。Codex の `${CODEX_HOME:-$HOME/.codex}/skills/` と対称な配置で、personal install は同名 project skill を override する。global scope では adapter は生成しない (Claude Code は user skill 直下の `SKILL.md` を直接読むため)。なお新規 install の primary は plugin marketplace path であり、この legacy script は offline / internal mirror / fresh-tester acceptance smoke 用の fallback である。
- `project` (legacy opt-in): 対象 project の `.claude/skills/mozyo-bridge-agent/` (adapter) と `skills/mozyo-bridge-agent/` (shared body) に同期する。Claude Code を当該 project root から起動した時だけ Claude が認識する。**grace-period deprecation 中で default ではない**: 明示的に `MOZYO_BRIDGE_CLAUDE_SCOPE=project` を渡した時だけ走り、script は project 分岐で非致命的な deprecation note を stderr に出す。**注意**: Claude Code の precedence rule により、同じ name (`mozyo-bridge-agent`) を持つ personal skill が `~/.claude/skills/` にもあると、project copy は shadow されて Claude は personal copy を読む。project scope を明示 opt-in したい正当な場面は (a) personal/global install を持たない利用者、(b) repo に skill を commit して contributor 全員に配布したい場合、(c) 実行時に personal を一時的に外して project copy を使いたい場合に限られる。新規 install では plugin marketplace path を推奨する。

`scope=both` は提供しない。Claude Code の precedence rule で personal が project を上書きするため、両方同名で install すると project copy が常に shadow され混乱を生む。両方の destination を同時に持ちたい場合は、明確な意図のもとで `scope=project` と `scope=global` を順に実行する。

## 運用

- 共通 workflow は `skills/mozyo-bridge-agent/SKILL.md` に置く。
- 詳細は `skills/mozyo-bridge-agent/references/` に分離する。
- Claude 専用設定は `.claude/skills/mozyo-bridge-agent/SKILL.md` にだけ置く。
- Codex UI metadata は `skills/mozyo-bridge-agent/agents/openai.yaml` に置く。
- Codex install は `scripts/install_codex_skill.sh` で public GitHub repository `hollySizzle/mozyo_bridge` の `skills/mozyo-bridge-agent` から `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/` へ同期する。
- Claude install は `scripts/install_claude_skill.sh` で同じ public repository から同期する。`MOZYO_BRIDGE_CLAUDE_SCOPE=project|global` で scope を選ぶ。
- install source は必要に応じて以下で上書きできる:
  - `MOZYO_BRIDGE_SKILL_REPO` (`owner/repo`)
  - `MOZYO_BRIDGE_SKILL_REF` (branch / tag / commit)
  - `MOZYO_BRIDGE_SKILL_PATH` (Codex skill source path)
  - `MOZYO_BRIDGE_SHARED_SKILL_PATH` / `MOZYO_BRIDGE_CLAUDE_ADAPTER_PATH` (Claude script だけ)
  - `MOZYO_BRIDGE_SKILL_ARCHIVE_URL` (どちらの script でも、`https://codeload.github.com/...` 以外の tarball URL を直接指定できる。`file:///...` を使えば未 push の local checkout から smoke / 手動配布できる)
- Claude install の対象 project は `MOZYO_BRIDGE_CLAUDE_PROJECT_DIR`、Claude home は `MOZYO_BRIDGE_CLAUDE_HOME` で上書きできる。
- root `AGENTS.md` / `CLAUDE.md` は skill と docs への router のままにする。

## Install Commands

### Claude Code plugin marketplace (primary)

```bash
claude plugin marketplace add hollySizzle/mozyo_bridge
claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user
```

Plugin skills are namespaced `mozyo-bridge-agent:mozyo-bridge-agent`, so they do not conflict with personal (`~/.claude/skills/`) or project (`.claude/skills/`) skills with the same name. This is the recommended path because (a) it avoids the personal-overrides-project precedence gotcha, (b) it pins to a marketplace catalog the team controls, and (c) `/plugin marketplace update` refreshes content without per-user shell scripts.

### Codex `$skill-installer` (primary)

Canonical path: <https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent>

Codex `$skill-installer` reads `SKILL.md` and copies the surrounding directory tree into `${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/`. Use the canonical path above so the install gets `references/`, `agents/openai.yaml`, etc.

### CLI / rules

```bash
pipx install mozyo-bridge
mozyo-bridge rules install
mozyo-bridge rules status
```

### Fallback: curl-based scripts

```bash
# Codex skill (fallback, user-global)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_codex_skill.sh | sh

# Claude Code skill (fallback, user-global)
curl -fsSL https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/main/scripts/install_claude_skill.sh \
  -o /tmp/install_mozyo_bridge_claude_skill.sh
MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh
```

env を pipe の右側 (`sh` の直前) に置くのは、`VAR=... curl ... | sh` の形だと env が `curl` にしか渡らず script は env 未設定で走る (= script の default scope を使う) ためである。default は `global` なので global install ではたまたま正しい結果になるが、`project` など非 default scope を明示したい場合はこの誤形では無視されるので、scope を確実に選ぶには env-before-`sh` 形を使う。

Claude Code は user/personal skill だけで運用する場合、project root から起動する必要はない。project skill を併用する場合は対象 project root から起動する。**personal/user skill (`~/.claude/skills/`) は同名 project skill を override する**ため、project 固有の skill body を使いたい場合は (a) personal install を行わない、(b) project skill の name を変えて衝突を避ける、または (c) plugin marketplace 経由で `mozyo-bridge-agent:mozyo-bridge-agent` namespace を使う。

## Local checkout install

未 push の commit や fork ブランチから配布したい場合は、ローカルで tarball を作成し、`MOZYO_BRIDGE_SKILL_ARCHIVE_URL` に `file://` URL を渡す。`tar --transform` は GNU tar 専用で macOS の bsdtar では動作しないため、staging directory を使う portable な手順を使う。

```bash
src=/path/to/mozyo_bridge
out=/tmp/mozyo_bridge_local.tar.gz
stage=$(mktemp -d)
mkdir -p "$stage/mozyo_bridge-local"
cp -R "$src/skills" "$stage/mozyo_bridge-local/"
mkdir -p "$stage/mozyo_bridge-local/.claude"
cp -R "$src/.claude/skills" "$stage/mozyo_bridge-local/.claude/"
tar -czf "$out" -C "$stage" mozyo_bridge-local
rm -rf "$stage"
MOZYO_BRIDGE_SKILL_ARCHIVE_URL="file://$out" \
  sh "$src/scripts/install_codex_skill.sh"
```

この経路は smoke / dogfood 用であり、通常の標準 install path は GitHub `main` のままとする。

## README との役割分担

- README は public user 向けの install / command / safety summary に留める。
- skill 配布の配置理由、override env、scope、precedence、禁止事項はこの文書を正本にする。
- skill 本体の runtime reference は `skills/mozyo-bridge-agent/references/` に置き、README や root router へ詳細規約を重複させない。
- `skills/mozyo-bridge-agent/references/` は Codex install と Claude install のどちらにも同期される配布対象である。agent 実行時に従うべき運用境界は、まずこの runtime reference に置く。

## Beta Tester Verification

beta tester が GitHub `main` から CLI を install した後、skill 配布が期待通り動いていることを確認する流れ。詳細 README handoff は `README.md` の `Beta Tester Install (GitHub main)` 節を見る。本節はそこに重複させずに、検証観点と落とし穴だけを残す。

### Primary path verification (plugin marketplace + Codex $skill-installer)

1. Claude plugin marketplace install: `claude plugin marketplace list` に `mozyo-bridge` が出て、`claude plugin list` に `mozyo-bridge-agent@mozyo-bridge` が出ていることを確認する。plugin skill は Claude Code が `~/.claude/plugins/cache/` 配下に展開し、`mozyo-bridge-agent:mozyo-bridge-agent` namespace で読み込まれる。`mozyo-bridge doctor` の `claude_skill` section は legacy directory (`~/.claude/skills/` / `<project>/.claude/skills/`) に加えて plugin cache (`~/.claude/plugins/cache/mozyo-bridge/mozyo-bridge-agent/<sha>/skills/mozyo-bridge-agent/SKILL.md`) も検出する。primary path だけで install した場合は `claude_skill: plugin-managed` が出る。これは期待状態で、`next_action` は空 (legacy install hint は plugin が検出されている間は抑制される)、overall doctor 結果も `result["ok"] == True` を維持する。primary path の最終確認は `claude plugin list` で行う。
2. Codex user-global skill: `$skill-installer` を canonical path `https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent` で実行した後、`${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/SKILL.md` が存在し、`SKILL.md` の `name` / `description` が GitHub `main` の内容と一致する。`mozyo-bridge doctor` の `codex_skill: ok` でも同時に確認できる。
3. agent を再起動した後、Claude の skill 一覧に `mozyo-bridge-agent:mozyo-bridge-agent` (plugin) が、Codex の skill 一覧に `mozyo-bridge-agent` が出る。同 session 内では skill index がキャッシュされるため再起動を省略しない。

### Fallback path verification (curl/script + doctor)

curl/script による install を併用または primary 不可で fallback した場合の検証。

1. Codex: `scripts/install_codex_skill.sh` 実行後、`${CODEX_HOME:-$HOME/.codex}/skills/mozyo-bridge-agent/SKILL.md` が存在する。
2. Claude: `curl ... -o /tmp/install_mozyo_bridge_claude_skill.sh` → `MOZYO_BRIDGE_CLAUDE_SCOPE=global sh /tmp/install_mozyo_bridge_claude_skill.sh` (env var は pipe の右側で `sh` の直前に置く) で `${MOZYO_BRIDGE_CLAUDE_HOME:-$HOME/.claude}/skills/mozyo-bridge-agent/SKILL.md` が legacy directory に作られる。`global` scope では `.claude/skills/` 配下に adapter を生成しない。env を pipe の右側 (`sh` の直前) に置くのは、`VAR=... curl ... | sh` の形だと env が `curl` にしか渡らず script が env 未設定 (= default scope) で走るためで、default が `global` の今は global install ではたまたま一致するが、非 default scope を選ぶには env-before-`sh` 形が必要になる。
3. fallback path だけ使った場合、`mozyo-bridge doctor` の `codex_skill: ok` / `claude_skill: ok` で 1 と 2 を 1 command で確認できる。primary plugin install と fallback を併用すると plugin skill (`mozyo-bridge-agent:mozyo-bridge-agent`) と legacy skill (`mozyo-bridge-agent`) が両方有効になり、Claude Code 内で 2 つの skill 名として出る (plugin namespace で衝突しない)。

### PyPI release との見分け

- `mozyo-bridge --version` の出力は `pyproject.toml` の package version 文字列であり、GitHub `main` で未 bump の状態だと PyPI release と同じ string が表示されうる。skill 配布側で beta 適用を確認するには、(a) install 直後の plugin skill (`~/.claude/plugins/cache/...`) または legacy skill 内容と GitHub `main` の最新差分を突き合わせる、(b) 同 commit に紐づく未 release 変更 (例: 新 reference file 追加) の存在を確認する。

### Precedence の落とし穴

- Claude Code は同名 skill で personal (`~/.claude/skills/`) が project (`.claude/skills/`) を override する。fallback path で `MOZYO_BRIDGE_CLAUDE_SCOPE=global` を使い、かつ同 repo に project skill も置く構成は shadow を生む。plugin marketplace 経由 (primary path) で install した skill は `mozyo-bridge-agent:mozyo-bridge-agent` namespace に分離されるため personal / project と衝突せず、precedence の落とし穴を回避できる。
- Codex には Claude のような multi-scope precedence rule は documented されていない。Codex skill は user-global 配置のみを標準経路とする。

`scope=both` を提供しない理由、override env、archive URL の意味は本文書の `Claude install scope` / `運用` セクションを正本にする。

## 禁止事項

- skill ディレクトリに README や install guide を増やさない。
- Claude 専用 frontmatter を共通 `SKILL.md` に混ぜない。
- Codex 専用 metadata を Claude adapter に混ぜない。
- Claude Code が officially サポートしていない skill path を「標準」として docs に書かない。
- Claude Code の precedence rule (personal overrides project) を逆方向で記述しない。同名 skill では personal が project に勝つ。
- secret や local `.env` を skill 配布対象に含めない。
- Codex の標準 install path を local checkout 依存にしない。
- `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/` を直接編集しない。canonical (`skills/mozyo-bridge-agent/`) を編集してから `scripts/sync_plugin_skill.sh` で mirror を再生成する。drift test (`PluginMarketplaceTest`) が手動編集を検出する。
- plugin root の外を参照する symlink (例: `../../../skills/...`) を plugin tree 内に作らない。plugin install 時に cache へコピーされ、symlink の参照先は失われる。
- skill 配布 directory 名は `mozyo-bridge-agent` 固定。`mozyo-bridge-agent.bak`、`mozyo-bridge-agent.tmp`、`mozyo-bridge-agent.bak-plugin-only-test` 等の改名 copy / backup copy / 重複名 directory を同階層 (`skills/`、`plugins/mozyo-bridge-agent/skills/`、`.claude/skills/`、`~/.claude/skills/`、`${CODEX_HOME:-$HOME/.codex}/skills/`、`~/.claude/plugins/cache/...` のいずれにおいても) に置かない。Claude Code の skill discovery は directory 名を skill 名として登録する (frontmatter `name` を見ない) ため、改名 copy が並ぶと別 skill として available-skills list に並び (例: `mozyo-bridge-agent.bak-plugin-only-test` が新規 skill として現れる)、operator 操作 / agent invocation 双方で混乱を生む。Asana 1214732699548536 comment 1214732662430548 の Side finding として観測済み。一時的な検証で別名 copy を作る場合は別 directory tree (例: `/tmp/mozyo_bridge_skill_test/`) に出して skill discovery scan の対象外に置く。
