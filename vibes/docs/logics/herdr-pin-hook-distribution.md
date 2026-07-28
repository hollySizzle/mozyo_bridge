# herdr 配布面: pin posture + opt-in integration-hook installer (Redmine #13249)

herdr backend swap (#13242) の**配布面** operator runbook。supply-chain の pin posture を
config に固定する手順と、session-resume 用 integration hook を **explicit opt-in** で導入する
installer の使い方・安全境界を durable に固定する。設計根拠は #13175 PoC
(`vibes/docs/logics/herdr-poc-13175-experiment-log.md` E2/E3/E10) と `spec-herdr-native-identity`。

この runbook は **利用手順の正本**であり、CLI `--help` を replay 可能な形にしたもの。実装は
`e_140_adapter_provider/f_130_terminal_runtime_provider` の `herdr_pin_posture` /
`herdr_integration_install`(domain)と `*_ops`(application)、CLI は `cli_herdr_distribution`。

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

## 3. 境界 (本 issue で扱わないこと)

- **実 home への hook apply / live herdr 実行 / network smoke は #13249 の gate 外** (non-goal)。
  本 issue の自動テストは隔離 temp HOME/XDG + fake runner で apply 経路を網羅する。実機
  `herdr integration install` の live smoke は coordinator の post-review acceptance に委ねる。
- 外部 download / tag / release / TestPyPI / PyPI は gate 外。
- coordinator placement の home-config/topology (#14139)、lane-role placement (#13647)、herdr
  binary の取得/同梱 (#14138)、live relayout (`logic-herdr-live-relayout-runbook`) は本配布面の
  対象外。
