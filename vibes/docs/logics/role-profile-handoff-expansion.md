# role profile handoff 展開 (送信側解決) ロジック

Redmine #12396 / US #12388 / Feature #12386 (`Delegated Coordinator / Nested Handoff`)。

`handoff send` / init 相当の送信側が固定 role profile template を解決し、structured fields とともに受信側へ展開する runtime contract の設計正本。template 本文の正本は `vibes/docs/specs/delegated-coordinator-role-profile.md` (US #12387) であり、本書はその template を **runtime で解決し handoff に載せる** ロジックのみを定義する。

## 背景と目的

- agent に cwd / directory 探索で custom instruction を自力発見させると、worktree / tool / project によって読まれ方がぶれる。
- 送信側で role profile を確定し、受信 agent が template path を推測しなくても role contract を読める contract にする。
- role profile は受信側の **custom instruction** であり、handoff の **structured fields** とは分離する (US #12387 設計方針)。本ロジックは routing landing marker を一切変更しない。

## 解決ロジック (送信側)

実装: `src/mozyo_bridge/domain/role_profile.py` (resolver) + `role_profile_config.py` (config schema) + packaged `role_profile_templates.yaml` (template 本文の runtime 正本; Redmine #12952)。

- template registry: 4 role token (`coordinator` / `delegated_coordinator` / `implementation_gateway` / `implementation_worker`) の本文は、wheel に同梱される config artifact `role_profile_templates.yaml` を runtime 正本として持つ。本文の human-facing 正本は引き続き US #12387 spec であり、packaged YAML はその machine-readable 写しである。
- config load: `role_profile.py` が import 時に一度だけ `importlib.resources` で packaged YAML を読み (cwd / worktree の path 探索はしない = package-anchored resource)、`RoleProfileConfig.from_record` で schema 検証してから registry を構成する。runtime で markdown を parse せず、path 推測もしない (self-contained / fail-closed)。malformed / missing artifact は import 時に `RoleProfileConfigError` で loud に fail-closed し、handoff 途中で partial contract を送らない。
- config schema (`role_profile_config.py`): 固定 4 role 語彙を code invariant として持ち、config は「その 4 token を過不足なく定義する」ことを要求する。unknown role token / role 欠落 / 空 template / declared `placeholders` と template の `<...>` token 不一致 / 不明 key / 空 `version`・`source` はすべて `RoleProfileConfigError` で fail-closed する。`version` は `ROLE_PROFILE_VERSION`、`source` は `ROLE_PROFILE_SOURCE` の durable pointer を運ぶ。
- `resolve_role_profile(role, fields)`: template を取得し、`<...>` placeholder を structured field 値で置換する。pure / deterministic。
- `RoleProfileResolution`: 解決結果。structured pointer field (`role_profile` / `profile_source` / `profile_version` / `unresolved_placeholders`) と `resolved_text` を持つ。
  - `profile_source`: template 本文の正本への repo-relative pointer (spec path)。
  - `profile_version`: builtin template set の安定識別子。template 本文を変更したら bump する。
- placeholder 値は `--profile-field KEY=VALUE` (反復可) で渡す。`durable_anchor` は anchor から自動補完する。

## fail-closed と明示 fallback

- **template missing は fail-closed**: 未知 role token は `RoleProfileError`。CLI では argparse `choices` で弾き、orchestrator では `blocked` / `invalid_args` を emit して停止する。pane send は一切行わない。
- **不正な `--profile-field` は fail-closed**: `=` を含まない、または key が空の pair は `RoleProfileError`。
- **明示 fallback**: `--role-profile` を渡さない場合は profile 展開なし (`role_profile=None` を record)。path 推測による暗黙解決は行わない。
- **placeholder 部分未充足は明示 fallback**: 値が無い placeholder は literal `<name>` のまま残し、`unresolved_placeholders` に列挙する。黙って欠落させない。この fallback は send-time auto-fill 源を持たない placeholder (例: `lane` / `parent_project`) に適用する。auto-fill 源を持つ `redmine_project` は下記 send-time field resolver が別扱いする。

## send-time field resolver と `redmine_project` 自動解決 (Redmine #13477)

pure template resolver (`role_profile.py`) は IO-free を保つ (cwd / worktree の path 探索も defaults file 読取もしない)。send-time に runtime context を要する field auto-fill は application-layer の send-time field resolver `f_130_handoff_routing/application/role_profile_field_resolution.py` (`resolve_handoff_profile_fields`) が担い、`handoff send` 配線 (`orchestrate_handoff`) から呼ばれる。この resolver が唯一 filesystem を読む seam である (`core.state.workspace_defaults.resolve_default_project` 経由)。

- **`durable_anchor`**: anchor pointer から自動補完する (Redmine #12388、上記のとおり)。
- **`redmine_project`** (`coordinator` / `delegated_coordinator` template のみが持つ placeholder): 次の優先順位で解決する。
  1. **valid な非空 explicit 値 > verified default**: `--profile-field redmine_project=<id>` に strip 後非空の値があれば、それを優先し default へ fallback しない。
  2. **explicit 省略時は verified workspace-local default で補完**: `redmine_project` が未指定なら、repo root の固定 defaults path (`<repo>/.mozyo-bridge/project-defaults.yaml`、legacy `workspace-defaults.yaml` は fallback) の **verified** default project identifier を補完する。読取は fixed path であり cwd / worktree の探索ではない。
  3. **fail-closed** (`RoleProfileError` → 配線側で `blocked` / `invalid_args`、pane send せず): explicit の空/空白値 (有効な identifier ではない)、および explicit 省略時に default が missing / unverified / conflict (new+legacy 両在) のとき。missing/unverified を fact として黙って送らない (workspace default-project resolution 契約と整合。`skills/mozyo-bridge-agent/references/workflow.md` `### Default project 解決`)。
  4. **placeholder を持たない role は default を読まない**: `implementation_gateway` / `implementation_worker` は `redmine_project` を持たないため defaults 読取をせず、この gate も適用しない (missing default で send が壊れない)。

runtime 実装詳細 (関数分割・error message 文言) は doc に複製しない。正本は上記 module と unit/integration test。

## ADR context の注入 (Redmine #15722)

ADR-0011 のトレードオフ 3 が記録するとおり、「ADR は全階層で参照必須」には各層の実行文脈へ ADR を注入・解決する機構が無かった。本節はその最小機構を定義する。role profile を載せる handoff は、同じ展開経路で repo-local ADR 集合への解決可能な pointer も運ぶ。

実装: `f_130_handoff_routing/domain/adr_context.py` (pure pointer 型) + `f_130_handoff_routing/application/adr_context_resolution.py` (repo 読取の唯一の seam) + `handoff_envelope_planner` の配線 (`resolve_adr_context` port)。

- **注入点は role profile 展開 1 箇所**: L2 delegated coordinator / L3 gateway / L3 worker への dispatch はいずれも `handoff send --role-profile <role>` を経由する (`sublane dispatch-worker` / gateway dispatch / `delegation launch adopt` / grandchild dispatch)。したがって planner の role profile 展開に additive で載せることで 3 階層すべてが同じ pointer を受け取る。`sublane create` 自体は role profile を展開しないため、注入点は dispatch 側である。
- **pointer 主義**: ADR 本文は複製しない。運ぶのは ADR index (`vibes/docs/adr/README.md`) の canonical path、各 ADR の `adr-NNNN` id / canonical path / resolvable paths、そして宣言された status のみ。resolvable paths は workflow contract ref と同じく canonical 形と monorepo nested 形の両方を持つ (#12700 j#66929)。
- **status を昇格させない**: `normalize_adr_status` は closed vocabulary (`active` / `proposed` / `superseded` / `unknown`) へ正規化し、**binding は `active` のみ**。`proposed` は `proposed` として、未知・欠落・読取不能な status は `unknown` として提示され、いずれも「拘束する規約」としては描画されない。payload 側の `binding` flag は派生値で、`adr_context_from_payload` は `status` から再計算する (payload 詐称で昇格できない)。
- **解決は fixed path・explicit fallback**: `<repo_root>/vibes/docs/adr/README.md` を fixed path で読む (cwd / worktree 探索なし)。ADR ディレクトリまたは index が無い repo では `None` を返し、handoff は #15722 以前と同一の payload を送る。すなわち ADR 運用を持たない adopter repo は影響を受けない。
- **後方互換 (additive)**: `RoleProfileResolution.adr_context` は default `None` の追加 field。`to_structured_dict()` は **pointer が解決された場合のみ** `adr_context` key を持つ — 未解決時は key 自体を省略し、payload は #15722 以前と byte-identical に保たれる (review j#108679 finding_nullkeybreaksnoadrcompat)。`profile_source` / `profile_version` / `unresolved_placeholders` の既存契約と template 本文は不変である。`profile_version` は template 本文の pointer であり、send 時に解決される ADR context はこれに含めない (`record_contract_text()` が別 heading で追記する)。
- **展開先**: pane body には単一行の pointer clause (index / active 件数 / non-active 件数 / read obligation)、durable delivery record には resolved contract に続く `# ADR context` block、structured outcome には `role_profile.adr_context` payload。

## 受信側への展開と durable record

実装: `src/mozyo_bridge/domain/handoff.py` (`build_notification_body` / `DeliveryOutcome` / `make_outcome` / `build_delivery_record`)、wiring は `orchestrate_handoff` (`src/mozyo_bridge/application/commands.py`)。

- **pane notification body**: 単一 `tmux send-keys -l` で配送され landing-marker gate が行を grep するため、body は単一行を保つ。role profile は compact な単一行 pointer clause (role token / source path / version / 未充足 field) のみを append する。複数行の解決済み contract は body に入れない。
- **durable delivery record / structured outcome**: 完全に解決した role contract 本文は durable delivery record (`build_delivery_record`) に fenced block として載せる。structured pointer field (`role_profile` / `profile_source` / `profile_version` / `unresolved_placeholders`) は `DeliveryOutcome.role_profile` として JSON outcome にも常に載る。受信 agent は durable anchor を読めば role contract を path 推測なしで読める。
- **durable anchor の優位は崩さない**: pane notification は pointer に過ぎず、判断の正本は Redmine issue / journal に残る durable record である。

## public / private 境界

- structured pointer field は free-text を含まず常に durable-record safe。
- 解決済み contract 本文は operator 供給の `--profile-field` 値を埋め込み得るため、印字 (pasteable) record にのみ載せ、opt-in auto-persist body からは省く (`--record-command` と同じ posture)。`--profile-field` 値は repo-relative / redacted に保つこと。

## 安全 invariant (固定)

- role profile は routing landing marker を変更しない (custom instruction と structured fields の分離)。
- template 解決は cwd / worktree の path 探索をせず、wheel-packaged config artifact を schema 検証したうえで registry に閉じる (fail-closed)。config 正本の外出し先は packaged resource に限り、send 時に外部 path を推測しない。
- profile_version は解決済み contract 本文への忠実な pointer である (template 本文変更時に `role_profile_templates.yaml` の `version` を bump)。

## 参照正本

- `vibes/docs/specs/delegated-coordinator-role-profile.md` (role 語彙 / 責務境界 / 固定 template 本文の正本)
- `vibes/docs/logics/coordinator-sublane-development-flow.md` (coordinator / sublane 実行 spine)
- `vibes/docs/rules/public-private-boundary.md`
- `vibes/docs/rules/agent-workflow.md`
- `skills/mozyo-bridge-agent/references/workflow.md`

## 検証

- `python3 -m unittest tests.unit.e_110_execution_platform.f_130_handoff_routing.test_handoff_role_profile`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_130_handoff_routing.test_role_profile_config`
- `python3 -m unittest tests.unit.e_110_execution_platform.f_130_handoff_routing.test_adr_context`
- `python3 -m unittest tests.integration.e_110_execution_platform.f_130_handoff_routing.test_handoff_adr_context_injection`
- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/role-profile-handoff-expansion.md --repo . --format text`
