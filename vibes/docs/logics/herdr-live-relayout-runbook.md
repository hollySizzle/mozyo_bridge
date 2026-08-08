# herdr live pane 再配置 runbook (operator 向け)

live な herdr pane pair (coordinator + auditor / gateway + worker 等) の **位置交換 (swap)** と **split 方向変換 (左右 ⇔ 上下)** を、実機で検証済みの手順として replay 可能な形で固定する。2026-07-12 の live 実測 (herdr 0.7.1) で確立した recovery recipe と、その安全境界・herdr 側 gap、および identity-bound な製品 command を記録する (Redmine #13648 / #13664 / #14608)。

> **Herdr 0.8移行注記 (#15101、2026-08-08):** managed launchは`pane split <exact pane>`で
> 新paneを作り、そのpaneへ`agent start <mza1> --kind <provider> --pane <pane>`を実行する方式へ
> 移行した。したがってfresh launchはactive paneへの暗黙splitに依存しない。本runbookのA–Dは
> 既存live paneを動かす別操作であり、記載された実測versionは0.7.1/0.7.4のままである。
> 製品commandが使うswap / resizeのsignatureと応答schemaは0.8のbundled schemaで再照合済み。
> 下記の手動recipeを0.8で追加実行する際は各`--help`とread-only layoutを先に照合し、未再実測の
> signatureを推測で実行しない。

対象は **既存 live pair の再配置**。標準入口は下記の preview-first 製品 command、低レベルの Herdr command は部分失敗時の調査・復旧 recipe である。設定駆動の恒久配置 (`lane_placement`) との境界は下記「設定駆動配置との境界」を読む。設計正本は [[spec-herdr-native-identity]] (target authority = herdr assigned name)、lane 運用手順の正本は [[task-herdr-lane-operations]]、pane identity / marker の意味構造は [[logic-pane-centric-cockpit-semantics]]。本書は手順のみを扱い規約本文を複製しない。

## 適用範囲と非 scope

- **scope**: 既に launch 済みの dedicated live pane pair を Unit identity から preview / apply する製品 command、および operator が Herdr CLI で状態を調査・復旧する手順と安全性の根拠。
- **非 scope**:
  - herdr 本体の改修 (same-tab re-split / rotate action の追加)。
  - shared tab の一部だけを動かすこと、3 pane 以上の tab の自動組み替え。
  - agent の kill / relaunch、workflow role / route / durable state の変更。
  - 設定駆動の恒久配置 (`.mozyo-bridge/config.yaml` の `lane_placement`。#13646 / #13647) 自体の編集。
- 製品 command は外部 binary (`herdr`) の pane API を呼ぶ。対応 signature は実行時の Herdr 0.8契約とテストで固定し、未記録の signature は推測で埋めない。低レベル復旧では `herdr pane --help` / 各 subcommand の `--help` を実機正本にする。

## 標準入口: identity-bound preview / apply (#14608)

対象は pane id ではなく、登録済み workspace と exact lane id で指定する。command は live inventory
から現在の pane id を都度解決するため、pane id を引数へ渡す面は持たない。

```sh
# read-only。現在値、設定上の目標値、必要な操作だけを表示する
mozyo-bridge herdr pair-placement preview --repo <project-root> --lane default

# preview と同じ検査を直前に再実行してから、明示的に適用する
mozyo-bridge herdr pair-placement apply --repo <project-root> --lane default
```

`--workspace <registered-workspace-id>` も使える。ただし `--repo` と同時指定した場合は、両者が
同じ登録 workspace を指すことを要求する。`--json` は同じ判定を機械可読で返す。

適用前の必須条件:

1. Unit に exact 2 agent だけがあり、assigned name、provider、live 状態、workspace directory が一致する。
2. 2 agent の現在の launch generation が attested で、現在の locator と一致する。
3. 2 pane が同じ tab を排他的に占有し、1 本の `down` または `right` divider を形成する。
4. 目標 split / order / ratio が現在の `lane_placement` から一意に解決できる。sublane の既定 order は現在の provider binding ではなく、その lane に保存された gateway / worker pair を使う。

apply は検査結果が preview 後に変わっていれば mutation 前に拒否する。split 方向変更の 2 段 bounce
では、退避後に元 tab と一時 tab がそれぞれ expected singleton であること、agent identity / generation / target
設定が不変であることを確認してから戻す。swap / bounce の後と resize の各 pass 前、最後にも live layout を
読み直す。pane の kill / relaunch は行わない。

Herdr 0.8では、process exit 0だけを変更証拠にしない。swapは
`result.type = pane_swap`かつ`result.swap.changed`、resizeは
`result.type = pane_resize`かつ`result.resize.changed`を厳密に読む。`changed: false`は既知の
無変更、field欠落・型違い・別result typeは効果不明として扱い、生の応答本文は公開出力へ出さない。
resizeの試行回数も変更証拠には使わない。

結果の扱い:

- `matched`: 変更不要。
- `applied`: 操作後の再計測が目標値と一致。
- `refused`: identity、generation、liveness、layout、設定の前提が成立せず、mutation 無し。
- `failed`: command が変更しなかったことを確認済み。原因を解消し、再度 preview してから apply する。
- `partial_failure`: 既に変更した、または外部 command の効果を確定できない。blind retry せず、workspace / lane で Unit を再確認し、下記 recipe で live layout を調査してから preview へ戻る。

公開出力は workspace / lane / provider label を表示用に正規化し、credential 形・絶対 path・terminal
control をそのまま反映しない。pane id、tab id、launch generation は出力しない。

## 前提 / 用語

- **target identity はlogical identity権威**: paneのroute authorityはmozyoの`mzb1` logical identity
  + live inventoryである。Herdr 0.8内部では32文字の`mza1`を使うが、binding storeで`mzb1`へ
  復元してからroute判定する。pane位置・tab配置・pane id・raw `mza1`はdurable authorityではない
  ([[spec-herdr-native-identity]])。操作前にlogical identityとlive状態を確認する。
- **tab join の権威は `tab_id`**: どの pane が同一 tab に属するかは live inventory の `tab_id` のみが authority で、tab label は cosmetic (#13411)。bounce で「元の tab へ戻す」際は label ではなく元 tab の `tab_id` を指定する。
- **設定だけでは live pair を動かさない**: herdr は same-tab re-split を拒否するため (下記)、`lane_placement` 設定 (#13646 / #13647 / #14569) を保存しただけでは既存 live pair の配置は変わらない。設定は fresh launch / heal の geometry と、`pair-placement apply` が既存 dedicated pair へ適用する目標値を決める。方向変換で製品 command が内部利用する検証済み低レベル手順が recipe B / C である。
  - **fresh coordinator append の自動 reflow (#14996 R2 / #15098)**: `role_grouped_space` の shared `project-coordinators` または `shared_space` の `coordinators` workspace へ **fresh な coordinator pair を append する launch** は、その pair を独立 column にするために recipe B と同じ 2 段 bounce を自動で 1 回だけ行う。続けてRIGHT軸dividerを対象指定でresizeし、全project列の幅差を1cell以内へ揃える。これは dedicated two-pane tab を対象とする `pair-placement` とは別の shared-tab launch-time path である。境界の正本は [[spec-herdr-native-identity]] の `### project column geometry — append 時の狭い verified relayout (#14996 R2)`。
  - **fresh full-pairの設定列順・相対幅 (#15123)**: 上記reflow、pair内ratio、launch generation、startup transactionが全て成功した後、全Unitが2-paneなら`position`順と`relative_width`を自動適用する。隣接Unitのlower paneを退避してtop同士をswapし、lowerを元の内部ratioで戻した後、RIGHT軸dividerを設定weightへresizeする。commandごとにtyped変更結果と全Unitの世代・layoutを再確認する。1-pane混在は`deferred_until_full_pair_set`でzero-write、途中失敗はblind retry禁止である。これは既存live Unitを任意に動かすplugin actionではない。
    - 11列以上は均等幅にせず、reflowがfull-height列形状だけを確認して後続設定planへ引き渡す。中間結果だけでは起動成功にせず、設定ratioの実測成功が必要である。
  - #14568 で未設定既定が縦 (`down`) になったため、**既定変更より前に立ち上げた live pair は左右のまま残る**。左右のまま使い続けても不整合ではない (設定と live 配置は別 authority)。今すぐ縦に揃えたい場合は下記 recipe B を使い、pair を再起動できる場面なら fresh launch に任せる方が安全である (live 操作を伴わない)。

## herdr 0.7.1 の制約 (2026-07-12 実測)

- **same-tab re-split は 1 発 API が無い**: 同一 tab 内で split 方向を変換する直接 command は存在しない。`herdr pane move <id> --tab <同一 tab_id>` は `changed:false` / `reason:same_tab` を返す **no-op** で、方向は変わらない。
- **`herdr pane swap` は位置交換のみ**: pair の左右 (または上下) の位置を入れ替えるだけで、split の **方向** は変換しない。
- **方向変換は 2 段 bounce が必要**: 一旦別 tab へ退避してから、元 tab へ望む split 方向で戻す (下記 recipe)。

## 検証済み recipe

いずれも操作前に対象 pane の assigned name と live 状態を確認し (target identity 権威)、期待した pane を掴んでいることを実測してから実行する。失敗・想定外の応答 (`changed:false` 等) が出たら停止し、blind に再実行しない (fail-closed)。

### A. 位置交換 (swap)

同一 tab 内で 2 pane の位置だけを入れ替える (split 方向は保つ)。herdr 0.7.1 の `herdr pane swap` は 2 形式を持つ。

```sh
# 明示 2-pane 指定: source と target の位置を交換する
herdr pane swap --source-pane <pane-a> --target-pane <pane-b>

# 方向指定: 対象 pane を指定方向の隣接 pane と交換する (TUI の swap_pane_{left,right,up,down} に対応)
herdr pane swap --direction left|right|up|down [--pane <pane-id>|--current]
```

- 効果: 対象 pane の表示位置を交換する。split の方向は変えない。assigned name / route は不変。
- 上記は herdr 0.7.1 の `herdr pane --help` で確認した verified signature。positional の `herdr pane swap <pane-a> <pane-b>` 形式は **存在しない** (指定は `--source-pane` / `--target-pane` か `--direction`)。

### B. 左右 → 上下 (down 化) の 2 段 bounce

左右 split の pair を上下 split に変換する。片方の pane を一時 tab へ退避し、残す pane の **下** へ戻す。

```sh
# 0. 現状確認: pane id と元 tab の tab_id を list で確認、現在の split 方向は layout API で実測
herdr pane list [--workspace <workspace_id>]
herdr pane layout [--pane <pane-id>|--current]   # ← 現在の split 方向 (direction) を読む layout API

# 1. 退避: 動かす pane を新しい一時 tab へ移す
herdr pane move <moving-pane-id> --new-tab

# 2. 戻す: 元 tab へ、残す pane の下 (down) に戻す
herdr pane move <moving-pane-id> --tab <original-tab-id> --split down --target-pane <staying-pane-id>
```

- `--target-pane` は tab に残っている pane。`--split down` はその pane に対して戻る pane を **下** に配置する。
- 一時 tab は最後の pane が退去した時点で herdr が自動消滅させる (husk は残らない)。
- 検証: 戻した後に `herdr pane layout` で対象 pane の `direction` が `down` になっていることを実測確認する (#13646 close condition の「layout API 実測 `direction: down`」に対応)。

### C. 上下 → 左右 (right 戻し) の 2 段 bounce

recipe B と対称。`--split down` を `--split right` に置換して、上下 pair を左右へ戻す。

```sh
herdr pane move <moving-pane-id> --new-tab
herdr pane move <moving-pane-id> --tab <original-tab-id> --split right --target-pane <staying-pane-id>
```

- 検証: `herdr pane layout` で `direction` が `right` になっていることを実測確認する。

### D. live pair の分割比率を変える (Redmine #14569、2026-07-28 実測 / herdr 0.7.4)

split の **方向**ではなく **配分**を、既に立っている pair に対してその場で変える手順。方向変換 (recipe
B / C) と違い bounce は不要で、`herdr pane resize` 1 系統で完結する。

```sh
# 0. 現状確認: 対象 pane の assigned name と live 状態を確認し、現在の ratio を実測する
herdr pane list [--workspace <workspace_id>]
herdr pane layout --pane <pane-id>   # splits[].ratio が現在値、splits[].direction が軸

# 1. divider を動かす (direction は「動かしたい向き」であって pane の側ではない)
herdr pane resize --pane <pane-id> --direction down|up|right|left --amount <0..0.5>

# 2. 検証: 宣言したかった比率になったかを layout で実測する
herdr pane layout --pane <pane-id>
```

2026-07-28 の live 実測 (isolated scratch workspace、agent 無し。probe 後 `workspace close` 済み) で
確定した挙動:

- **`splits[].ratio` は first child の占有率**。`direction: down` なら上 pane、`right` なら左 pane。
  pane の実 extent は `round(split_extent * ratio)` (extent 75 で ratio 0.5 → 38/37、0.6 → 45/30)。
- **`--direction` は divider を動かす向き**であり、`--pane` がどちら側かに依らない。`down` / `right` が
  first child の取り分を増やし、`up` / `left` が減らす。pair のどちらの pane を `--pane` に渡しても
  同じ divider が同じ向きに動く。
- **`--pane` は「どの divider か」だけを選ぶ**。herdr は指定 pane の祖先のうち **direction と軸が一致する
  最も近い split** を動かす。nested layout ではこれが外側の divider になりうるので、**動かす前に
  `pane layout` の rect を読んで、狙った divider が最近祖先であることを確認する**。確認せずに撃つと
  隣の pane / lane を再配置する。
- **`--amount` は 1 回あたり 0.5 に clamp される**。0.1 → 0.9 のような大きな移動は 2 回に分ける
  (実測: ratio 0.1 に対し `+0.55` / `+0.79` / `+0.8` はいずれも 0.6 で止まる)。**毎回 layout を読み直して
  残差から次の amount を決める**。exit 0 は「動いた」証拠にならない。
- **結果 ratio は `0.1..0.9` へ silent clamp される** (0.5 に `up 0.9` を当てると 0.0 ではなく 0.1)。
  範囲外を狙っても静かに端で止まるので、狙いは必ず layout で照合する。
- **非有限 / 非数値の `--amount` は `invalid amount: <v>` / exit 2 で拒否**され、layout は変化しない。
- assigned name / route / projection は不変 (recipe A–C と同じ安全性根拠。divider を動かすだけで
  pane を move / swap / kill しない)。

## 安全性の根拠 (実測)

2026-07-12 の live 実測で、上記 recipe が以下を保つことを確認した。

1. **assigned-name authority 不変**: 再配置は表示位置を変えるだけで、pane の assigned name / route / projection を変えない。handoff / dispatch の宛先解決は assigned name + live inventory 経由で自動追従する ([[spec-herdr-native-identity]])。
2. **agent process 無傷**: 退避・戻しの間に pane 内の Claude / Codex TUI process は終了・再起動しない。session / 会話状態は保持される (pane の移動であって kill / relaunch ではない)。
3. **一時 tab の自動消滅**: bounce で mint した一時 tab は最後の pane が退去した時点で herdr が自動 close する。手動 cleanup は不要で、husk は残らない。
4. **target identity 確認**: 操作前に対象 pane の assigned name と live 状態を実測し、pane id を durable target として扱わない。掴む pane を取り違えないことを再配置の前提にする。
5. **失敗時 fail-closed**: `changed:false` / `reason:same_tab` 等の想定外応答、または対象 pane の消滅を観測したら停止し、durable record に残してから判断する。blind な再実行・別 pane への当て推量操作をしない。

## herdr 側 API / TUI gap と upstream 追跡

- **CLI gap (herdr 0.7.1)**: same-tab re-split (同一 tab 内の split 方向変換) を 1 発で行う API は無い。`pane move --tab <同一 tab>` は `same_tab` で no-op、`pane swap` は位置交換のみ。方向変換は本書の 2 段 bounce が唯一の経路。
- **TUI gap (herdr 0.7.1)**: keybindable action として `swap_pane_left` / `swap_pane_right` / `swap_pane_up` / `swap_pane_down` は実在するが、split の **orientation 変換** (左右 ⇔ 上下) を行う action は binary の keybind 語彙 全走査でも不存在。
- **upstream 追跡状態**: herdr upstream への機能要望 (same-tab re-split / rotate action) は **2026-07-13 時点で未提出**。owner 方針により外部への投稿はしない (#13648: 「まあひとまずはじゃあいいよ、APIのみで」)。要望を提出する場合は owner 承認を得てから行い、提出後は本節にその追跡先 (upstream issue link) を追記する。それまでは本 recipe が回避策の正本。

## 設定駆動配置との境界

- 恒久的な pair 配置 (どの lane class を左右 / 上下、どちらの provider を先に置くか、その pair をどんな比率で割るか) を宣言駆動にする作業は別 US: `.mozyo-bridge/config.yaml` への閉集合 `lane_placement` block 追加が #13646、親子孫 3 層別 (lane-role 別) の keying が #13647、pair 内部の相対 split 比率 (`ratio`) が #14569。config key は `lane_placement` であり `pane_placement` では **ない** — repo-local schema boundary は `pane` を含む key を allowed-key 判定より前に拒否するため、旧名で書いた config は fail-closed で拒否される (正本: [[spec-herdr-native-identity]] §5.1)。
- `lane_placement` の保存自体は既存 live pair を動かさない。新規 launch / heal は設定を自動利用し、既存 dedicated pair へは operator が `pair-placement preview` で差分を確認してから `apply` を明示する。partial failure の調査・復旧では本書の低レベル recipe を使う。
- Unit列の`position` / `relative_width`は別軸で、fresh full-pair append時だけ#15123がshared tab全体へ自動適用する。adopt-only / heal / 既存live tabへの任意操作では動かない。既存Unit列をUIから移動する場合は、全体previewと再照合を持つ後続plugin actionを使い、本fresh-launch処理を手動反復しない。
- **`ratio` (#14569) も同じ境界**である。`lane_placement.<class>.ratio` を変えても既存 live pair は自動 resize されない。設定が pane を触るのは **その launch 自身が今作った divider に対して 1 度だけ**で、既に立っている pair の divider には届かない。今すぐ live で比率を変えるなら本書 **recipe D** を使い、pair を再起動できる場面なら fresh launch に任せる方が安全である (live 操作を伴わない)。
- 設定した比率と実機の食い違いを疑ったら、まず `mozyo-bridge config status` の `lane_placement.<class>.ratio` leaf row で **宣言値 (`declared`) か既定 (`default`) か**を読み、次に `herdr pane layout` で **実 ratio** を読む。両者は別 authority なので、片方だけを見て「設定が効いていない」と判断しない (設定は次の fresh launch / heal の geometry を決め、layout は今の geometry を報告する)。

## 記録の衛生

- journal / commit message に host-local 絶対 path や pane の内部 id を durable target として書かない。pane は assigned name / lane label / role で参照する (正本: [[rule-public-private-boundary]])。
- pane id は snapshot であり durable identity ではない。再配置の記録は「どの assigned name の pane を down/right に変換した」という identity 軸で残す。
