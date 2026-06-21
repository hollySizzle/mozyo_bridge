# Module-Health Gate (oversized-module visibility / PyLint-equivalent foundation)

Redmine #12321 (parent #11825 Plugin/Adapter 境界設計, fixed_version v0.10.7)。mozyo_bridge の module health を主観ではなく CLI / CI / review で測れる状態にするための設計正本。特に `presentation_grouping.py` / `cockpit_ui.py` のような oversized file を隠さず管理し、新規肥大化を止める。

## 採用判断: PyLint ではなく equivalent native gate

#12321 dispatch (j#62637) は "PyLint **or equivalent** module-health gate" を明示委任した。本実装は重量級 runtime 依存 (pylint) を足さず、stdlib-only の native gate を採用する。

- 根拠 1: mozyo-bridge の runtime 依存は `build` / `PyYAML` / (3.10 のみ) `tomli` に限定されており、lint 専用の重い依存を runtime に持ち込まない方針。
- 根拠 2: 既存 oversized file の管理には **per-file の reason / owner_issue / 解消予定 Version** を記録する必要がある。PyLint の `# pylint: disable=too-many-lines` はこの governance metadata を担えない。native allowlist (`module_health.yaml`) はそれを構造化して持てる。
- 根拠 3: gate semantics (新規 oversized = fail / 既存 oversized の成長 = fail) を完全に制御できる。

owner が literal pylint を要求する場合は follow-up issue で差し替え可能 (gate の外形契約は維持できる)。

## 閾値

```yaml
max_module_lines: 1000   # PyLint の max-module-lines default と一致
```

1000 は PyLint の `too-many-lines` default であり、#12321 が名指しする oversized file (`presentation_grouping.py` 1228 / `cockpit_ui.py` 1041) を捕捉する。値は `module_health.yaml` の `max_module_lines` で上書きできる。

## scope

```yaml
include:
  - src/mozyo_bridge   # 初期 scope: runtime package のみ
```

`tests/` は初期 gate から除外する (dispatch の "pragmatic / focused" 制約)。tests は自然増大しやすく、別 baseline 方針が要る。将来拡張 (tests への適用 / per-function complexity gate / symbol-count gate) は follow-up とし、本 gate の外形を壊さず `include` 拡張で行う。

## gate 契約

正本実装は `src/mozyo_bridge/domain/module_health.py` (pure core) と CLI handler `src/mozyo_bridge/application/commands_module_health.py` / registrar `cli_module_health.py`。

### 計測値 (`mozyo-bridge health report`)

module ごとに以下を出す。

- `lines`: physical line count (`str.splitlines()`; trailing newline で水増ししない)。PyLint `too-many-lines` と同じ数え方。
- `complexity`: **近似** signal。decision-point node (if/for/while/except/with/ternary/assert/comprehension と boolean 追加 operand) + def/class 定義数の総和。McCabe の正確値ではなく health の粗い指標。
- `top_level_symbols`: module scope で見える名前数 (top-level def/async def/class + module-level assignment target)。

### gate (`mozyo-bridge health check`)

```yaml
fatal (exit 1):
  new_oversized:            threshold 超過かつ allowlist 未登録 → fail (新規 oversized file)
  growth:                   allowlist 登録済だが current lines > baseline → fail (既存 oversized の成長)
  dangling_allowlist:       allowlist の path が disk 上に存在しない → fail (stale entry)
  baseline_below_threshold: allowlist baseline が threshold 以下 → fail (不要な entry / 設定ミス)
warning (exit 0, stderr):
  resolved:                 allowlist 登録済だが現在 threshold 以下 → entry 削除を促す
  shrunk:                   allowlist 登録済だが current lines < baseline → baseline 引き下げを促す
```

`baseline` は recorded line count であり、**成長のみ fail**。改善 (縮小) は warning にとどめ CI を壊さない。新規ファイルが threshold を超えると、allowlist に reason/owner_issue/resolution_version を書いて意図的に登録するまで fail する = 新規肥大化の歯止め。

config が不正 (読めない / 型違い / required field 欠落) なら fail closed (`ModuleHealthError`, exit 2)。config 不在時は default で動く (allowlist 空 = oversized は全て new_oversized 扱い = fail closed)。

## allowlist (`module_health.yaml`)

repo root の `module_health.yaml` が正本。各 entry は `path` / `lines` (baseline) / `reason` / `owner_issue` / `resolution_version` を必須とする。`resolution_version: TBD` は分割 target version 未定を意味する。分割の version 割当は roadmap 判断 (coordinator/owner; #11825 epic 配下) であり、本 gate の責務は新規成長の停止であって分割計画ではない。

baseline 更新フロー: 既存 oversized file を意図的に増やす必要があるとき、`module_health.yaml` の該当 `lines` を新しい値へ上げ、journal に理由を残す。silent な肥大化はできない。

## CI 接続

`.github/workflows/test.yml` の `Module-health gate` step が `mozyo-bridge health check` を install 後に実行する。gate fail で CI job が fail する。

## 非目標

- 既存 oversized files の大規模分割 (本 gate は計測と歯止めのみ)。
- 全 PyLint rule への一括準拠。
- release / publish / tag。
