# herdr 配布面: pin posture + opt-in integration-hook installer + plugin policy

herdr backend swap (#13242) の**配布面** operator runbook。supply-chain の pin posture を
config に固定する手順、session-resume 用 integration hook を **explicit opt-in** で導入する
installer の使い方・安全境界、そして managed lane における community plugin の
allow / deny 境界を durable に固定する。設計根拠は #13175 PoC
(`vibes/docs/logics/herdr-poc-13175-experiment-log.md` E2/E3/E10)、`spec-herdr-native-identity`、
および #14613 / #14614 の隔離 characterization (#14614 j#91226)。

この runbook は **利用手順の正本**であり、CLI `--help` を replay 可能な形にしたもの。実装は
`e_140_adapter_provider/f_130_terminal_runtime_provider` の `herdr_pin_posture` /
`herdr_integration_install` / `absolute_path_rule` + `herdr_plugin_identity` +
`herdr_plugin_policy`(domain)と `*_ops`(application)、CLI は `cli_herdr_distribution`。
plugin policy の domain は「**plugin が何であるか**」(`herdr_plugin_identity`: source
identity と正規化済み observation) と「**何が許可されるか**」(`herdr_plugin_policy`:
review registry と admission 判定) で分かれており、依存は前者→後者の一方向のみ。
絶対 path 判定は**どちらにも属さない** `absolute_path_rule` が持ち、#14258 の
`herdr_probe_redaction` と共有する。owner 別に:

| 節 | 対象 | Redmine |
|---|---|---|
| §1 pin posture | herdr 自身の update egress | #13249 |
| §2 integration hook | operator home への opt-in mutation | #13249 |
| §4 plugin policy | community plugin の enable / install 可否 | #14619 |

## 1. Pin posture — supply-chain 固定 (generate + verify)

herdr の唯一の unattended egress は update 層の 2 スイッチ (`[update]` table、PoC E2/E3):

- `version_check` — herdr.dev への version 取得。**mirror override は存在しない**ので、pin では
  常に `false`。
- `manifest_check` — agent-detection manifest catalog の更新。catalog URL は trusted-env
  `HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL` で operator mirror に差し替え可能。

**★最重要不変条件: 欠落したスイッチは herdr の既定 (= on = egress)。** キーを省いた config は
"pinned" ではなく **UNPINNED** として扱う。

### 採用できる pinned mode は 2 つだけ

- `offline` — 両スイッチ `false`。完全オフライン (PoC E3 実測)。**hook installer が要求する既定
  posture**。
- `pinned_mirror` — `version_check=false` のまま `manifest_check=true`。ただし **absolute
  `https://` の operator mirror URL** を `HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL` で
  export している場合のみ pinned。URL 無し / `http` / 相対値は UNPINNED。

### 手順

```sh
# 生成 (read-only。stdout に出すだけで operator config は書かない)
mozyo-bridge herdr pin-posture                       # offline (既定)
mozyo-bridge herdr pin-posture --mode pinned_mirror \
    --manifest-catalog-url https://mirror.example.org/agent-catalog

# 出力の [update] block を herdr の config (HERDR_CONFIG_PATH / XDG config) に反映する。
# pinned_mirror は加えて HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL を trusted-env に export。

# 検証 (read-only。既存 herdr config が pinned か fail-closed で判定)
mozyo-bridge herdr pin-posture --verify /path/to/herdr/config.toml
mozyo-bridge herdr pin-posture --verify /path/to/config.toml \
    --manifest-catalog-url https://mirror.example.org/agent-catalog   # pinned_mirror 検証時
```

`--verify` は pinned なら exit 0、UNPINNED / malformed なら exit 1 と reason
(`version_check_enabled` / `manifest_check_unpinned` / `mirror_url_insecure` /
`update_table_malformed`) を返す。

## 2. Integration hook — opt-in installer (plan / apply)

herdr の session hook (`~/.claude` / `~/.codex`) は session-resume に必須 (PoC E10) だが、
operator home の変更は **明示 opt-in でのみ**行う。hook 自体は herdr の成果物 (完全 local、
PoC E2) なので、installer は hook を author せず `herdr integration install <agent>` を
**snapshot / diff / rollback transaction で bracket** する。

### 既定は read-only PLAN (zero-mutation)

```sh
# PLAN: 何も変更しない。対象 config dir / 実行される herdr argv / gate 結果を表示
mozyo-bridge herdr integration-install \
    --herdr-config /path/to/herdr/config.toml            # 既定 both agent
mozyo-bridge herdr integration-install --agent claude \
    --herdr-config /path/to/config.toml --json
```

### APPLY は明示 `--apply` (opt-in) のみ

```sh
mozyo-bridge herdr integration-install --apply \
    --herdr-config /path/to/herdr/config.toml
```

`--apply` は各 agent について: pre-snapshot → backup → `herdr integration install` 実行 →
post-snapshot → diff。いずれかの agent が失敗したら**全 agent を rollback** し、復元を
**検証**してから結果を出す (home を発見時の状態へ戻す)。

### `--herdr-config` は「検証対象」であると同時に「実行時の effective config」

`--herdr-config` に渡した file は §1 の pin 検証対象であるだけでなく、apply が
`HERDR_CONFIG_PATH` に **固定**して herdr へ渡す config でもある。pin を証明した file と
herdr が実際に読む file が別々でよいなら、無関係な pinned file を decoy にして
`unpinned_remote` gate を通過できてしまう (PoC 実測: herdr は `HERDR_CONFIG_PATH` で config を
差し替え可能、`logic-herdr-poc-13175-experiment-log`)。したがって:

- apply は検証済み config の realpath を `HERDR_CONFIG_PATH` に設定して herdr を起動する。
- 呼び出し側の環境が **別の** config を `HERDR_CONFIG_PATH` で名指している場合、installer は
  黙って上書きせず `config_pin_mismatch` で **zero-mutation 拒否**する (どちらが正かを installer が
  勝手に決めない)。同一 file を指す symlink 等は realpath 同一性で同一とみなす。
- plan / apply の report は `herdr_config_bound` として bind 先を表示する。

**path を固定しても pin は固定されない。** pin が主張しているのは path ではなく **content の
性質** (`[update]` の switch) なので、同じ path の bytes を検証後に差し替えれば herdr は unpinned
config を読む。したがって installer は posture 検証時に config の **content digest と file identity
(`st_dev`/`st_ino`)** を捕捉し、**各 herdr 呼び出しの直前と直後**に「今も pinned か」「同じ bytes か」
「同じ file か」を再検証する。いずれかが崩れていれば `config_pin_mismatch` で fail-closed
(直前検出なら herdr 未実行、直後検出なら verified rollback して非成功)。

**残存 window (実装保証の限界。何を検出できて何を検出できないかを正確に書く)**:

- 検出**できる**もの: 直前 check の時点、または直後 check の時点で **まだ残っている** drift。
  直後 check で見つかった場合は verified rollback を行うので、hook が残ったまま成功にはならない。
- 検出**できない**もの: herdr 実行中に unpinned へ差し替え、**herdr が読んだ後・installer の直後
  check の前に元へ戻す** transient swap。この場合 **herdr は unpinned config を読み、hook はそのまま
  残り、report は成功になる**。直後 check は「今 drift しているか」しか答えられないため、原理的に
  検出できない。
- これを塞ぐには herdr 側が「本 process が開いている config」を読む必要があり、installer からは
  強制できない。したがって **config file への write 権限を持つ主体を排除することが operator 側の
  責務**であり、本 installer の保証範囲外である。
- この既知の限界は characterization test (`test_KNOWN_LIMIT_transient_config_swap_is_not_detected`)
  で固定してある。期待挙動の pin ではなく **限界の pin** であり、将来 fail するようになったら本節と
  test を同時に更新する。

### plan gate は過去の観測、mutation は現在の行為 (action-time 再検証)

config dir の存在 / directory 性 / home 内 realpath は plan gate で一度確認するが、それは
**「見たときはそうだった」**という過去の陳述にすぎない。plan 後に dir を削除したり home 外への
symlink に差し替えたりできるため、installer は同じ問いを **action time に必ず問い直す**:

- preflight (snapshot / backup) の前
- **各 herdr 呼び出しの直前**
- **rollback の書き込み前** (guard は「書く場所」に置く。drift 後の root へ backup を書き戻すと
  operator の bytes を home 外へ押し出すため)

さらに **config dir を「読む」操作はすべて identity で括る** (`with_identity_bracket`: check →
read → check)。括る対象は preflight の snapshot+backup、**apply 後の read (diff の材料)**、
**rollback の復元検証 read** の 3 つで、いずれも「読んだ bytes が staged object のものである」ことを
両側の check で保証する。前後どちらか片側だけでは足りない:

- 先行 check だけ → 読んだ *結果* が何の object のものか言えない。
- 後続 check だけ → 読み *始め* が正しい object だったと言えない。

これを call site の習慣ではなく **単一 helper** にしてあるのは、習慣が実際に破れたからである
(preflight の read だけ括り、apply 後の read と rollback 検証 read を素通しにしていたため、同一 path
への directory 差し替えが **成功扱い**になり、diff は差し替え先の内容を「herdr の変更」として誤報し、
rollback は **pre-apply と同じ中身に見せかけた別 object** を読んで「復元を証明した」と主張した)。

**identity は path ではなく filesystem object** (`realpath` + `st_dev` + `st_ino`)。同一 path に
別 directory を作り直す replacement は、containment も realpath 一致もすべて通過するが
**installer が snapshot していない別 object** である。これを許すと (1) herdr の write が読んでいない
dir に着地し、(2) rollback が **他人の dir の中身を削除して staged backup を書き込む**破壊的操作に
なる。したがって staged identity と `(dev, ino)` まで一致しない場合も drift とする。snapshot と
backup の**両方が同一 object から得られた**ことも、読み取り後の再検証で確認する。drift は
`config_dir_missing` / `unsafe_config_path` で fail-closed。

**限界**: これらの操作は path 経由であり、開いた descriptor に固定されていない。identity check は
読み取り区間・各 invoke・各 rollback write を **括る**ので、括りをまたいで残る swap は捕捉できるが、
括りの内側で個々の file 操作との間に起きる swap は捕捉できない。

### 「完全に読めた」ことが rollback 開始の前提 (列挙失敗も含む)

rollback は snapshot と backup が対象 dir を **完全に読めた時だけ**開始する。ここでの「完全」は
file の read 成否だけでなく **列挙 (`os.walk` / `lstat`) の成否**を含む。列挙に失敗した subtree は
snapshot からも backup からも同時に消えるため、file 単位の unreadable 検査では「読めなかった」では
なく「存在しない」に化ける。列挙失敗・read 失敗はいずれも `config_dir_unreadable` で mutation 前に
拒否する。**root 自体の不在 / 非 directory も「完全に読めた空 dir」ではなく列挙失敗として扱う**
(subtree で閉じた同型欠陥が root に残っていた)。

同じ理由で、herdr が exit 0 を返しても **apply 後の dir を完全に読み戻せない場合は成功にしない**。
exact diff も最終 home state も観測できていないためで、この場合は backup から verified rollback を
行い `config_dir_unreadable` で closed 扱いとする。

### report contract (consumer が構造から再現できること)

- `applied` — transaction が **実際に mutation へ到達したか**。最初の herdr 呼び出し前に拒否した
  場合は `false`。
- `rolled_back` — **その agent の mutation が revert されたか**。何も書いていない拒否では `false`
  であり、「rollback した」とは表示しない (`nothing was mutated` と表示する)。
- 失敗は必ず **closed reason** として `plans` または `outcomes` に載る。plan 通過後に binary や
  config が drift した場合も、自由文 detail だけでなく `herdr_unresolved` / `config_pin_mismatch`
  を structured reason として投影する。JSON / text いずれの consumer も「mutation の有無」「rollback
  の有無」「失敗理由」を散文の解析なしに再現できる。

### Fail-closed (成功扱いしないケース)

| reason | 意味 |
|---|---|
| `unknown_agent` | claude / codex 以外の agent |
| `config_dir_missing` | 対象 `~/.claude` / `~/.codex` が存在しない (先に作成すること)。plan 後に消えた場合も action-time 再検証で同じ reason |
| `unsafe_config_path` | config dir が symlink / traversal で home 外へ解決、または staged 時点と **filesystem identity** (`realpath`+`dev`+`ino`) が変わった (同一 path への別 directory 差し替えを含む) |
| `unpinned_remote` | herdr posture が pinned でない (§1 を先に満たすこと) |
| `config_pin_mismatch` | 検証した config と herdr が読む effective config が一致しない — 環境 (`HERDR_CONFIG_PATH`) が別 file を名指す、または検証後に同一 path の bytes / file identity が変わった |
| `config_dir_unreadable` | config dir を完全に読めない (file read 失敗 / 列挙失敗 / root 不在・非 directory)、または apply 後に読み戻せない |
| `herdr_unresolved` | trusted-env から herdr binary を解決できない (plan も gate される) |
| `herdr_error` | herdr が非ゼロ終了 / 起動失敗 |
| `rollback_incomplete` | rollback が復元を証明できず residue が残る (home 未復元) |
| `partial_failure` | 別 agent の失敗で rollback された (復元検証済み) |

- `--home` で操作対象 home を明示できる (既定 `$HOME`)。config dir はこの home + 既知 agent 名
  から導出され、任意 dir を指すことはできない。
- herdr binary は trusted-env (`MOZYO_HERDR_BINARY` または trusted PATH) からのみ解決する
  (`resolve_herdr_binary`、#13496)。repo-local config は binary を指せない。
- installer は credential 形の file を snapshot / backup / diff / rollback から除外し、
  operator の秘密を読まない・コピーしない。

## 3. 境界 (§1 / §2 で扱わないこと)

- **実 home への hook apply / live herdr 実行 / network smoke は #13249 の gate 外** (non-goal)。
  本 issue の自動テストは隔離 temp HOME/XDG + fake runner で apply 経路を網羅する。実機
  `herdr integration install` の live smoke は coordinator の post-review acceptance に委ねる。
- 外部 download / tag / release / TestPyPI / PyPI は gate 外。
- coordinator placement の home-config/topology (#14139)、lane-role placement (#13647)、herdr
  binary の取得/同梱 (#14138)、live relayout (`logic-herdr-live-relayout-runbook`) は本配布面の
  対象外。

## 4. Plugin policy — managed lane の allow / deny 境界 (Redmine #14619)

herdr 0.7.5 は community plugin を install / enable できる。#14614 j#91226 の隔離
characterization が測った 2 つの事実が、方針を任意ではなく必須にしている。

- **★plugin の enabled state は user 単位 global** であり、session でも workspace でもない。
  「この workspace だけ有効」は表現できず、隔離は `HOME` / `XDG_CONFIG_HOME` を分けることでのみ
  成立する。1 回の実験的 enable が **全 managed lane に効く**。
- **★plugin は agent の input へ直接書ける**。`herdr-reviewr` は key 一つで review comment を
  workspace の agent へ送る。これは exact-once handoff rail と durable Redmine anchor を迂回する
  **delivery 面の衝突**であり、review verdict や approval の議論以前の問題である。

### 2 軸を独立に判定する (統合しない)

plugin ごとに **独立した 2 つの問い**へ答える。統合すると、どちらかで嘘をつくことになる。

| 軸 | 問い | 決めるもの |
|---|---|---|
| `enable` | managed lane がある状態で **enable** してよいか | capability class (lane authority) |
| `install` | `herdr plugin install` を **実行**してよいか | build provenance (supply chain) |

`herdr-file-viewer` がこの分離を必要にした実例である。capability としては read-only viewer で
authority 面に触れないので **enable は admit** (#14614 が記録した現状維持)。しかしその
`[[build]]` は GitHub release から prebuilt binary を download し、その照合先は
**pin した commit ではなく source が宣言する version**、checksum は **同一 origin** の
`SHA256SUMS` である (#14619 で installed plugin の `scripts/fetch-or-build.sh` を実読)。
つまり install の再実行は unpinned remote execution である。2 軸を畳むと、無害な plugin の
enable を拒否するか、unpinned fetch を追認するかのどちらかになる。**両方報告すれば両方とも
真のままである**。既に install 済みの plugin はこの判定の影響を受けない (本 policy は
**将来の install** を統治し、遡って uninstall を要求しない)。

### capability class (closed vocabulary)

| class | 意味 | enable |
|---|---|---|
| `ux_only` | read-only な UX 面。agent input・lane state・durable record のいずれにも書かない | **admit** |
| `test_oracle` | 参照 schema / 期待 layout の対照物としては有用。lane identity・generation・occupancy・retire の概念を持たない | deny `no_lane_authority` |
| `agent_input_writer` | agent input へ書き、handoff rail と durable anchor を迂回する | deny `agent_input_writer` |
| `unknown` | この identity を review していない。**fail-closed の既定** | deny `unreviewed_pin` |

### ★allow は subdir + commit pin + manifest digest、deny は repository scope (非対称は意図的)

- **allow は exact `(kind, owner, repo, subdir, commit)` と、現在inventoryが返した実行可能
  manifest面のdigestに固定する。** 「この code は安全」は読んだbytesとreviewしたmanifest面についての
  言明である。repository 単位の allow は upstream の**将来の全 commit**へ
  黙って延長され、同じrepositoryの別pluginまで許可する。Herdr 0.8 inventoryの
  `source.subdir` と `resolved_commit` を別々に読み、root・sibling・child-prefix・別commitは
  すべて `unknown` に落としてdenyする。install後にmanifestのcommandやhookが変わった場合も、
  source metadataだけが以前のpinを名乗り続けてもdigest不一致でdenyする。
- GitHubのowner / repoはASCII lowercaseへ正規化して比較する。GitHub上で同一repositoryを指す
  大文字小文字違いを、別のallowやrepository-wide denyとして扱わない。
- **deny は `(kind, owner, repo)` に固定する (commit なし)。** `herdr-reviewr` が不可なのは
  *project* が agent input へ書くからで、新しい commit がそれをやめるわけではない。commit 単位の
  deny は「誰も見ていない commit を install する」ことで迂回でき (それも `unknown` で deny される
  ので実害はないが)、**理由が真でなくなる**。repository scope なら、抽象化された commit しか
  記録に無い characterization からでも正しく分類できる。
- 安全性を担保する不変条件は構築時に検査する: **repository scope の entry は deny class しか
  持てない**。したがってこの形が allow を広げることは構造的にありえない。

### 現在の分類 (正本は `herdr_plugin_policy.REVIEWED_PLUGINS`)

| plugin | ref | class | build provenance | enable | install |
|---|---|---|---|---|---|
| `mozyo.unit-board` | `hollySizzle/mozyo_bridge/herdr-plugins/mozyo-unit-board` + commit pin `aa39b4c9…` | `ux_only` | `no_build` | admit | admit |
| `smarzban/herdr-file-viewer` | commit pin `96fcc0a2…` | `ux_only` | `remote_artifact_same_origin_checksum` | admit | deny `unpinned_remote_build` |
| `yuk1ty/herdr-spreader` | repository | `test_oracle` | `unreviewed_build_provenance` | deny `no_lane_authority` | deny `unreviewed_build` |
| `persiyanov/herdr-reviewr` | repository | `agent_input_writer` | `remote_artifact_same_origin_checksum` | deny `agent_input_writer` | deny `unpinned_remote_build` |

`unreviewed_build_provenance` は **tri-state** である。「build が無い」でも「build がある」でも
なく「**review が何も確立していない**」を意味するので、manifest drift の比較対象にしてはならない
(畳むと、比較していない事柄について drift を主張することになる)。

### 手順 (すべて read-only。apply mode は存在しない)

```sh
# STATUS: installed plugin を全件分類する。既定は trusted-env の herdr binary へ
# `plugin list --json` (read-only) を 1 回だけ発行する。
mozyo-bridge herdr plugin-policy
mozyo-bridge herdr plugin-policy --json
mozyo-bridge herdr plugin-policy --from-json ./captured-plugin-list.json

# PLAN ENABLE: enable してよいかだけ答える。enable はしない。
mozyo-bridge herdr plugin-policy --plan-enable herdr-file-viewer

# PLAN INSTALL: install を実行してよいかだけ答える。install はしない。
mozyo-bridge herdr plugin-policy \
    --plan-install hollySizzle/mozyo_bridge/herdr-plugins/mozyo-unit-board \
    --ref aa39b4c9e9c3f43bf054649916a4803bb9a75c7f
mozyo-bridge herdr plugin-policy --plan-install smarzban/herdr-file-viewer \
    --ref 96fcc0a2bdd2727ec88c38f8c8806f97b7ca0ea0
```

exit code: STATUS は「全 record が読めた」かつ「inadmissible な plugin が enable されていない」
とき 0。PLAN は当該判定が admit のとき 0。**deny されているが enable されていない plugin は
breach ではない** (policy が機能している状態)。enable 済み かつ inadmissible の連言だけが
`BREACH` であり、operator の action を要する。

### fail-closed reason (closed vocabulary)

| reason | 意味 |
|---|---|
| `unpinned_source` | exact な upstream subdir + commit identity が無い (`plugin link` の local、非 `github` kind、欠落 / 不正な commit、malformed な owner/repo/subdir)。**abbreviated commit は identity ではない**ので pin としては拒否する |
| `unreviewed_pin` | source は pin されているが、**その identity** を review した記録が無い |
| `identity_mismatch` | pin は review 済みだが、local manifest が別の `plugin_id` を名乗る |
| `manifest_drift` | 現在の正規化済みmanifest capability digestがreview記録と食い違う。比較面はminimum Herdr version、platform、build、startup、action、event、pane、link handler。command文字列自体はreportへ出さない。**commit pinだけではinstall後にdisk上のmanifestが差し替わった場合を固定できない** |
| `manifest_unavailable` | Herdr が manifest warning を返し、現在実行される面をclean reloadから完全に確立できない。plugin identityは読めるため、そのplugin固有のdenyとして保持する |
| `agent_input_writer` | agent input へ書く |
| `no_lane_authority` | test oracle として認識済み。live lane に対する authority を持たない |
| `unpinned_remote_build` | `[[build]]` が remote artifact を download し、その整合性証明が同一 origin からしか得られない |
| `unreviewed_build` | `[[build]]` が何を実行するかを review が確立していない |
| `malformed_record` | plugin record として読めない。未知のtop-level fieldまたは正規化不能なcapability値もこの分類になる。**読み飛ばさず**報告し、report を fail させる |
| `inventory_incomplete` | inventory に `malformed_record` が 1 件以上ある。**enable plan は残りから答えを作らず**、plugin固有verdictより先にこのreasonで拒否する |
| `ambiguous_target` | 同一 `plugin_id` に複数の installed plugin が該当する。先頭一致で黙って解決しない |
| `target_not_installed` | 該当 `plugin_id` の plugin が無い |
| `invalid_target_id` | operand が bounded identifier でない。**生値は echo しない**（closed token で表示） |

### ★malformed commit・subdir は pin を壊すが repository deny は壊さない

`(kind, owner, repo)` が valid なら、commitまたはsubdirが不正でも
**repository-scoped reference は保持する**。
これが無いと、abbreviated commit を与えられた `reviewr` が `agent_input_writer` ではなく
`unknown` に落ち、**deny は残るのに class も reason も真でなくなる** — repository-scoped deny を
置いた目的そのものが失われる。deny の理由が間違っている deny は、自分を説明しなくなった deny
である。逆向きも同じ規則で閉じる: repository identity を保持しても、**pinned allow は
unpinned identity では成立しない**（file-viewer に abbreviated commit を与えると
`unpinned_source` で deny）。

subdirは相対segment列として閉じ、空segment、`.`、`..`、絶対path、backslash、control、
非文字列、長すぎるsegment、深すぎる列を拒否する。拒否時にroot pluginのallowへfallbackしては
ならない。commitとsubdirを落とした`repo_key`だけがrepository-wide denyを解決する。

reference の構築は `source_ref_from_parts` **1 つ**で、observed inventory と operator が
名指した候補の両方が通る。以前は候補側にだけ「pinned 失敗 → repository へ fallback」があり、
observed 側に無かった。**同じ概念を 2 箇所で書くと、片方だけが古くなる。**

`--plan-enable` / `--plan-install` はflagの**存在**でmodeを決める。空文字を「flagなし」と同じに
扱ってstatusへfallbackしてはならず、invalid operandとしてnon-zero・inventory未取得で拒否する。

### enable plan は「答えが一意に定まらない」全経路で fail-closed

`plan_enable` は plugin 自身の verdict を見る**前**に次を拒否する。

1. **inventory が完全に読めていない** — 読めなかった record が、まさに問い合わせ対象の plugin か、
   その id を名乗る第二の record かもしれない。どちらも知り得ないので、残りから作った答えは
   信用できない。**問い合わせた id では絞り込めない**（id 自体が読めなかった側にあり得る）。
2. **同一 id に複数該当** — herdr の id は operator が `herdr plugin enable` に打つ値そのもの。
   先頭一致で答えると、**実際に enable される plugin とは別の plugin について**答えることになる。
3. **該当なし** — identity を確立する対象が無い。

`PolicyStatus.ok` と `plan_enable` は「完全に読めたか」の述語 (`fully_read`) を**共有する**。
以前は同じ論拠が `ok` の docstring にだけ書かれ、`plan_enable` に適用されていなかった —
**報告側が fail-closed で、実際に operator の行為を gate する admission 側が fail-open** という
向きの誤りだった。

### 保証と、その保証の範囲外

- **read-only の担保は構造的である。** 本 surface が組み立てる argv は定数
  `INVENTORY_ARGV = ("plugin","list","--json")` **ただ一つ**で、install / enable / disable /
  uninstall へ至る code path は存在しない。全 mode について「subprocess を呼ばない、または
  この argv でだけ呼ぶ」ことを test が pin している。
- **値非表示は「closed representation」で担保する。closed とは *核が所有する値* であることで、
  形を狭めたことではない。** `PluginObservation` の各 field は投影済み vocabulary token /
  検証済み reference / strict bool のいずれかで、例外は `plugin_id` だけ — これは operator が
  打つ operand なので隠せず、代わりに**長さで bound する**。`__post_init__` は
  **validator table と dataclass の field 集合を突き合わせ、validator の無い field があれば
  構築自体を拒否する**（手書き check の書き漏らしで担保が崩れないようにするため）。

- **untrusted input は 3 面ある。** 1 面だけ塞いでも足りない。
  1. **inventory** → closed representation（上記）。
  2. **自分で書いていない text**（subprocess の stderr、parse message）→ `redact_probe_paths`。
  3. **plan operand**（`--plan-enable <id>` / `--plan-install <spec>`）→ bounded identifier の
     ときだけ echo し、それ以外は closed token。「operator が打った値だから」は echo してよい
     理由にならない — 本 report は durable record へ貼られる artifact なので、operand は
     path を公開し得るし、**改行を含む operand は record 中に行を偽造できる**（`BREACH:` 行の
     偽造を実測）。これは値非表示より重い、記録の完全性の問題である。

  **訂正の履歴（silent edit にしない）。** 本節の「値非表示」記述は 2 度誤っていた。
  1. 初版「record に path を格納する field が無いので formatter が redact を忘れても漏れない」
     → 誤り。正しくは「**path を意図した** field が無い」だけで、`version` / `source_kind` は
     任意 text を保持し、`version` 経由では **`ok=true` の clean な report が private path を
     運んだ**（review j#92053 F1）。
  2. 第二版「`version` は version 形の bounded token だけ echo するので closed」→ これも誤り。
     **字母を狭めても第三者の値であることは変わらず**、alphanumeric な marker がそのまま report に
     出た（review j#92092 F3）。`version` は**削除**した。identity は commit pin であり、
     version は識別に不要である。同 round の F1 は、その第二版を実装した
     `__post_init__` が 8 field 中 3 field しか検査していなかったこと
     （docstring は全 field を主張）を突いている。
  3. regression の形も 2 度変えた。field 名の列挙 → **payload の全 string leaf へ marker を
     1 つずつ注入する oracle**（列挙が漏れたので列挙をやめた）→ さらに **operand 面へ oracle を
     拡張**（operand は inventory を通らないので、前者の oracle では原理的に見えなかった）。
  4. **factory を閉じても value object は閉じていなかった**（review j#92141 F1）。
     `EnablePlan` / `InstallPlan` / `MalformedEntry` / `PolicyDecision` / `PluginVerdict` は
     直接構築と `dataclasses.replace` で path と偽造行を通した。**描画される当のもの**が
     開いていた。

### ★2 層で守る: source(各 DTO) と sink(唯一の出口)

**4 round すべてで「閉じた面の隣」が開いていた**。1 面ずつ塞ぐ方式そのものを変える。

1. **source 層** — renderable な text を持つ全 DTO が constructor で自分の値を閉じる。
   共有述語は 1 つ (`require_renderable_field`: path-free / control-free / bounded)。
   - **我々が書いた text**（decision detail / review anchor / rationale）は **拒否**する。
     違反はこちらのバグなので、黙って書き換えず落とすのが正しい。
   - **第三者由来の text**（parse message / subprocess stderr）は **sanitize** する
     (`sanitize_renderable`: redact → control 文字を空白へ平坦化 → 長さ制限)。
     こちらは敵対的入力が想定内なので、値を返せなければ report が作れない。
   - 両者とも「安全でない値を保持した record が存在しない」点は同じ。違うのは
     **違反したとき誰の責任か**だけである。
2. **sink 層** — CLI の**唯一の出口** `_emit` が、**組み上がった artifact** を検査する。
   面に結びつけないのが要点で、**将来 field / DTO / formatter が増えても、誰も気づかなくても
   効く**。検査は「絶対 path を含まないか」「(text では改行以外の) control 文字を含まないか」。
   違反したら **何も出力せず non-zero で終える** — private path や偽造行を含む report を
   durable record へ貼るくらいなら、出力しない方がよい。

### ★path 判定は repository 単一 authority を使う

絶対 path の判定規則は **`domain/absolute_path_rule` の 1 箇所**にあり、`herdr_plugin_identity`
(#14619) と `herdr_probe_redaction` (#14258) の**両方がそこへ収束する**。この module は
**どちらの consumer にも属さない**。以前は汎用の #14258 redaction が後発で用途特化の
#14619 identity module を import しており、**依存が逆向き**だった（review j#92285 F3、
coordinator 裁定 j#92243 の「one *neutral* authority」に反する）。**root pattern と relative-continuation pattern だけでなく、
positive-proof predicate (`keeps_absolute_root`) も共有する** — 最初は regex object しか
共有せず、同じ規則が 2 実装のまま残っていた（review j#92241 F3）。しかも当時の test は
**regex の identity しか検査しておらず、「単一 authority」という主張を falsify できなかった**。
現在は関数 identity と、両 consumer が corpus 全件で一致することの両方を pin している。以前は本 boundary が独自に「`/` + segment + `/`」という第三の
規則を書いており、**`/etc` / `/` / `/秘密` / `/tmp-☃/secret` をすべて安全と読んだ**
（review j#92194 F1）。同じ問いに対する硬化済み実装が repo 内に既にあったのに再利用せず、
**最も新しい面に最も弱い写しを置いていた**。

規則は「root 出現はすべて path。**語の途中の `/` だけが path でない証明**」という反転形で、
**行単位で評価する**。patterns だけ写して評価構造（行分割）を写さなかったとき、`$` が
改行直前にも一致するため `line\n/etc/passwd` が「前の行の末尾文字」を継続証明として
安全と読んだ — これも実測して直した。

帰結として **prose 中の裸の ` / ` も path として発火する**。文言側を直した（`HOME / XDG` →
`HOME or XDG`、`split / ratio` → `split, ratio`）。**境界に prose を合わせるのが正しい向き**で、
prose に合わせて境界を緩めるのではない。

### ★authority は「一箇所」であるだけでなく「書き換え不能」であること

policy authority（`REVIEWED_PLUGINS` / `VERDICTLESS_ENABLE_STATES` / observation の
validator table）は **read-only mapping** で公開する。

**集約と不変性は別の性質であり、集約は不変性を要求する。** planner と constructor に
同じ表を読ませたのは正しい修正だが、その表が可変だと**両者が一緒に動く**ので、closed
vocabulary の外の state が両側で同時に正当化される（review j#92330 実測: 禁止 reason を
表へ注入すると、本来拒否される verdictless plan が受理された）。

**より重い同型が `REVIEWED_PLUGINS` にあった**（自己申告）。任意 pin の allow entry を
注入すると、その plugin が `ux_only` / enable admitted になる。**構築時に重複・矛盾を
拒否しても、構築後に書き換えられるなら意味がない。**

派生 view は**表から導出する**。`VERDICTLESS_ENABLE_REASONS` は import 時 snapshot だった
ため、表が動くと黙って乖離した。

### ★field の妥当性は組合せの整合性ではない

per-field の型・語彙検査を全 field に課しても、**field 間の関係**は閉じない
（review j#92194 F2）。実測で: `source_kind=unrecognized` の observation が reviewed pin を
併せ持って `ux_only` / admitted に、`EnablePlan(found=False, verdict=None, decision=admit)` が
`ok=true` に、`class=unknown` × `enable=admit` の verdict が enabled plugin で `breach=False` に
なった。

- `PluginObservation`: `ref` を持つなら `source_kind` は `github` でなければならない。
- **verdictless な enable 拒否は、reason だけでなく *その reason が意味する state* を閉じる。**
  `VERDICTLESS_ENABLE_STATES` が `reason → (id を echo するか, 何か見つかったか)` を持ち、
  **planner はここから state を導き、constructor は同じ表と照合する**。reason の集合だけを
  閉じていたとき、`target_not_installed` なのに `found=True`、`invalid_target_id` なのに
  `found=True` という **planner が到達できない state** を public plan が報告できた
  （review j#92285 F1）。
- **`InstallPlan` の `spec` と `ref` は presence が連動する。** factory は同じ owner/repo
  検証から両方を作るので、片側だけ存在する state は到達不能である。それを許すと、
  「target は `<withheld>` なのに source と class は開示する」record が作れる（同 F2）。
- **computed result はすべて「policy から再計算した値と一致すること」で閉じる。**
  `PluginVerdict` は `resolve_reference` / `decide_enable` / `decide_install` の結果と、
  `InstallPlan` は `plan_install(ref)` と一致すること。`EnablePlan` は verdict を持つなら
  その verdict の `enable` / `plugin_id` / `found` と一致し、verdict を持たないなら
  **verdict 無しで到達しうる deny reason の閉じた集合**（`inventory_incomplete` /
  `ambiguous_target` / `target_not_installed` / `invalid_target_id`）に限る。

  **同じ問いには同じ強さの解を当てる。** 最初は `PluginVerdict` にだけ再計算を入れ、
  plan には「admit にだけ最小前提を課す」という弱い形を選んだ。結果、policy が
  `unpinned_remote_build` で deny する reference に invented admit を渡すと
  **supply-chain preflight が自分の答えを反転できた**（review j#92241 F1）。
  さらに deny 経路を無制約にしたため、**enable-admitted な verdict を抱えたまま deny する
  plan** が作れ、JSON と text が同じ問いに反対の答えを返した（同 F2）。
  「deny には *前提* が要らない」は正しいが、「deny の *整合性* を検査しなくてよい」ではない。

- **1 つの問いには 1 つの authority。** enable plan の text は `plan.decision` だけが答え、
  verdict block は enable 行を伏せた context として出す。両方が enable を語ると、
  どちらが答えか決まらない。

### regression の形

1. **path detector は仕様由来の corpus** で pin する（single-component / root / non-ASCII /
   mixed / doubled / trailing / drive / UNC / labelled と、誤検出してはならない自前 identity）。
   production の正規表現から書き起こさない — 以前は test 側 oracle が実装と同形だったので
   **2 層が同時に盲目**だった。
2. **E1–E10 を名前付き matrix**として直接 pin する。dataclass の自動列挙をやめた理由は、
   `InventoryReadError` が dataclass でないため **自分で見つけた E9 が回帰から落ちていた**
   から。coverage assertion は双方向。
3. **偽造行の probe には、hostile 入力にしか現れない marker を使う**。`BREACH:` で探すと、
   本 report が正当に書く breach 行を偽造と誤判定する（実際に誤判定した）。
- **authority は core 所有のまま。** `FORBIDDEN_PLUGIN_AUTHORITIES` は既存の
  `FORBIDDEN_PROVIDER_AUTHORITIES` (#12035) と `CORE_OWNED_AUTHORITIES` (#12155) を
  **そのまま再利用**した上で lane 固有 (`delivery_authority` / `durable_anchor_authority` /
  `lane_identity` / `retire_authority`) を足す。plugin へ Redmine / handoff / review /
  retire authority を移す経路は無い。
- **範囲外**: operator 環境への自動 install / enable、credential 表示、production publish、
  #13249 の pending Review の変更・免除、Herdr 0.7.5 transport migration (#14617 / #14618 の
  native agent transport とは混ぜない)。plugin の runtime 挙動は source audit と manifest
  観測に基づく (#14614 の限界をそのまま引き継ぐ。install / build / 実行はしていない)。
