# work-unit granularity config

Redmine #13002 / US #12638。

owner decision: 今後の標準作業単位は `1US=1作業単位` とする。sublane dispatch も coordinator が 1 UserStory を target-lane Codex gateway へ渡し、gateway が同一 US を same-lane Claude worker へ渡して、US 配下の Task / Test / Bug を一気通貫で実装・検証・記録する。leaf issue 単位の dispatch は handoff / callback / review / close の固定費が実装量に勝ちやすいため、標準ではなく例外へ降格する。本書はその **configurable work-unit granularity** の config knob schema と、緩めてよい運用しきい値・緩めてはいけない固定 invariant の境界を定義する repo-local の一次正本である。

## 目的

- work-unit granularity を closed enum (`epic` | `feature` | `user_story` | `leaf_issue`) の project 設定として固定する。
- default を `user_story` に固定する (owner decision)。
- `leaf_issue` の適用条件を central preset の `us_level_audit.task_level例外` と同一集合に保ち、新しい例外語彙を作らない。
- `epic` / `feature` を implementation dispatch unit にする場合は explicit owner/operator decision (durable anchor) を必須にする。

## 設定の置き場所と層

repo-local の `.mozyo-bridge/config.yaml` に `work_unit:` top-level section として置く。`delegation:` / `sublane_integration:` と同じ層 (review 可能な desired declaration) であり、runtime truth ではない。

```yaml
work_unit:
  version: 1
  granularity: user_story   # epic | feature | user_story | leaf_issue
```

- config 不在 / `work_unit:` section 不在は **behavior-preserving default** (`user_story`)。
- schema は closed / fail-closed。unknown key、unsupported version、enum 外の `granularity` は reject し、silent に default へ倒さない (typo が dispatch 単位を静かに変えない)。
- 実装 source of truth: `src/mozyo_bridge/e_110_execution_platform/f_140_delegated_coordinator_nested_handoff/domain/work_unit_granularity.py` (`WorkUnitGranularityConfig` / `decide_work_unit_dispatch`)、composition は `repo_local_config.py` の `work_unit` field。

## granularity 語彙と dispatch 契約

| granularity | dispatch | 条件 |
| --- | --- | --- |
| `user_story` | 許可 (default / recommended) | 標準の governed work unit。coordinator -> target-lane Codex gateway -> same-lane Claude worker の route はそのまま、渡す durable anchor / scope を US 単位にする。worker は US 配下の Task / Test / Bug を US scope 内で処理し、各 child の implementation_done / task_close 相当を durable record に残す |
| `leaf_issue` | 許可 (例外) | central preset `us_level_audit.task_level例外` (guardrail / workflow / preset / router / skill / scaffold rule 変更、release / packaging / CI、credential / 外部 service、destructive / migration、architecture / 互換性、実装者の判断迷い、owner / 監査者の明示要求) に該当する場合の単位。該当条件は coordinator の dispatch decision journal に記録する |
| `epic` / `feature` | **explicit decision なしでは blocked** | scope が巨大化しやすい構造 node。explicit owner/operator decision を durable anchor (journal id) として dispatch に添えた場合のみ許可。config 値だけでは解除できない (per-dispatch anchor 必須) |

runtime の fail-closed gate は `mozyo-bridge sublane create/start` の plan 面 (`plan_sublane_create`) と actuation 面 (`--execute` / `--dry-run`) の両方に載る。granularity の解決順は CLI `--work-unit` flag > repo-local config `work_unit.granularity` > default `user_story`。`epic` / `feature` の decision anchor は CLI `--work-unit-decision-journal` で渡す。present-but-broken config は fail-closed し、default で silent に dispatch しない。

blocked 時の diagnostic は fixed token (`work_unit_explicit_decision_required` 等) で、plan / journal にそのまま残せる durable-record safe な語彙とする。

## 緩めてよい運用しきい値と固定 invariant

| 項目 | 区分 | default | 正本 |
| --- | --- | --- | --- |
| 標準 dispatch granularity (`work_unit.granularity`) | project knob | `user_story` | 本書 |
| per-dispatch の granularity override (`--work-unit`) | operator knob | config 値 | 本書 |
| `epic` / `feature` の explicit decision 必須 | **固定 invariant** | — | 本書 (config key なし) |
| durable anchor (Redmine journal) を work record とする | **固定 invariant** | — | preset / skill workflow |
| review_request / review / owner close approval / Close Gate | **固定 invariant** | — | central preset `## Completion` / `### Gate Schema` |
| US-level audit model (US 配下 child の implementation_done / task_close 記録) | **固定 invariant** | — | central preset `### US-Level Audit Model` |
| sublane route (`coordinator -> target-lane Codex gateway -> same-lane Claude worker`) | **固定 invariant** | — | `logic-coordinator-sublane-development-flow` |
| hidden worker / invisible subagent の sublane 扱い禁止 | **固定 invariant** | — | `rule-project-agent-workflow` / spine |

どの granularity を選んでも次は変わらない。これらは config schema に対応する key を持たず、設定値で無効化できない。

- **gate は granularity で消えない**: `user_story` 単位にしても、US 配下 child ごとの implementation_done / task_close 相当の durable record、US の review_request (US-level audit request)、owner close approval、Close Gate はすべて central preset のまま要求される。granularity は「何を 1 回の dispatch で渡すか」だけを選ぶ。
- **`epic` / `feature` は config だけでは開かない**: `work_unit.granularity: epic` と書いても、per-dispatch の explicit decision anchor が無ければ fail-closed する。恒久設定で oversized dispatch を常時解禁する経路は存在しない。
- **`leaf_issue` は task_level例外の別名ではなく適用面**: 例外条件の正本は central preset であり、本書は条件集合を複製しない。

## fail-closed / fallback matrix

| condition | result |
| --- | --- |
| config 不在 / `work_unit:` section 不在 | `user_story` default を適用 |
| unknown key / unsupported `version` / enum 外 `granularity` | config application を fail-closed (`RepoLocalConfigError`)。silent default 化しない |
| `--work-unit epic\|feature` かつ `--work-unit-decision-journal` 不在 | plan / actuation とも `dispatch_blocked` (`work_unit_explicit_decision_required`) |
| decision anchor が空白のみ | 不在として扱う (bare flag では解除できない) |
| present-but-broken config で `sublane create/start` | 実行前に fail-closed (exit 非 0)。default granularity で dispatch しない |

## Public / Private boundary

OSS default に入れてよいもの: generic な enum 語彙 / default / 固定 invariant との対照表 / fail-closed matrix。入れてはいけないもの: private project の lane naming、operator 固有 profile、private path / Redmine project 名 (`rule-public-private-boundary`)。

## 参照正本

- `vibes/docs/logics/coordinator-sublane-development-flow.md` (sublane route / 帯域 / admission / dispatch decision の正本)
- `vibes/docs/specs/delegation-policy-project-config.md` (config knob と固定 invariant を分離する同型の先行 spec)
- `.mozyo-bridge/rules/presets/redmine-governed/agent-workflow.md` (`### US-Level Audit Model` / `us_level_audit.task_level例外` / `## Completion`)
- `vibes/docs/rules/agent-workflow.md` (`## Redmine Hierarchy Semantics` / `## Audit Handoff`)
- `vibes/docs/logics/unit-presentation-state-db.md` (repo-local `.mozyo-bridge/config.yaml` desired config の schema 契約 / fail-closed pattern)

## 検証

- `mozyo-bridge docs validate --repo .`
- `mozyo-bridge docs validate --check-file-coverage --repo .`
- `mozyo-bridge docs generate-file-conventions --check --repo .`
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`
- `mozyo-bridge docs resolve vibes/docs/specs/work-unit-granularity-config.md --repo . --format text`
