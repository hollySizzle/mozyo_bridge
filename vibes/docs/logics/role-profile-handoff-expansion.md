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
- **placeholder 部分未充足は明示 fallback**: 値が無い placeholder は literal `<name>` のまま残し、`unresolved_placeholders` に列挙する。黙って欠落させない。

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
- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/logics/role-profile-handoff-expansion.md --repo . --format text`
