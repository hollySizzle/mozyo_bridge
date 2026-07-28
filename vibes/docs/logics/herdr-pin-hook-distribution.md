# herdr 配布面: pin posture + opt-in integration-hook installer + plugin policy

herdr backend swap (#13242) の**配布面** operator runbook。supply-chain の pin posture を
config に固定する手順、session-resume 用 integration hook を **explicit opt-in** で導入する
installer の使い方・安全境界、そして managed lane における community plugin の
allow / deny 境界を durable に固定する。設計根拠は #13175 PoC
(`vibes/docs/logics/herdr-poc-13175-experiment-log.md` E2/E3/E10)、`spec-herdr-native-identity`、
および #14613 / #14614 の隔離 characterization (#14614 j#91226)。

この runbook は **利用手順の正本**であり、CLI `--help` を replay 可能な形にしたもの。実装は
`e_140_adapter_provider/f_130_terminal_runtime_provider` の `herdr_pin_posture` /
`herdr_integration_install` / `herdr_plugin_policy`(domain)と `*_ops`(application)、CLI は
`cli_herdr_distribution`。owner 別に:

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

### ★allow は commit pin、deny は repository scope (非対称は意図的)

- **allow は exact `(kind, owner, repo, commit)` に固定する。** 「この code は安全」は読んだ
  bytes についての言明である。repository 単位の allow は upstream の**将来の全 commit**へ
  黙って延長され、それこそが本 policy の塞ぐべき穴になる。review 済み以外の commit は
  `unknown` に落ちて deny される。
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
| `unpinned_source` | exact な upstream commit が無い (`plugin link` の local、非 `github` kind、欠落 / 不正な commit)。**abbreviated commit は identity ではない**ので拒否する |
| `unreviewed_pin` | source は pin されているが、**その identity** を review した記録が無い |
| `identity_mismatch` | pin は review 済みだが、local manifest が別の `plugin_id` を名乗る |
| `manifest_drift` | local manifest の `[[build]]` の有無が review 記録と食い違う。**commit pin が固定するのは upstream が publish した内容であって、install 後に operator の plugin directory に置かれた bytes ではない** |
| `agent_input_writer` | agent input へ書く |
| `no_lane_authority` | test oracle として認識済み。live lane に対する authority を持たない |
| `unpinned_remote_build` | `[[build]]` が remote artifact を download し、その整合性証明が同一 origin からしか得られない |
| `unreviewed_build` | `[[build]]` が何を実行するかを review が確立していない |
| `malformed_record` | plugin record として読めない。**読み飛ばさず**報告し、report を fail させる |

### 保証と、その保証の範囲外

- **read-only の担保は構造的である。** 本 surface が組み立てる argv は定数
  `INVENTORY_ARGV = ("plugin","list","--json")` **ただ一つ**で、install / enable / disable /
  uninstall へ至る code path は存在しない。全 mode について「subprocess を呼ばない、または
  この argv でだけ呼ぶ」ことを test が pin している。
- **値非表示も構造的である。** herdr の payload は絶対 path を 3 つ (`manifest_path` /
  `plugin_root` / `source.managed_path`) 運ぶが、正規化後の `PluginObservation` には
  それを**格納する field が無い**。したがって formatter が redact を忘れても漏れない。
  自分で書いていない text (subprocess の stderr、malformed record の parse message) だけは
  `redact_probe_paths` を通す。
- **authority は core 所有のまま。** `FORBIDDEN_PLUGIN_AUTHORITIES` は既存の
  `FORBIDDEN_PROVIDER_AUTHORITIES` (#12035) と `CORE_OWNED_AUTHORITIES` (#12155) を
  **そのまま再利用**した上で lane 固有 (`delivery_authority` / `durable_anchor_authority` /
  `lane_identity` / `retire_authority`) を足す。plugin へ Redmine / handoff / review /
  retire authority を移す経路は無い。
- **範囲外**: operator 環境への自動 install / enable、credential 表示、production publish、
  #13249 の pending Review の変更・免除、Herdr 0.7.5 transport migration (#14617 / #14618 の
  native agent transport とは混ぜない)。plugin の runtime 挙動は source audit と manifest
  観測に基づく (#14614 の限界をそのまま引き継ぐ。install / build / 実行はしていない)。
