# Unit / Target / Projection model

Redmine #11905 / #11906。`mozyo cockpit`、通常 local `mozyo` session、
cross-project / multi-worktree 運用を、tmux / iTerm の表示形状ではなく
同じ unit target model で扱うための設計正本。

## 結論

```text
Canonical model: TargetRecord / UnitRecord
Recommended projection: cockpit_pane
Supported compatibility projection: normal_window
```

`normal local session` は即退役しない。compatibility maintenance mode として
維持する。ただし新しい multi-lane / cross-project / coordinator /
projection-state 機能は `cockpit_pane` を primary projection として進める。

「どちらでも同格」にはしない。handoff / discovery / docs の判断語彙は
`TargetRecord` / `UnitRecord` へ寄せる。

## Design-first gate

`TargetRecord` / `UnitRecord` は cockpit / normal local / cross-project /
DB state 境界にまたがるため、実装を先に走らせると resolver、handoff、docs、
state store が別々の語彙で育つ。

したがって #11905 配下では、次の順序を守る:

1. Redmine に意思決定と経緯を残す。
2. repo-local logic doc に現在の設計正本を固定する。
3. catalog に登録し、`docs resolve` / `audit-impact` の導線へ乗せる。
4. その後に #11907 以降の実装へ進む。

Redmine journal は意思決定の履歴であり、repo-local logic doc は実装者と監査者が
読む現在の正本である。片方だけでは足りない。

## 用語

### Unit

作業単位。人間、coordinator、Redmine gate が扱う logical grouping である。

```text
Unit = workspace + lane + project/governance context + role set
```

例:

- mozyo_bridge main lane
- mozyo_bridge issue lane
- 別 project の cockpit column

Unit は handoff の直接配送先ではない。handoff する場合は Unit から role を選び、
最終的に Target へ解決する。

### Project Group

複数 Unit を operator が見やすいまとまりとして扱う **presentation grouping**。
Project Group は Unit の identity component ではなく、routing / approval / close /
workflow truth の正本でもない。

```text
Project Group = display grouping of Units
Unit          = workspace + lane + project/governance context + role set
Target        = live delivery endpoint
```

例:

- 同じ Redmine project の main lane と issue sublane を一つの group に並べる。
- 別 project の Unit を同じ cockpit view に置き、gateway handoff の入口を見つけやすくする。
- local / remote host の groups を shared operator view で近くに表示する。

Project Group が持ってよいもの:

- group id / display label / sort order。
- Unit membership と position / width preference / pinned / hidden などの view preference。
- stale / unknown / unmanaged を隠さないための presentation status。

Project Group が持ってはいけないもの:

- handoff target identity。
- owner approval / review / close / completion state。
- Redmine project ownership の二重正本。
- private project policy、private path、operator 固有 layout の OSS default。

group membership は desired presentation state であり、live tmux session/window/split
tree から逆算して durable truth にしない。live geometry と group membership が
矛盾した場合、display は drift を出してよいが、handoff safety は Target の live
preflight で判断する。

### Target

実際に送れる配送先。live tmux 上の pane を中心に、host、runtime、
role、workspace/lane identity を束ねる。

```text
Target = host + tmux runtime + pane_id + role + workspace/lane identity
```

handoff は最終的に Target に対して行う。pane_id だけを信じず、process /
cwd / repo / role / workspace / lane の preflight を通す。

### Projection

Unit / Target の見せ方。routing / governance の正本ではない。

代表例:

- `normal_window`: workspace-scoped session の `claude` / `codex` window
- `cockpit_pane`: cockpit group session の `cockpit` window 配下 pane
- future `webviewer_unit`: event / inventory projection による表示

## session / window / pane の扱い

```text
session = runtime group / view attribute
window  = view / compatibility attribute
pane    = runtime target identity
```

### session

通常 local `mozyo` では workspace の canonical session が使われる。cockpit では
named cockpit session が group として使われる。remote SSH host の tmux session は
local host の tmux session と物理的に混ぜない。

`canonical_session` は workspace identity に近い安定名として registry に持ってよい。
一方で、今その tmux session が存在するかは live tmux runtime の正本である。

### window

window name は primary role identity ではない。

- normal local では `claude` / `codex` window が role fallback になる。
- cockpit では window name が `cockpit` になるため、window name だけでは role を
  判定できない。

window name fallback を使った場合は `role_source=window_name` として明示する。

### pane

pane は handoff の最終配送先である。ただし pane_id は runtime identity なので、
DB / docs / Redmine の durable identity ではない。

preflight では少なくとも次を確認する:

- pane が存在する
- foreground process が receiver allowlist に入る
- cwd / repo が期待と合う
- role が期待と合う
- workspace_id / lane_id が selector と合う
- ambiguous ではない

## Resolver priority

1. explicit pane target が指定されている場合:
   - pane existence / process / cwd / repo preflight を確認する
   - pane option role / workspace / lane があれば primary とする
2. tmux pane option:
   - `@mozyo_agent_role`
   - `@mozyo_workspace_id`
   - `@mozyo_lane_id`
3. workspace registry / anchor / repo facts:
   - workspace identity
   - canonical session
   - git branch / common dir / checkout facts
4. window name fallback:
   - normal local compatibility のためだけに使う
   - `role_source=window_name` と明示する
5. same-lane narrowing (Redmine #12011):
   - explicit `--target` 不在で role resolution が同一 session 内に複数 agent pane
     を返したとき、fail closed する前に sender pane 自身の
     `(workspace_id, lane_id)` で候補を絞る。一意に決まればその same-lane pane を
     auto-resolve する (例: 複数 lane を載せた cockpit で `--to codex` を sender
     lane の Codex gateway に解決する)。
   - これは **same-lane addressing 限定** であり、候補集合を縮小するだけで sender 自身の
     lane 外の pane を選ばない。lane 境界を越える handoff は引き続き target lane の
     Codex gateway に明示 addressing する (`coordinator-sublane-development-flow.md`
     `## Cross-Lane Routing Rule`)。
   - sender pane が不明、または concrete な lane identity を持たない (`workspace_id`
     空かつ lane が `default`) 場合は narrowing を行わず、6 の fail closed に落とす。
     identity source は live tmux の pane option であり、pane title を正本にしない。
   - same-session local Claude auto-select (Redmine #12070): `--to claude` が同一
     session 内に複数 Claude pane を返したときは、lane narrowing に加えて
     **(a) sender が non-empty `workspace_id` を持つこと** と
     **(b) repo identity gate** を要求する。repo identity gate は、sender と候補の
     cwd がともに repo root を infer できればその root が一致すること、どちらも infer
     できなければ同一 registered `workspace_id` で identity を担保すること、を満たした
     場合だけ通過する。片側だけ root を持つ / root が異なる / sender が `workspace_id`
     を持たない場合は fail closed する。これにより、同一 `(workspace_id, lane_id)` だが
     別 repo checkout に居る複数 Claude pane でも sender 自身の local Claude を一意選択
     できる。pane 選択のみを解決し、nested project の実行 root 伝搬 (Redmine #12098)
     は `## Execution root propagation` が扱う別レイヤである。cross-session Claude
     direct / cross-lane Claude は緩めない。
6. ambiguous / missing:
   - fail closed または explicit target を要求する。fail closed 時は具体的な候補
     (pane_id / workspace / lane)、絞り込めなかった理由、推奨 retry
     (`--target %pane`) を提示する。

### Coordinator target resolution (Redmine #12015)

same-lane narrowing (5) は sender 自身の lane に解決するため、sublane から
**main coordinator Codex** への cross-lane callback には使えない (sender lane の
Codex = sublane 自身に解決してしまう)。そこで pseudo-target `coordinator` を
別経路として用意する。

- `coordinator` は **sender と同じ `workspace_id`** で **`lane_id == default`
  (primary checkout = coordinator lane; `cockpit_layout.resolve_lane_identity`)**
  の Codex pane に解決する。これは sanctioned な sublane -> coordinator callback
  路 (Codex-to-Codex) であり、`%953` 等を手で拾わずに済む導線を与える。
- **workspace-scoped 限定**: 別 workspace の coordinator には決して到達しない
  (cross-workspace consult は別 primitive #11779)。sender が同一 workspace の
  default-lane Codex を一意に持つ場合のみ解決する。
- **fail-closed**: sender 不明 / workspace identity 不在 / default-lane Codex が
  0 または複数 のときは解決せず、理由・候補・推奨 retry (`--target %pane`) を
  提示する。silent な選択はしない。
- identity source は live tmux の `@mozyo_*` pane option であり、pane title /
  iTerm UI を正本にしない。`coordinator` は explicit `%pane` override を置換せず、
  通常運用の導線として追加するだけ (override は常に残る)。

## Execution root propagation (Redmine #12098)

pane 選択 (Resolver priority) は「どの pane に送るか」を解決するが、「receiver が
どの directory を作業 root にするか」までは決めない。両者は別レイヤである。

cockpit workspace では pane cwd が workspace anchor root (例: 一段上の workspace
root) になり、実際の作業対象がその配下の nested project (例:
`.../rovoice/shinsei_llm`) のことがある。durable anchor が相対保存 path しか持た
ないと、receiver は nested execution root を一意に復元できず、別 checkout を誤探索
する。`%pane` scrollback を手で grep して訂正する運用は durable handoff の再現性を
壊すため、標準導線にしない。

そこで handoff は **target execution root / workdir** を明示 carrier として運べる。

```text
pane cwd / repo root   != target execution root (nested project)
解決: pane 選択 (上記)   別レイヤ: execution root 伝搬 (本節)
```

- `mozyo-bridge handoff send --workdir <path>` で receiver の作業 root を明示する。
  pane 選択や cross-session / cross-lane gate は変えない。record / wording 層のみ。
- carrier は `repo-root-relative pointer` を第一に持つ。repo anchor は
  `--target-repo` (指定時; `auto` は解決後の root) を優先し、無ければ target pane の
  inferred repo root を使う。workdir が anchor 配下にあるとき relative pointer
  (例: `rovoice/shinsei_llm`) を計算する。relative pointer は personal home prefix
  を持たない portable 表現であり、pane notification と durable delivery record の
  唯一の表記に使う (`public-private-boundary.md`)。
- **relative `--workdir` の解決基準は asserted `--target-repo` である**
  (Redmine #14249)。`--target-repo` は `target_repo_mismatch` gate 付きで
  「receiver の repo root はここだ」と主張する assertion なので、relative path は
  sender の process cwd ではなく **その target repo root** を基準に解決する。
  したがって portable な `--workdir .` は target repo root を意味し、lane の
  absolute path を渡した場合と A/B で同一の execution root になる。
  `--target-repo` 未指定時は権威ある receiver frame が無いため、relative path は
  従来どおり sender cwd 基準で解決する (既存契約は不変)。
- **`--target-repo auto` は target 自身の frame へ解決し、sender cwd へ fallback
  しない** (Redmine #14249 R2、reproduction j#94419)。上記の「`auto` は解決後の
  root」は誰の root かを述べていなかった。tmux では target `%pane` の cwd から
  推論する。herdr には読むべき pane cwd が無く、#13331 j#73312 #2 はそこを
  **sender の repo root** で埋めていたが、それが正しいのは target が sender 自身の
  lane であるときに限られる。cross-lane send では receiver が居ない repo を
  assert し、しかも上記の relative-workdir 基準がそれを検証済みとして運んでいた。
  herdr の `auto` は次の frame で解決する。
  - target unit == sender unit: この checkout が target の root である (#13331 の
    ケース。resolved route identity から**検証**して選ぶ。仮定しない)。
  - 同一 workspace の別 lane: その lane の canonical worktree。join key は
    lifecycle authority の `worktree_identity` token であり、workspace repo 自身の
    `git worktree list --porcelain` entry のうち exactly one がその token を
    re-derive しなければならない (`bind_lane_worktree` — hibernate topology
    observation と共有する単一 join。send 経路なので local only、`ls-remote` は
    しない)。lane id を branch と見なす推論はしない (review j#86739 R3-F2)。
    display metadata の `lane_metadata.worktree_path` を authority に昇格させない。
    **binding は path identity のみを与え、branch authority を含まない**:
    `worktree_identity` は canonical path の hash なので、**detached HEAD の lane
    worktree もそのまま解決する** (review j#94499 finding 2)。branch を要求するのは
    push / HEAD topology を観測する `observe_lane_topology` 側の責務であり、
    execution root の解決には不要。detached を拒否しても正しい root を持つ稼働中
    lane を止めるだけで安全上の利得が無い。
  - それ以外 (identity 未 attested / foreign workspace / lifecycle row 不在 /
    `worktree_identity` 空 / join 非一意 / store 読取不能): **typed fail-closed**。
    `blocked` / **`auto_target_repo_unresolved`** で zero-send し、explicit
    `--target-repo <target lane worktree>` を要求する。sender cwd への silent
    fallback は禁止 — それが本 defect であり、誤った execution root を検証済みの
    ものとして送達する。
    ただし **store が runtime より新しい場合だけは既存の `reader_upgrade_required`
    へ写す** (review j#95843 point 2)。同 reason が既に「現行 runtime 経由で送れ、
    store を downgrade するな」という唯一正しい repair を持つため、同じ条件に
    2 つ目の token を作らない。
  - **refusal は durable な subreason を伴う**。どの段で解決に失敗したかは
    `DeliveryOutcome.auto_target_repo` (`subreason` / `basis` / `detail`) に載せる
    (review j#95843 finding 1)。narrative は全 subreason に対して真である表現に
    留め、具体的な段は同 field を読ませる。**受信側が持っていない情報を
    next_action で参照しない** — R3 は存在しない「structured detail」を参照していた。
  - **検証した「文字」ではなく、検証済みの「値」を使う** (review j#96049 finding 1)。
    `str` subclass は `__format__` / `__hash__` / `__len__` / `__eq__` 等で
    **検証の後に**再介入できる。R8 は regex と長さを caller の object に対して確認し、
    その object をそのまま table lookup と f-string へ渡したため、
    `__hash__` が例外を投げ、`__format__` が newline と absolute path を再注入した。
    **exact builtin `str` へ正規化してから使う**。本 repo は既に同じ問題を裁定しており
    (`_owned_str(value) = str.__str__(value)`、review **j#86068 / j#86081**)、
    `str(value)` / `value[:]` / `"" + value` がいずれも subclass hook に届くことも
    そこに記録されている。**同種の問題は、まず既存 ruling を探す。**
    payload 型は **exact `dict`** に限定し、adversarial な `Mapping.get` / equality を
    generic fallback へ閉じる (「never raises」を宣言ではなく証明可能にするため)。
  - **未知 token を durable 文面へ出してよい条件** (review j#96042 finding 1)。
    producer が closed vocabulary を出すことは、**producer 保証が及ばない経路**
    (未知 token の表示) の安全性を意味しない。表示する側の境界で値自身を検証する:
    **`str` 型であること / `[a-z][a-z0-9_]*` に一致すること / 明示的最大長以内であること**。
    いずれかを満たさなければ **raw 値を出さず stable placeholder** にする。
    payload が Mapping でない・値が unhashable でも**例外を出さず** generic へ閉じる。
    これにより ticket へ出る token は **single-line・bounded・path-free** であることが
    構造的に保証される (R7 は raw 補間で newline / backtick / absolute path / 10,000 文字を
    そのまま durable 文面へ通していた)。
  - **fallback は最も保守的な文面にする**。subreason が欠損 / 未知のときに通る経路は
    「最も情報が無い」経路であり、**原因を推測してはならない**。全 refusal に真な
    generic repair のみを返し、未知 token は「未知である」と提示する
    (review j#96019 finding 1)。R6 は既知 token 経路だけを直し、fallback を
    直前に誤りと確定した最も具体的な文面のまま残していた。
    **reason と payload の coupling も helper 内で検証する** — 別 reason の payload が
    紛れても、その reason 自身の repair を返す。
  - **surface ごとに検査すべき属性は 2 つある: 可読性 (subreason が読めるか) と
    助言の正しさ (その subreason に対する repair か)**。R5 は前者だけを全 surface で
    測り、後者は stderr でしか確認しなかったため、`next_action` (wire JSON と
    pasteable record の両方に載る) が 6 subreason で同一の誤 repair を出し続けた
    (review j#95995 finding 1)。**「全 surface を見た」ではなく
    「surface × 属性」の行列で測る**。
  - **subreason は reader surface 全てに届かせる** (review j#95911 finding 1/4)。
    wire JSON だけでは足りない。`--persist-delivery` が保存し人が ticket に貼るのは
    **pasteable markdown** であり、sender が最初に読むのは **stderr** である。
    markdown には closed vocabulary の token (`subreason` / `basis`) を固定形式で
    render し、stderr は **subreason ごとの正しい repair** を出す
    (`auto_target_repo_die_message`)。全 refusal に同一の repair を出してはならない —
    `identity_unattested` / `foreign_workspace` は binding 段に到達せず、
    `lifecycle_store_upgrade_required` は binding repair では解消しない。
  - **refusal payload に filesystem path を入れない** (review j#95911 finding 2)。
    `detail` は wire outcome・pasteable record・stderr の 3 経路へ同時に出るため、
    raw exception を補間すると個人 home path が 3 箇所へ publish される
    (R4 が実際に起こした)。exception は **type 名のみ**を使い、path・秘密値は載せない。
    正本: organization baseline (個人情報を chat / ticket / Git / log へ記録しない)。
  - **これらの refusal は proven pre-injection zero-send として closed consumer に
    登録する** (review j#95843 finding 2)。`callback_delivery._NOT_SENT_BLOCKED_REASONS`
    と `sublane_worker_dispatch.SEND_KNOWN_NOT_SENT_REASONS` の双方。未登録だと
    `uncertain` へ劣化し、安全な bounded retry が行われない。
    **この失敗に `target_repo_mismatch` を使ってはならない** (review j#94499
    finding 1)。同 reason の narrative / next_action は「観測された target pane の
    repo root が asserted 値と不一致」「flag を外して repo gate を skip」を述べるが、
    herdr auto は pane cwd を観測しておらず (比較対象が存在しない)、かつ flag を
    外すと relative workdir が sender cwd 基準に戻り、本節が禁じた状態そのものに
    なる。**reason を再利用することは、その narrative / next_action を継承すること
    である** — token の抽象的な近さではなく、sender に渡る repair が正しいかで選ぶ。
  herdr の synthesized target record の `cwd` も、この解決済み root で re-state
  する。そうしないと下流の `target_repo_mismatch` gate が「検証済み target root」対
  「sender root」を比較し、正しくなった send を構造的に落とす。
- **asserted `--target-repo` 配下に無い execution root は pre-send で
  zero-send 拒否する** (Redmine #14249)。repo_root と workdir が互いに矛盾した
  delivery — receiver は gate を通された repo とは別の root を指される — を
  `sent` として記録しない。`blocked` / `execution_root_outside_target_repo` で
  transport rail の手前で落とし、body も Enter も送らない。この reason は
  `target_repo_mismatch` (target *pane cwd* が repo gate に落ちた) とは別軸で、
  pane gate は通ったうえで sender 自身の `--workdir` が自分の `--target-repo` と
  矛盾している場合を指す。next action owner は sender。
- **absolute workdir は structured delivery outcome (`execution_root.workdir`) に
  だけ runtime fact として残す**。Redmine / Asana に貼る pasteable markdown delivery
  record と pane notification body には absolute path を出さない。Redmine journal や
  tracked file に personal home / private project absolute path を入れない境界
  (`public-private-boundary.md` Public Record Constraints) を満たすため。
- workdir が anchor 配下に無い (out-of-tree) / anchor 不明で relative pointer を
  計算できないときは、pasteable record / notification body は absolute を出さず、
  `execution_root.workdir` (structured outcome) を見るよう redaction 表記に倒す。
  OSS docs / defaults / tests には abstract placeholder のみ使う。
  ただしこの redaction fallback が適用されるのは **anchor が inferred の場合
  (`--target-repo` 未指定)** に限る。`--target-repo` を assert した out-of-tree は
  上記のとおり pre-send で zero-send 拒否され、record 自体が作られない
  (Redmine #14249)。
- receiver 契約は不変: pane notification は pointer であり、receiver は durable
  anchor を source-of-truth として読んでから着手する。execution root も「anchor で
  確認する」pointer であって新しい権威ではない。
- nested execution root の復元は pane scrollback / session / window name / 手 grep
  に依存しない。durable record (`- Target execution root:` 行) と structured outcome
  の `execution_root` から復元する。

JSON structured outcome の `execution_root` 形:

```json
{
  "workdir": "<abs runtime path>",
  "repo_root": "<abs repo anchor or null>",
  "relative": "rovoice/shinsei_llm"
}
```

`--workdir` 未指定 (pane cwd == execution root の通常ケース) では carrier は
`null` で、notification body / record は従来どおり execution-root 行を `—` にする。

## Projection policy

### cockpit_pane

recommended projection。multi-lane / cross-project / coordinator / sublane
運用の primary UX とする。

特性:

- session は cockpit group
- window は cockpit layout
- pane が role / lane / workspace を持つ
- role_source は pane option が primary
- workspace/lane は pane option と registry / checkout facts で確認する

### normal_window

supported compatibility projection。即退役せず、compatibility maintenance mode
として維持する。

特性:

- session は workspace canonical session
- window は `claude` / `codex`
- role_source は window name fallback になり得る
- safety / compatibility bug は直す
- new multi-lane / cross-project UX を無理に同等移植しない

### cross-project cockpit

同じ cockpit group に別 project unit を載せてよい。ただしそれは display grouping
であり、routing / governance の正本ではない。

cross-project handoff は target project の Codex Target を gateway として通す。
別 project の Claude へ direct send しない。

### Project Group projection

`cockpit_pane` projection は Project Group -> Unit -> Target の三層で表示できる。

```text
Project Group
  -> Unit (workspace/lane/governance/role set)
    -> Target (role-specific live pane)
```

この階層は display/read model のための構造である。Target resolver は Project
Group を authority として使わない。handoff は常に Unit から role を選び、最終的に
TargetRecord へ解決し、live pane / process / cwd / repo / role / workspace / lane
preflight を通す。

Project Group read model は次の入力を合成してよい:

- desired presentation config / current table: group membership, order, pinned /
  hidden, preferred projection。
- workspace registry / anchor: workspace identity と portable repo label。
- live tmux / inventory projection: TargetRecord と observed geometry。
- Redmine / workflow records: Unit の governance context への pointer。

ただし合成結果は projection であり、routing / approval / close の正本にはならない。
stale / unreadable / contradictory な入力がある場合は group の表示状態へ残し、
private policy で黙って補正しない。

#### Project Group tmux-window presentation (#12290)

Project Group ごとの分離を表す desired presentation option は、iTerm2 固有の
`tab` / `OS window` を直接 contract にせず、tmux レイヤの
`project_group_tmux_window` として扱う。

Redmine #12290 の実測では、現在の iTerm2 control-mode 環境で
`tmux new-window` が iTerm2 native tab として表示された。これは crowded な
cockpit で Project Group を切り替えやすくする opt-in 表示として有用だが、
iTerm2 の version / preference に依存する presentation result である。

したがって fast path default は引き続き `same_cockpit_column` とする。
`project_group_tmux_window` は、Project Group を同一 OS window 内の native tab
として分離できる環境では有用な opt-in だが、routing / approval / close authority
には一切使わない。Target resolver は Project Group tab ではなく、Unit -> role ->
TargetRecord の preflight で配送先を決める。

Redmine #13015 は faithful `project_group_tmux_window` 実行の上に sublane 分離を
重ねる: `delegation_window_policy: separate` (opt-in。#13085 以降の既定は `shared`
= 単一 sublane host window 再利用。正本は
`delegated-coordinator-cockpit-display.md` `## window 分離方針`) の下で、非 default
lane (worktree / clone / relocated checkout) の launch は Project Group window 内の
column ではなく専用 sublane tmux window として配置され、opt-in topology
`cockpit main window -> project window -> sublane window` が actuation で満たされる。
既定の `shared` では新規 sublane は project/common の単一 host window (faithful
実行時は Project Group window、それ以外は shared cockpit column) を再利用し、
2 本目以降の sublane が window を増やさない (#13085)。
sublane window は window-level `@mozyo_group_id` marker に `lane:<workspace_id>/<lane_id>`
key を stamp して再配置に使い (window 名は表示のみ)、cross-window duplicate gate と
pane identity stamping は Project Group window と同一。配置できない場合の fallback
(`same_cockpit_column` compat / session bootstrap / 既定 `shared`) は `--json` の
`sublane_window` field に machine-readable に記録され、silent reroute しない。
routing / approval / close authority に使わない点は変わらない。

> 実装メモ (#12264): 本 Project Group read model の **生成 (home-state projection)** は
> `src/mozyo_bridge/domain/grouped_read_model.py` に実装済み
> (`build_grouped_read_model`)。入力は #12263 の desired presentation config /
> launch-placement resolver (`presentation_grouping.resolve_launch_placement` /
> `diagnose_unit_overrides`)、#12224 の runtime observation envelope
> (`runtime_observation.RuntimeObservationSnapshot` の `observed_at` / `freshness` /
> `stale_reason` / `contradiction`)、および home-scoped な観測 Unit
> (`ObservedUnit`、観測 liveness = `active`) の **object** であり、on-disk loader /
> DB current table migration はまだ結線しない (object-to-object の最小 slice)。
> 出力 (`ProjectGroupView` / `UnitView` / `GroupedReadModel`) は display-only の
> projection であり、Target / pane / route / approval を一切持たない (`*View` 命名で
> canonical `TargetRecord` / `UnitRecord` と区別する)。config と runtime observation の
> 矛盾は visible degraded status (`identity_conflict` / `desired_unit_missing` /
> `stale` / `partial` / `unreadable` / `contradicted` / `unknown`) として残し、
> partial/reload_required な observation も `observed` にせず needs_reload に倒す。
> `desired_unit_missing` 判定は override の host-aware selector (`UnitOverride.selects`)
> で行い host-specific override を別 host で隠さない。config 不在時は repo/workspace
> label ごとの default group に分け、distinct project を混ぜない。
> hidden(desired) と active(observed) を別 bucket で分離し、
> stale/unreadable/contradictory は `healthy` を導出せず
> reload/live preflight を要求する (上記 fallback matrix と
> `runtime-observability-boundary.md` の fail-safe semantics 準拠)。action permission は
> side-effecting command の action-time live preflight が決める。残作業の on-disk loader
> 結線 (#12263 から引き継いだ `presentation:` namespace の surface selection vs grouping
> の確定を含む) は #12286 で完了した (下記 #12286 実装メモ)。

> 実装メモ (#12266): 本 Project Group read model に対する **reload / freshness UX**
> projection は `src/mozyo_bridge/domain/grouped_reload_view.py` に実装済み
> (`build_grouped_reload_view`)。flat pane cockpit の #12225 (observation line +
> Reload button) の grouped-view 対応で、`GroupedReadModel` から display-only の
> `GroupedReloadView` を導出する: whole-projection の `observed_at` / `freshness` /
> `display_state` と、read model が payload に出さない `reload_required` を view /
> group / Unit の各層で明示し、manual reload の affordance semantics
> (`ReloadAffordance`: 常時 available・auto 不発・display-only) を data として持つ。
> freshness 事実は read model から読むだけで新たな authority を作らず、
> stale/unreadable/contradicted/unobserved は `reload_required` に倒して `healthy` /
> current を導出しない (`runtime-observability-boundary.md` の fail-safe semantics と
> `### Contract handoff to follow-up issues` `#12225` 準拠)。v1 = explicit reload +
> action-time live preflight を維持し、polling / push / sidecar / background observer
> を増やさない (`## Future Push / Sidecar Observer Scope Split`)。reload は表示中
> snapshot を refresh するだけで workflow gate を動かさず side effect を authorize せず、
> grouped action は #12265 の action-time live preflight が決める。#12264 と同じく
> object-to-object slice で、served endpoint / HTML page (grouped page は未存在) は
> 結線しない。

> 実装メモ (#12255): 本 Project Group read model の **display / render projection** は
> `src/mozyo_bridge/domain/grouped_display.py` に実装済み
> (`build_grouped_display_view`)。#12264 の `GroupedReadModel` (group placement /
> lane label / issue label / role-pane presence) と #12266 の `GroupedReloadView`
> (freshness label / `reload_required`) を join し、grouped cockpit が描画する単一の
> renderable structure (`GroupedDisplayView` -> `GroupDisplaySection` ->
> `UnitDisplayRow`) に projection する。受入条件の "Project Group header を表示する" /
> "Unit ごとに lane label / issue label / Codex・Claude role panes が判別できる" を、
> header (`header_label` / `source` / `managed`) と Unit row (`lane_label` /
> `issue_label` / `roles` / `role_label`) として満たす。Codex・Claude role panes は
> `ObservedUnit` / `UnitView` に追加した display-safe な `roles` (観測された agent role
> 名のみ; pane id / session / target を持たない、`active` の presence refinement) を
> carry して表示し、`unit-target-model.md` の Project Group -> Unit -> Target が
> *display* 階層である境界を守る (pane id は action-time live preflight が持つ routing
> authority のまま)。stale / unknown / unmanaged は隠さない: degraded な
> `status` / `freshness_label` / `reload_required` を surface し、default / ungrouped
> bucket は `managed=False` (unmanaged) として区別し、no-live-target group は `stale`
> のまま残す。display は routing / approval / review / close / completion authority を
> 持たず (`GROUPED_DISPLAY_DIAGNOSTIC_ONLY_NOTE`)、grouped action は #12265 の
> action-time live preflight が決める。#12264 / #12266 と同じく object-to-object slice
> で、served endpoint / HTML page は結線しない。

> 実装メモ (#12286): 上記 object-to-object slice 群の **served grouped cockpit HTML /
> live wiring** を結線した。(1) `presentation:` namespace の確定: desired grouping
> config (`project_groups` / `grouping`) と新規の display-placement field
> `project_group_presentation` を `.mozyo-bridge/config.yaml` の `presentation:` 直下
> に置き、`repo_local_config.PresentationSelectionConfig` が surface selection と並べて
> parse する (grouping sub-key のみ `PresentationGroupingConfig.from_record` へ委譲;
> grouping schema error は `RepoLocalConfigError` に再 raise し loader の単一 except
> 境界を保つ)。`project_group_presentation` は #12290 の display placement
> (`same_cockpit_column` default / `project_group_tmux_window` opt-in /
> `normal_window` 互換) を表す display-only metadata で、`preferred_projection` とは
> 別 field とし routing / approval / window 保証を持たない。missing config は
> behavior-preserving。(2) live wiring: `cockpit_ui.observed_units_from_inventory` が
> pane-centric inventory を workspace 単位の `ObservedUnit` (role presence 集約; lane
> `default` / host `local`; stale snapshot は `active=False` の fail-safe; 同一
> workspace_id に複数 lane/worktree が projection されて 1 role に live pane が複数付く
> 場合は faithful な lane 分割ができないため、healthy な actionable Unit に collapse せず
> visible contradicted (`live_runtime_conflict`) row へ degrade し needs_reload/
> unactionable にする — #12286 review j#61995。この lane `default` 固定の制約は #12293 で
> 解消し、inventory が `@mozyo_lane_id` を読んで faithful な lane 分割を行う; degrade は
> lane discriminator が読めない collision の fail-closed fallback として残る — 下記
> #12293 実装メモ) に集約し、
> `grouped_units_payload` が repo-local grouping config + live snapshot から
> `build_grouped_read_model` -> `build_grouped_display_view` を構築する。freshness
> envelope は rows と同じ snapshot から `snapshot_from_inventory` で導出し、reload /
> freshness 表示が projection snapshot と矛盾しないようにする。(3) served surface:
> daemon の `GET /api/grouped-units` が display payload を返し (invalid repo-local
> config は 400 で fail-closed)、cockpit HTML に Project Group -> Unit -> Target
> (role panes) section を DOM-only render で追加した。`UnitDisplayRow` は action 結線
> 用に public-safe identity (`workspace_id` / `lane_id` / `host_id`) を carry するが
> pane / target は持たず、grouped action は引き続き #12265 の candidate selector +
> action-time live preflight (`grouped-reveal` / `grouped-jump`) を通る。public plugin
> API / VS Code Agent Pane へは拡張しない。

> 実装メモ (#12293): grouped Unit projection に **lane identity を結線**した。(1)
> inventory が lane を読む: `session_inventory.InventoryRecord` に `lane_id` /
> `lane_label` を追加し (cache schema v2 -> v3)、`collect_runtime_inventory` が
> `agent_discovery` 経由で pane の `@mozyo_lane_id` / `@mozyo_lane_label` (tmux layer が
> 既に読む pane option) を fold する。lane option を持たない通常 `mozyo` pane は
> backward-compatible な `default` lane に正規化する。(2) faithful split:
> `cockpit_ui.observed_units_from_inventory` の集約 key を `workspace_id` から
> `(workspace_id, lane_id)` に変更し、同一 repo の複数 lane/worktree が distinct lane id
> を持てば distinct な `ObservedUnit` (= `Unit = workspace + lane + role set`) に分割される。
> 1 つの healthy actionable row への collapse はしない。lane discriminator が読めない
> (lane option 不在で複数 pane が同一 `(workspace_id, default)` に collision する) 場合は
> 従来どおり visible contradicted (`live_runtime_conflict`) へ degrade する
> fail-closed fallback を per-lane に維持する (#12286 の degrade は廃止せず保持)。(3)
> action 解決: `cockpit_ui._resolve_unit_target` の非 default lane 拒否を撤廃し、fresh
> inventory を再 query して候補集合を `(workspace_id, lane_id, role)` で絞る。lane は
> identity selector にすぎず、表示中 projection の値ではなく action 時に live で読み直した
> lane と照合し、0 件 / 複数件は引き続き fail closed する。lane / group display metadata は
> routing / approval / review / close authority にならず、Target live preflight 境界
> (`runtime-observability-boundary.md` `## Action-Time Live Preflight Boundary`) を維持する。
> 非目標どおり tmux window / iTerm tab を identity authority にせず、public extension API /
> dynamic plugin loading も開かない。

> 実装メモ (#13356): grouped Unit projection に **backend 軸**を通した (design
> j#73386 案 A)。`ObservedUnit` / `UnitView` / `UnitDisplayRow` は additive な
> `backend` (default `tmux`、既存 tmux row は表示互換) を carry し、herdr Unit は
> live `herdr agent list` の mzb1 decode (#13247) を `cockpit_payload.herdr_observed_units`
> が同一 read model へ fold する (#13303 membership fold の視覚面拡張。default-off /
> config fail-soft / unreadable inventory は diagnostic として可視)。herdr Unit の
> 人間可読 identity (`lane_label` / `issue`) は **lane metadata record**
> (`managed-state-model.md` の `lane_metadata_records` native component) の display
> join であり、record 欠落は `wt_<hash>` 生値 + `lane_record_missing` の fail-open
> degrade。per-role `agent_status` は core receiver-state 語彙 (busy / blocked /
> awaiting_input / turn_ended / unknown) へ写像した **runtime observation layer**
> (`role_runtime_states` / row の `runtime_blocked` / summary の herdr counts) として
> 供給し、Redmine workflow attention / blocked gate へ昇格させない (runtime-blocked
> は別 label)。herdr 行の観測 envelope は tmux snapshot と独立の live-query
> (`source="herdr"`) で、非 tmux Unit の `unit_id` は backend-qualified
> (`unit:host:ws:lane:herdr`) にして hybrid 観測の衝突を防ぐ。routing / approval /
> close authority への非昇格境界は従来どおり (herdr 行の grouped action は disabled)。

> 実装メモ (#12296): 本 Project Group read model に対する **detail / command preview**
> projection は `src/mozyo_bridge/application/grouped_detail.py` に実装済み
> (`build_grouped_unit_detail`)。operator が grouped cockpit で Unit / Target を選んだ
> とき、その Unit の public-safe identity / state と「次に実行できる安全な action」を
> 一画面で見せるための display 投影で、#12264 の `UnitView` 一行から
> `GroupedUnitDetailView` (+ 行ごとの `CommandPreview`) を導出する: 観測された
> agent role pane (`codex` / `claude`) ごと x cockpit action (`reveal` / `jump`) ごとに
> 1 件の command preview を出す。preview は **情報であって権限ではない**:
> `live_preflight_required=True` を常に立て、実行は #12265 の action-time live preflight
> (`cockpit_ui.grouped_reveal` / `grouped_jump` -> `_resolve_unit_target`) を必ず通す。
> availability は表示行だけから fail-closed に導出し (`candidate_unit_selector` /
> `_resolve_unit_target` の refusal gate を preview として写す): degraded
> (`needs_reload`) 行 / 非 local host / live target 未観測 の行は
> available な command を一切出さず、各 action を可視の reason 付き unavailable として
> 残す (silently drop しない)。非 default lane は #12293 で `@mozyo_lane_id` を読む
> faithful な lane 分割が入ったため、もはや projection の block 条件ではなく first-class な
> identity selector である (#12303 統合: `grouped_detail._blocking_reason` の非 default
> lane 拒否を撤廃し、command preview の selector が行の `lane_id` を載せて live preflight が
> 同一 lane で再解決する)。stale / contradictory / ambiguous が action unavailable
> として現れる受入のうち、ambiguous (同一 identity に複数 live pane) は表示投影からは
> 見えないため、live runtime を read-only で再観測する非破壊の counterpart
> `cockpit_ui.grouped_action_preview` を追加した: 既存の `_resolve_unit_target` をそのまま
> 走らせ side effect は出さずに `{available, reason}` を返す (stale / ambiguous / missing /
> remote を live に検出; 非 default lane は #12293 以降 narrowing 用の identity selector で、
> 該当 lane の pane が無い場合のみ missing として unavailable)。これを served の
> `POST /api/actions/grouped-preview`
> (#12265 と同じ token gate、常に 200、非変更) に結線し、grouped cockpit detail 画面が
> 副作用なしに現在の availability を確認できるようにした。detail payload は public-safe な
> identity (`workspace_id` / `lane_id` / `host_id`) / display label / status・freshness token /
> 観測 role 名 / 公開済み action endpoint と selector のみを持ち、pane id / repo path /
> credential / prompt body を UI JSON / HTML に出さない
> (`GROUPED_DETAIL_DIAGNOSTIC_ONLY_NOTE`、`public-private-boundary.md`)。detail 投影は pure
> (`grouped_detail.py`) で、preview / served wiring は application 層に置く。

> 実装メモ (#12297): Project Group header の **attention / freshness summary** を
> `src/mozyo_bridge/domain/grouped_display.py` に追加した
> (`GroupAttentionSummary` / `GroupDisplaySection.summary` /
> `GroupedDisplayView.summary`)。受入条件の "group header に active lanes /
> stale・reload-required / blocked・attention candidate の summary を表示する" を、
> display row 上に既にある事実だけから導出する projection-only な 3 つの独立 count
> として満たす: `active_lanes` (live Target を持つ row = active lanes)、
> `reload_required` (snapshot が current でない row; 各 row の `reload_required` flag
> をそのまま数える)、`attention` (contradiction-class の attention candidate =
> `ATTENTION_CANDIDATE_STATUSES` = `contradicted` / `identity_conflict` /
> `desired_unit_missing`; reload では解消しない、operator が *resolve* すべき row)。
> count は partition ではなく独立 projection で、`attention` ⊆ `reload_required`。
> summary は group header (どの group / Unit を先に見るか) と whole-projection
> roll-up の両層で持ち、hidden member も含めて数える。**projection-only**: Redmine
> journal 本文 / owner approval / review state / governance `blocked` truth を一切
> 読まず (それは durable record の正本; UI への複製は #12297 の非目標)、
> `cockpit-attention-state.md` の governance `attention_state` (owner_waiting /
> review_waiting / blocked 等、Redmine 由来) とは別物の表示派生値である。routing /
> approval / review / close authority を持たない
> (`GROUPED_SUMMARY_DIAGNOSTIC_ONLY_NOTE`)。#12255 / #12264 / #12266 と同じく
> object-to-object slice で、served endpoint / HTML page は結線しない。

> 実装メモ (#12302): `project_group_presentation` を **sublane launcher /
> cockpit append placement** に結線した。`cmd_cockpit` (`commands.py`) が
> `load_repo_local_config(repo_root).presentation.grouping` を読み、当該 workspace の
> `resolve_launch_placement` 結果と新規 pure 関数
> `presentation_grouping.resolve_group_window_placement(mode, placement)` から
> desired placement decision (`GroupWindowDecision`) を導出する。`same_cockpit_column`
> (既定 / config 不在) は現行 column append/create を一切変えない (behavior-preserving)。
> opt-in surface (`project_group_tmux_window` / `normal_window`) は **desired
> presentation として記録**するが、`executed_surface` は `cockpit_column` のままで
> **visible degrade** する (json `presentation` payload / dry-run / 実行ログに diagnostic
> を surface; routing を黙って変えない)。理由は、現 cockpit の append/create/focus/
> duplicate-detection/reset/rebalance/daemon が単一 `cockpit` window 固定であり、単一-window
> append 経路から別 tmux window を spawn すると duplicate-detection / pane-identity gate を
> 回避してしまうため。したがって本 slice は #12290 の tmux-window 表示を *desired* metadata
> として launcher に通すが、tmux window / iTerm tab / OS window は保証せず、placement を
> routing / approval / review / close authority にもしない。不正 placement config は
> `RepoLocalConfigError` で fail-closed (real run は die、`--json` / `--dry-run` は report)。
> Target resolution と side-effecting action は引き続き action-time live preflight /
> pane identity gate が決める (`runtime-observability-boundary.md`
> `## Action-Time Live Preflight Boundary`)。real な multi-window cockpit surface 管理
> (per-group window を duplicate-detection / focus / reset まで faithful に追跡する) は
> follow-up scope (#12330 で実装)。public extension API / dynamic plugin loading は開かない。

> 実装メモ (#12330): `project_group_tmux_window` を **faithful に execute** するよう
> #12302 の visible-degrade を昇格した。`cmd_cockpit` は
> `resolve_group_window_placement(mode, placement, execute_group_window=True)` を呼び、
> `project_group_tmux_window` のとき `executed_surface=group_tmux_window` (degrade なし) を
> 得る。faithful path は cockpit session 内に Project Group ごとの **専用 tmux window** を
> create / append / cross-window focus する (`commands._cockpit_group_window_action`)。
> 維持する不変条件:
>
> - **identity は pane option のまま**。group window の pane も
>   `pane_identity_commands` で `@mozyo_workspace_id` / `@mozyo_agent_role` /
>   `@mozyo_lane_id` を従来どおり stamp する。duplicate detection は **cross-window** で、
>   同一 `workspace_id + lane_id` の codex pane が **どの window** にあっても focus 優先 (新規
>   placement を二重に作らない)。`pane_lines()` は `list-panes -a` で session 全体を読むため、
>   group window の pane も target resolution / daemon / `agents targets` から従来どおり見える。
>   daemon / `agents targets` / pane-identity gate は無変更。
> - **window NAME は identity でない**。group の既存 window の特定は mozyo が deterministic に
>   stamp する **window-level** option `@mozyo_group_id` (display hint) で行い、window 名や
>   pane identity option では行わない。window 名は public-safe に sanitize した display label
>   (`sanitize_group_window_name`) のみ。
> - **discovery は multi-window かつ window-id keyed**。`commands._read_managed_cockpit_windows`
>   が session の各 window を stable な `#{window_id}` で列挙・pane 読み取りし
>   (`#{window_name}` は display のみ)、`@mozyo_workspace_id` を持つ pane を carry する window
>   (`cockpit` home + group windows) を返す。`@mozyo_group_id` を append target の locator に
>   使う。window 名が衝突しても (label が同一文字列に sanitize される 2 group) window-id で
>   distinct なため collapse / hide されず、名前は routing 依存にならない (#12330 review j#62380)。
>   group_id が空 (ungrouped) の Unit は window を共有せず常に専用 window を作る。
> - **preview / dry-run / visible diagnostic / rollback**。`--json` は `action`
>   (`group_create` / `group_append` / `group_focus`) と `group_window` を、`--dry-run` は
>   plan command と group window 注記を出す。create / append は
>   `execute_cockpit_plan(..., cleanup_captured=True)` で rollback し、captured pane を
>   kill すると tmux が空 window を drop するため、失敗した group-window 生成は orphan window を
>   残さない。
> - **session bootstrap は behavior-preserving**。cockpit session 未作成の初回 `mozyo cockpit`
>   は従来どおり `cockpit` home window に Unit を seed し、group window は次回以降の launch で
>   additive に作る。これにより reset / rebalance / reconcile / doctor-geometry が依拠する
>   `cockpit`-window identity model を壊さない。**再評価条件**: 初回起動から即 group window を
>   開きたい需要、または既存 Unit を group window へ migrate する需要が出た場合に bootstrap 境界を
>   見直す。
> - **whole-cockpit op の multi-window 安全性 (Unit 5)**。`reset` の `kill-session` は同一 session の
>   group window も破壊するため、preview / 実行ログに「他 window も破壊する」warning を可視化する
>   (confirm gate は維持)。`rebalance` / `reconcile` / `doctor-geometry` は `session:cockpit`
>   window のみを対象とし group window を read も mutate もしない (構造上 multi-window 安全;
>   blast radius を cockpit home window に限定する)。
>
> 非目標は #12330 どおり: iTerm 固有 tab / OS window の product guarantee なし、private operator
> layout policy の OSS default 化なし、public extension API / dynamic plugin loading なし、
> release / publish / tag なし。tmux window / iTerm tab / OS window は依然 routing / review /
> approval / close authority ではない。

> 実装メモ (#12336): #12330 が主張する「window NAME は identity でない / `agents targets` は
> 従来どおり見える」不変条件を、**target inventory の ambiguity 分類**でも貫いた。#12330 の
> post-close live smoke (j#62426) で、同一 Project Group の sublane を複数 launch し display 名が
> 衝突する tmux window が複数できると、`agents targets` がそれらの pane を
> `AMBIG=1` / `attention=unknown` / `reason=contradictory_sources` と誤分類した
> (`ROLE_SOURCE=pane_option` / `CONF=strong` であっても)。原因は
> `agent_discovery.discover_agents` が `(session, window_name)` の重複 (= 同一 display 名の
> 複数 window) を **role 識別の出所に関わらず** `window_ambiguous` として OR していたこと。
> 修正: duplicate `(session, window_name)` を ambiguity に算入するのは **window 名が role 識別の
> authority であるとき (`role_source == window_name` の legacy rail) に限定**する。role が pane
> option から解決した pane では window 名は display-only であり、pane id + option + lane が一意に
> target を識別するため、display 名の衝突では ambiguous にしない。これにより #12330 が grouping の
> ために意図的に共有する重複 display 名 (`project:<repo>` group) が strong な pane-option identity を
> 無効化しなくなる。legacy window-name rail (同一 session に `claude` window が 2 つ等) の
> fail-closed は不変。`agents targets` の ambiguous → attention `contradictory_sources` 写像
> (`commands._attention_for_candidate`) 自体は変えず、誤って立っていた `ambiguous` を discovery 層で
> 立てないようにする最小修正。tmux window 名は引き続き display-only で routing authority ではない。
> 回帰テスト: `tests/test_agent_role_identity.py`
> (`test_duplicate_group_window_names_keep_pane_option_unambiguous` /
> `test_duplicate_window_names_still_ambiguous_on_window_name_rail`)、
> `tests/test_compact_target_discovery.py`
> (`test_duplicate_group_window_names_stay_healthy_not_contradictory`)。

## TargetRecord / UnitRecord

### TargetRecord

JSON projection の概念例:

```json
{
  "host": {"id": "local", "label": "local", "kind": "local"},
  "runtime": {
    "provider": "tmux",
    "session": "mozyo-cockpit",
    "window": "cockpit",
    "pane_id": "%953",
    "process": "codex",
    "cwd": "<local path>"
  },
  "identity": {
    "workspace_id": "...",
    "lane_id": "default",
    "role": "codex",
    "role_source": "pane_option",
    "confidence": "strong",
    "ambiguous": false
  },
  "repo": {
    "label": "mozyo_bridge",
    "branch": "main"
  },
  "view": {
    "kind": "cockpit_pane",
    "group": "mozyo-cockpit",
    "active": true
  }
}
```

JSON は CLI / API projection であり、保存正本ではない。TargetRecord を unit /
target ごとの JSON file として永続化しない。

### UnitRecord

UnitRecord は TargetRecord の grouping である。

```json
{
  "unit_id": "unit:<host>:<workspace_id>:<lane_id>",
  "workspace_id": "...",
  "lane_id": "default",
  "repo_label": "mozyo_bridge",
  "branch": "main",
  "targets": {
    "codex": "tmux:<host>:<pane_id>",
    "claude": "tmux:<host>:<pane_id>"
  },
  "governance": {
    "ticket_system": "redmine",
    "owner_facing_role": "codex"
  }
}
```

UnitRecord は作業単位を表す。handoff は UnitRecord から role を選んで
TargetRecord へ落としてから行う。

### ProjectGroupRecord

ProjectGroupRecord は UnitRecord の表示 grouping である。

```json
{
  "group_id": "project:mozyo_bridge",
  "label": "mozyo_bridge",
  "source": "desired_presentation",
  "units": ["unit:<host>:<workspace_id>:default"],
  "display": {
    "position": 10,
    "collapsed": false,
    "stale": false
  }
}
```

`group_id` は portable display key であり、Redmine project id や repo path の
二重正本ではない。`label` は public-safe display label に限る。private absolute
path、private host name、operator 固有色・並び順を OSS default として保存しない。
private consumer が独自の grouping / color / layout policy を持つ場合は、その
consumer 側の config / runbook に置く。

## State boundary

```text
workspace identity      -> registry.sqlite + minimal workspace anchor
runtime liveness        -> live tmux
inventory projection    -> inventory.sqlite cache + JSON output
desired presentation    -> DB current tables (future)
desired event history   -> managed-events.sqlite / event tables
workflow completion     -> Redmine journal/status
```

DB current table の To-Be 境界は `unit-presentation-state-db.md` を正本とする。
この doc では層だけを示し、table schema を重複定義しない。

### Static file に残すもの

- docs catalog
- rules
- scaffold governance
- generated guard docs
- project defaults
- minimal workspace anchor
- human-readable docs / runbooks / specs

### DB に寄せるもの

- mutable desired state
- cockpit group membership
- Project Group membership / order / display preference
- projection preferences
- pinned / hidden / retired
- target observation cache

### DB に寄せないもの

- live liveness
- pane existence
- foreground process
- cwd truth
- Redmine review / owner approval / completion
- Project Group から推測した routing / approval / close authority

## File naming direction

現状:

```text
.mozyo-bridge/workspace.json
.mozyo-bridge/workspace-defaults.yaml
```

責務上のより良い名前:

```text
.mozyo-bridge/workspace-anchor.json
.mozyo-bridge/project-defaults.yaml
```

rename は互換 migration が必要である。旧 file read fallback、新規 write、doctor
warning、scaffold / docs / tests 更新を設計してから行う。file を増やさず rename を
基本方針とする。

rename の判断と compatibility story は `workspace-anchor-project-defaults-migration.md`
を正本とする。

## Anti-patterns

- `window_name == role` を primary identity に戻す。
- cockpit resolver と normal resolver を別々に育てる。
- normal local に cockpit と同じ multi-lane UX を無理に移植する。
- normal local を silent deprecated にして壊れたまま放置する。
- cockpit layout を core identity にする。
- Project Group を Unit identity / routing authority にする。
- unit / target ごとの JSON file を保存正本として増やす。
- project group ごとの private layout / color / path policy を OSS default に入れる。
- `workspace.json` に lane / cockpit / projection state を足す。
- `registry.sqlite` に pane / window / process / cwd を入れる。
- `inventory.sqlite` を liveness 正本にする。
- Redmine gate / completion を mozyo DB へ複製して正本化する。
- private cockpit composition / operator policy を OSS default に入れる。

## 実装順序

1. 本 doc で model / schema / resolver priority を固定する。
2. `agents targets` を TargetRecord canonical projection に拡張する (#11907)。
3. handoff / pane resolver を TargetRecord 経由へ寄せる (#11908)。
4. desired / presentation state の DB current table 境界を設計する (#11909)。
5. workspace anchor / project defaults の rename migration を判断する (#11910)。
6. cross-project cockpit smoke / runbook を定義する (#11911)。

cross-project cockpit の具体的な preview / append / adopt / discovery / handoff smoke は
`cross-project-cockpit-smoke-runbook.md` を正本とする。

local host と remote SSH host の cockpit 境界は
`local-remote-cockpit-host-boundary.md` を正本とする。`session` / `window` /
`pane_id` は host の tmux server 内でだけ意味を持つため、host をまたぐ discovery
や handoff では host-aware preflight を落としてはいけない。

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --repo . --check`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
