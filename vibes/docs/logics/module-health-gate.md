# Module-Health Gate (oversized-module visibility / PyLint-equivalent foundation)

Redmine #12321 (parent #11825 Plugin/Adapter 境界設計, historical fixed_version `v0.10.7 module health visibility / PyLint gate foundation`)。mozyo_bridge の module health を主観ではなく CLI / CI / review で測れる状態にするための設計正本。特に `presentation_grouping.py` / `cockpit_ui.py` のような oversized file を隠さず管理し、新規肥大化を止める。

#12321 は visibility / reporting foundation を据えた。**#12324 (historical Version #241 `v0.10.10 module health ratchet / regression policy`)** はこの gate を report-only / allowlist-heavy な段階から、通常開発で再肥大化を止める regression policy へ引き上げる。本 issue で追加した契約は後段の「期限付き allowlist」「ratchet 判断記録」の節に記す。番号付き Version 名は歴史的 anchor であり、package release version の正本ではない。

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
  expired:                  oversized のまま `expires` 期限超過 → fail (#12324 期限切れ例外)
warning (exit 0, stderr):
  resolved:                 allowlist 登録済だが現在 threshold 以下 → entry 削除を促す
  shrunk:                   allowlist 登録済だが current lines < baseline → baseline 引き下げを促す
  expiring_soon:            oversized のまま 期限まで `expiry_warning_days` 以内 → 期限前の予告
```

`today` は `evaluate(..., today=None)` で注入でき (default `date.today()`)、deadline 判定を test で決定論にする。CI の実 `health check` が live な期限切れ enforcement を担う。`expired` / `expiring_soon` は **module が依然 oversized のときのみ** 発火する (threshold 以下に縮んだ entry は `resolved` 扱いで、期限超過でも fail させない)。

`baseline` は recorded line count であり、**成長のみ fail**。改善 (縮小) は warning にとどめ CI を壊さない。新規ファイルが threshold を超えると、allowlist に reason/owner_issue/resolution_version を書いて意図的に登録するまで fail する = 新規肥大化の歯止め。

config が不正 (読めない / 型違い / required field 欠落) なら fail closed (`ModuleHealthError`, exit 2)。config 不在時は default で動く (allowlist 空 = oversized は全て new_oversized 扱い = fail closed)。

## allowlist (`module_health.yaml`)

repo root の `module_health.yaml` が正本。各 entry は `path` / `lines` (baseline) / `reason` / `owner_issue` / `resolution_version` を必須とする。`resolution_version` は既存 schema 名として残すが、意味は Redmine roadmap / resolution group であり、package release version でも active lane-set authority でもない。各 oversized module の解消 (分割) は coordinator 決定 (#12321 j#62668) で記録された対応 group 側で行う:

- `presentation_grouping.py` → **v0.10.8** (Version #239, split US #12322)
- `cockpit_ui.py` → **v0.10.9** (Version #240, split US #12323)
- 残り 6 module (commands / cockpit_layout / doctor / release / handoff / scaffold.rules) → **v0.10.10** (Version #241 module-health ratchet, US #12324)

本 gate の責務は新規成長の停止であり、分割は上記 historical resolution group で実施する。`resolution_version` に `TBD` 等の未定値は使わない (受入条件「解消予定 group を記録する」を満たすため)。将来 schema を変えるなら `resolution_group` などへ移行するが、既存 config 互換性に影響するため別 issue で扱う。

baseline 更新フロー: 既存 oversized file を意図的に増やす必要があるとき、`module_health.yaml` の該当 `lines` を新しい値へ上げ、journal に理由を残す。silent な肥大化はできない。

## 期限付き allowlist (#12324 ratchet)

#12324 で allowlist entry は `path` / `lines` / `reason` / `owner_issue` / `resolution_version` に加え **`expires` (ISO `YYYY-MM-DD`) を必須** とする。これにより残す例外は必ず「Redmine 所有 issue (`owner_issue`) と 期限 (`expires`)」を持つ (#12324 受入)。

- `expires` は例外が失効する日付であり、解消予定 Version の due date に合わせる。残り 6 module の `expires` は Version #241 (`v0.10.10`) due の **2027-11-30** に設定する。
- module が `expires` を過ぎても依然 oversized なら `expired` で **fail** = 期限切れ例外を CI / verification で検出する (#12324 受入)。
- 期限まで `expiry_warning_days` (config key, default 30) 以内になると `expiring_soon` の非 fatal warning を出す。これが **Review Request / Version close readiness の module-health impact surface** であり、失効後ではなく失効前に近づく期限が見える。
- `expires` は quoted string でも YAML date literal でも受理する (PyYAML は unquoted な `2027-11-30` を `datetime.date` に解釈する)。不正値は fail closed (`ModuleHealthError`)。

baseline (`lines`) を意図的に上げる既存フローに加え、`expires` を後ろ倒しする場合も journal に理由を残す。silent な期限延長はしない。

### allowlist 縮小について

残り 6 module (commands / cockpit_layout / doctor / release / handoff / scaffold.rules) は現状 baseline = 実 line count であり、分割なしには allowlist を縮小できない。**完全 teardown は本 issue 非目標** のため、各 module の実分割は Version #241 配下の per-module follow-up issue に委ねる。本 issue は (1) 期限の付与 (2) 期限切れ検出 (3) 新規成長停止の継続、で再肥大化を止める regression policy を確立する。

## ratchet 判断記録 (#12324)

受入条件「threshold を段階的に下げるか増加停止 gate を強化する」「too-many-branches / too-many-statements / complexity gate の採否を判断する」への記録された決定:

- **threshold 引き下げ: 採用しない (本 slice)**。`max_module_lines` を 1000 未満へ下げると、現状 threshold 直下の多数 module が一斉に new_oversized 化し、本 issue 非目標の「style-only lint の大量導入 / 全 legacy module の解体」に流れる。代わりに **期限付き allowlist + 期限切れ fail** で増加停止 gate を強化する方を採る。threshold 引き下げは module 数が十分減ってからの follow-up とする。
- **too-many-branches / too-many-statements / per-function complexity gate: 採用しない (本 slice)**。これらは per-function 解析で大量の既存違反を生み、allowlist 運用が file 単位の現行モデルから乖離する。module 単位の近似 `complexity` signal は `health report` で **report-only** のまま維持し、gate 化はしない。literal pylint への差し替えと同様、外形契約を壊さず follow-up issue で導入可能。
- **Review Request / Version close readiness 接続: 採用**。`expiring_soon` warning と `health check` の oversized summary を Review Request / Version close readiness に含める。Version close 前に期限が近い例外が surface する。

## CI 接続

`.github/workflows/test.yml` の `Module-health gate` step が `mozyo-bridge health check` を install 後に実行する。gate fail で CI job が fail する。`expired` も fatal なので、期限切れ例外は CI で検出される。Review Request / Version close readiness では `mozyo-bridge health check` の結果 (oversized 件数と `expiring_soon` warning) を引用する。

## 非目標

- 既存 oversized files の大規模分割 (本 gate は計測と歯止めのみ)。
- 全 PyLint rule への一括準拠。
- release / publish / tag。
