# Workflow Docs Source-Of-Truth Boundary

Redmine #13025。mozyo-bridge の workflow docs を「配布して他 project でも成立させる基幹運用」と「この repo 固有の運用」に分離するための判断規約。mozyo_bridge dogfood でうまくいった workflow が repo-local に閉じ込められ、他 project で 3-window workflow や Redmine 操作が再現しない状態を防ぐ。portable な判断ルール自体は配布側 `skills/mozyo-bridge-agent/references/workflow.md` の `## Workflow Docs Source-Of-Truth Boundary` 節が正本であり、本 doc はその mozyo_bridge repo への適用 (surface 対応表 / upstream 手順 / repo 固有分類) を定義する。同じ判断材料を両方に複製しない。

## Surface 対応表

mozyo_bridge repo における workflow docs の layer と正本の対応。

| layer | surface | 正本性 | 配布経路 |
| --- | --- | --- | --- |
| governance contract | central preset `agent-workflow.md` (packaged: `src/mozyo_bridge/scaffold/presets/**`; repo-local store: `.mozyo-bridge/rules/presets/**`) | packaged preset 側が正本。repo-local store は配布物 | `mozyo-bridge rules install` / `scaffold apply` |
| portable operating procedure | `skills/mozyo-bridge-agent/references/**` (canonical) | canonical が正本 | plugin marketplace / Codex `$skill-installer` |
| portable operating procedure (mirror) | `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/**`, `.claude/skills/mozyo-bridge-agent/**` | mirror / adapter。手編集禁止 (`logic-skill-distribution`) | `scripts/sync_plugin_skill.sh` |
| repo-local rule / logic / spec | `vibes/docs/**` | repo-local 正本 (この repo 限定) | 配布しない |
| entrypoint router | `AGENTS.md` / `CLAUDE.md` | thin router。scaffold 管理 (drift 追跡) | scaffold |

## 配置判断ルール (mozyo_bridge 適用)

新しい workflow 規約を書くとき、次の順で判断する。

1. **他の adopting project でも必要か?** yes なら配布側。gate 語彙 / 役割 / 編集権限 / close 条件は central preset、日常運用手順 (Redmine 基本操作、issue authoring / Epic / Feature / US / leaf / Version の一般的な使い方、handoff / callback / review / owner close の標準運用、3-window workflow 標準モデル、hidden worker 禁止・visible lane / durable anchor 前提) は skill references。
2. **この repo の実装・歴史・例外か?** yes なら repo-local `vibes/docs/**`。例: `src/mozyo_bridge/**` の architecture / OOP-first / source layout (`logic-object-oriented-architecture-policy`)、bounded context map / module_health / commands.py residual などの repo 固有技術負債、この repo 固有の Version 名・issue 履歴・例外、workspace preference (応答言語など)。
3. **operator-private か?** private path / cockpit 構成 / lane 数 profile / private runbook は layer を問わず配布 body に入れない (`rule-public-private-boundary`)。distributed / repo-local 軸 (本 doc) と public / private 軸 (`rule-public-private-boundary`) は直交する 2 軸であり、両方を満たす必要がある。

dogfood で得られた基幹 workflow が repo-local doc に先に書かれた場合、それは「配布 gap」であり恒久配置ではない。portable 部分を skill references / preset へ upstream し、repo-local 側は採用宣言と repo 固有差分だけを残す。

## 正本 / pointer / repo-local override の書き分け

- **正本**: 1 つの判断材料は 1 箇所にだけ書く。配布側に正本がある規約を repo-local に再説明しない。
- **pointer**: repo-local doc / router は「どの正本を読むか」と repo 固有の適用範囲だけを書く。pointer は `正本 path + 節名` の形で書き、本文を要約複製しない (要約が古びると二重管理に戻る)。
- **repo-local override**: 採用可否・patterns 拡張/縮小など preset が明示的に許す knob だけを Project-Local Additions または catalog 登録 doc で宣言する。preset が許さない override (例: autonomous lane への配布 surface 追加) は書かない。

## `skills/.../references/workflow.md` と `vibes/docs/rules/agent-workflow.md` の役割

- `skills/mozyo-bridge-agent/references/workflow.md`: **配布される portable 運用手順の正本**。adopting project がそのまま従う手順 (ticket entrypoint / handoff / coordinator-sublane / stall / retirement / fill loop 等) を持つ。mozyo_bridge 固有の path・採用事実・技術負債は書かない。
- `vibes/docs/rules/agent-workflow.md`: **mozyo_bridge repo 固有の運用規約の正本**。役割分担の採用宣言、gated-surface path 拡張、repo 固有 architecture boundary、dogfood lane profile への pointer などを持つ。portable 手順の本文を再掲しない。
- central preset: gate / journal / 編集権限 / completion の governance contract。skill references はこれを複製せず参照する。

二重読みの解消は「読む順序」で定義する: gate / 権限の判断は preset → 手順の実行は skill references → repo 固有の適用は `vibes/docs/rules/agent-workflow.md`。同じ質問に 2 つの doc が別の答えを持つ状態を作らない。

## Upstream 手順

repo-local に書かれた portable 規約を配布側へ移す場合:

1. skill references (または preset) に正本を書く。preset 変更は packaged template (`src/mozyo_bridge/scaffold/presets/**`) 側を正本として編集し、`mozyo-bridge rules install` / `scaffold apply --backup` で再配布する (`.mozyo-bridge/rules/**` の直接編集は drift)。
2. repo-local 側を pointer + repo 固有差分に縮退させる。削除ではなく縮退 (採用宣言と repo 固有例外は残る)。
3. skill canonical を変更したら `scripts/sync_plugin_skill.sh` で mirror を同期し、catalog (`.mozyo-bridge/docs/catalog.yaml`) の document entry / related refs を更新する。
4. 検証: `mozyo-bridge docs validate --repo .` / `--check-file-coverage` / `docs generate-file-conventions --check --repo .` / `docs audit-impact --all-changed --check-generated --repo .` / plugin mirror drift test。

## #13024 との関係

Redmine issue 記載粒度 (Epic / Feature / US / leaf) と Version 運用の guideline **内容** は #13024 が扱い、本 doc の判断ルール 1 により配布側 `skills/mozyo-bridge-agent/references/redmine-issue-authoring.md` に置く。本 doc は配置境界のみを扱い、記載粒度の判断基準を持たない (依存: #13024 → 本 doc の配置ルール)。

## 非目標

- central preset / packaged template 本文の変更 (release / packaging を伴うため別 issue)。
- 既存 repo-local docs の一括再配置。境界に反する既存 doc は発見時に follow-up issue で個別に upstream する。
- catalog schema / resolver tooling の変更。
