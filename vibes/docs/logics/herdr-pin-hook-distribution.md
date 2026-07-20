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

### Fail-closed (成功扱いしないケース)

| reason | 意味 |
|---|---|
| `unknown_agent` | claude / codex 以外の agent |
| `config_dir_missing` | 対象 `~/.claude` / `~/.codex` が存在しない (先に作成すること) |
| `unsafe_config_path` | config dir が symlink / traversal で home 外へ解決 |
| `unpinned_remote` | herdr posture が pinned でない (§1 を先に満たすこと) |
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
