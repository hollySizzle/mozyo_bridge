# 委譲コーディネータ role profile (採用記録 / runtime 同期 anchor)

Redmine #12393 / US #12387 / Feature #12386 (`Delegated Coordinator / Nested Handoff`)。`delegated_coordinator` を含む固定 role profile 語彙と責務境界を定義した repo-local の一次正本だったが、#13029 により portable 本文は配布側へ upstream された。本書は採用記録、既存 spine との対応、packaged runtime 設定の同期 anchor という repo 固有差分を保持する。

## 正本 (pointer)

role 語彙 (最小 4 role: `coordinator` / `delegated_coordinator` / `implementation_gateway` / `implementation_worker`)、権限対応表、委譲の責務境界 (parent issue close / owner approval route / callback route / downstream dispatch 境界)、固定 role profile template (4 role の custom instruction 本文)、安全 invariant の正本は、#13029 により配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `## 委譲コーディネータ role model (delegated coordinator)` にある。本書は再掲しない (#13029 で pointer 化)。孫 dispatch の context 保護判断も同節 `### 孫 dispatch / context 保護` を読む。

`coordinator_assistant` は skill `references/workflow.md` の **`coordinator_assistant` の安全使用境界**が定義する provider-neutral な**文書上の補助 actor**であり、この固定 4 role profile 語彙にはまだ含まれない。packaged `role_profile_templates.yaml`、handoff `role_profile`、`provider_binding` role、launch/runtime identity の追加は未実装であり、本書やskillの用語追加をruntime配線完了の証拠にしてはならない。runtime対応は別issueで4 role contractへの影響を設計して実装する。

## scope の分担 (repo issue 履歴)

- handoff で role profile template を解決し受信 prompt に展開する処理は #12388、孫 (grandchild) dispatch の context preservation policy は #12389、delegation policy の project 設定 knob は #12390、子 coordinator / 孫 lane の window / cockpit 表示方針は #12391、delegated coordinator の decision / callback / correction record は #12392 で扱った。本書は解決ロジック・設定 knob・表示方針・record schema を持たない。
- 設計方針 (Feature #12386): 無限階層を目指さず監査可能な shallow delegation を扱う。孫 dispatch の主目的は coordinator の context window 圧迫回避。project ごとの policy knob は許可するが、owner approval / parent close / durable callback の safety invariant は固定する。

## 既存 spine との対応 (repo 固有)

単一 project 内の coordinator / sublane 責務分担、帯域 / admission / pipeline fill、US close 権限、retirement の正本は `vibes/docs/logics/coordinator-sublane-development-flow.md` (spine) である。配布側 role model の spine actor 対応: `coordinator` = 管制塔 Codex (main coordinator lane Codex)、`delegated_coordinator` = 子 project の管制塔 Codex (単一 project model に存在しない新 role)、`implementation_gateway` = target-lane Codex、`implementation_worker` = sublane Claude。

## Packaged runtime 設定の同期 anchor (repo 固有)

固定 role profile template 本文の runtime source of truth は packaged `role_profile_templates.yaml` (`src/mozyo_bridge/e_110_execution_platform/f_130_handoff_routing/domain/`; #12952) であり、その `source:` field は本書を指す。#13029 で template 本文の doc 正本は配布側 skill 節 `### 固定 role profile template` へ移ったため、同期は本書を経由する pointer chain になる: packaged YAML の template 本文は配布側 skill 節と一致させ、いずれかを変更する場合は `version` を bump して両者を同じ commit で揃える。`source:` field の付け替え (本書 → skill 節) は runtime / packaging 変更を伴うため本書の scope 外とし、実施する場合は別 issue で扱う。

## 参照正本

- `skills/mozyo-bridge-agent/references/workflow.md` (`## 委譲コーディネータ role model (delegated coordinator)`; 配布正本)
- `vibes/docs/logics/coordinator-sublane-development-flow.md` (coordinator / sublane 実行 spine)
- `vibes/docs/rules/agent-workflow.md`
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md`
- `.mozyo-bridge/rules/llm_rule_authoring.md`

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/specs/delegated-coordinator-role-profile.md --repo . --format text`
