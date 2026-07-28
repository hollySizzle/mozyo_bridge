# Legacy Mirror 同期障害の状態機械 / テスト分割境界 Characterization

Redmine #14660 (親 #14592)。**read-only characterization であり、実装指示書ではない。**
本 doc は #14592 の production 実装に入る前に、現行の観測可能な挙動・error
precedence・cleanup / retry 契約を source と tests から導出して固定する。

対象 (すべて `origin/main-next@fef86cac20bd2a28c1870c5f036a317dd9c2909c` 時点の実測):

| path | lines |
| --- | --- |
| `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/domain/legacy_mirror_contract.py` | 335 |
| `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/application/legacy_mirror_sync.py` | 899 |
| `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/application/owned_descriptors.py` | 725 |
| `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/application/platform_capabilities.py` | 181 |
| `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py` | 3,865 |
| `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_contract.py` | 232 |

baseline: `python3 -m unittest tests.unit.e_130_governance_distribution.f_150_skill_plugin_distribution.test_legacy_project_skill_mirror` → `Ran 127 tests` / `OK` / rc=0。

本 doc の記述は 3 種に分かれ、混ぜない。

- **[実測]** — source / tests / 実行結果から直接読み取った事実。
- **[導出]** — 実測から機械的に導いた分類・対応。導出器を明示する。
- **[未確認]** — 本 Task の read-only scope では確定できず、実装 Task へ持ち越す判断。

---

## 1. 状態機械 (実測)

判断は 2 つの独立した機械に分かれている。片方は tree を観測して violation を返す
**audit 機械**、もう片方は unwind 中の teardown 失敗を retention する
**retention 機械**である。両者は現状 1 つの test module に同居しているが、
依存を持たない。

### 1.1 Audit 機械 (rule A–F / P / W)

rule token の正本は `legacy_mirror_contract.py`。

| rule | 意味 | write-blocking |
| --- | --- | --- |
| `A` `RULE_SOURCE_TOPOLOGY` | canonical path の各 component が実 directory | yes |
| `B` `RULE_SOURCE_ENTRIES` | pinned canonical name が regular file | yes |
| `C` `RULE_DEST_TOPOLOGY` | mirror path の各 component が実 directory | yes |
| `D` `RULE_DEST_ENTRY_SET` | mirror 直下の entry がすべて pinned name | yes |
| `E` `RULE_DEST_ENTRY_TYPES` | pinned mirror entry が regular file | yes |
| `F` `RULE_CONTENT_PARITY` | pinned mirror entry が canonical と byte 一致 | **no** |
| `P` `RULE_PLATFORM` | host が no-follow / dir-fd primitive を提供する | yes |
| `W` `RULE_WRITE` | 書き込み自体が完了した (tree の形の主張ではない) | yes |

`WRITE_BLOCKING_RULES = {A, B, C, D, E, P, W}`。**`F` の除外は意図的**である —
content drift を直すことが sync の存在理由であり、blocker にすると command が
自分の仕事を拒否する (`MirrorAudit.blocks_write` docstring)。

#### 評価順序と suppression [実測]

`LegacyProjectSkillMirrorSync.audit()`:

1. `missing_platform_capabilities()` が非空 → `P` 単独で即 return。**A–F は評価しない**。
2. source を `_bound(SOURCE_RELATIVE, A)` で開く。
   - `source_missing` → `A` / `PATH_COMPONENT_MISSING`。
   - fd 取得成功 → `_audit_source_entries` が `B` を評価。
3. mirror を `_bound(MIRROR_RELATIVE, C)` で開く。
   - fd 取得成功 → `_audit_dest_entries` が `D` と `E` を評価。
4. `F` は次のいずれかで **skip** され `skipped_rules` に載る:
   `source` violation が非空 / `dest_missing` / `source_fd is None` / `mirror_fd is None`。
   - 理由 [実測、docstring j#90397 R5-F3]: 壊れた source に対する parity は
     sync が解決できない drift を報告し、composite が「refuse する resync」を勧める。

`dest_missing` は violation ではない (sync が作る) が、`MirrorAudit.ok` は
`not violations and not dest_missing` なので **check は 1 を返す**。

#### Error precedence — recovery 導出 [実測]

`MirrorAudit.recovery_actions()` は **audit 全体から導出**する。class ごとに
emit すると R5-F3 の「restore the source と rerun the sync を同時に印字する」
が再発する。順序 (最初の分岐が短絡):

1. `has_rule(P)` → `(RECOVERY_PLATFORM_UNSUPPORTED,)` を返して**終了**。
   kind ではなく **rule** で keying している (kind keying は他の rule-P violation を
   resync 行へ落とした)。
2. `kinds & UNREADABLE_KINDS` → `restore_access`
3. `source_invalid` (`A` または `B`) → `restore_source`
4. `has_rule(C)` → `restore_mirror_path`
5. `has_rule(D)` → `disposition_unpinned`
6. `has_rule(E)` → `replace_entry`
7. `has_rule(W)` → `write_failed`
8. `CLEANUP_FAILED in kinds` → `clear_residue`
9. `resync` は `(has_rule(F) or dest_missing)` **かつ 1–8 が 1 つも出ていない**ときだけ。

つまり `resync` は「上流に何も詰まっていない」ことの連言であり、単独 flag ではない。

#### 観測失敗は typed violation であって例外ではない [実測]

`PATH_UNREADABLE` / `SOURCE_UNREADABLE` / `ENTRY_UNREADABLE` は `UNREADABLE_KINDS`
を成し、`restore_access` を導く。mode-000 の canonical file が traceback として
audit を抜けていた (j#90418 R6-F3) のがこの class の由来。

### 1.2 Write 機械 (`_replace_one`)

1 つの pinned name について、event → 遷移 → 発行 violation → staging の帰結。
`staging_live` が cleanup ownership を持つ 1 bit である。

| # | event | 次状態 | 発行 violation | staging の帰結 |
| --- | --- | --- | --- | --- |
| W0 | source `_read_bound` 失敗 | 中断 | `B` / `SOURCE_SWAPPED_DURING_SYNC` | 未作成 |
| W1 | `O_CREAT\|O_EXCL\|O_NOFOLLOW` 失敗 | 中断 | `W` / `WRITE_FAILED` "staging file could not be created" | 未作成 |
| W2 | `ownership.prove()` (`fstat`) 成功 | 書込可 | — | live |
| W3 | `os.write` ループ失敗 / 無進捗 16 回超 | 中断 | `W` / `WRITE_FAILED` "could not be written" | `release()` |
| W4 | `os.fchmod(0o644)` 失敗 | 中断 | 同上 | `release()` |
| W5 | `os.fsync` 失敗 (`flushing=True`) | 中断 | `W` / `WRITE_FAILED` "could not be flushed to disk" | `release()` |
| W6 | `resolve()` = `FOREIGN` | 中断 | `W` / `WRITE_FAILED` "rebound while the sync held it" | **`staging_live=False`。決して unlink しない** |
| W7 | `resolve()` = `ABSENT` | 中断 | `W` / `WRITE_FAILED` "gone before it could be installed" | `release()` |
| W8 | `resolve()` = `UNREADABLE` | 中断 | `W` / `WRITE_FAILED` "could not be re-validated" | `release()` |
| W9 | `resolve()` = `UNPROVEN` | 中断 | `W` / `WRITE_FAILED` "ownership could not be proved" | `release()` |
| W10 | `os.replace` 失敗 かつ dest が symlink / 非 regular | 中断 | **`E`** / `ENTRY_SYMLINK`\|`ENTRY_NOT_REGULAR` | `release()` |
| W11 | `os.replace` 失敗 (その他) | 中断 | `W` / `WRITE_FAILED` "could not be replaced" | `release()` |
| W12 | `os.replace` 成功 | 完了 | — | `staging_live=False` (rename が消費) |
| W13 | 任意時点で `BaseException` unwind | 再送出 | — | `_teardown_during(primary, release, temp.close)` |
| W14 | 上記いずれの後も必ず | — | close 失敗時 `W` / `WRITE_FAILED` "could not be closed cleanly" | `_close_staging` |

順序で load-bearing な点 [実測、Redmine #14652]:

- **staging descriptor は最後に閉じる。** ownership 証明は inode 番号比較であり、
  番号が identity なのは inode が pin されている間だけ。先に閉じると、staging 名に
  差し替えられた file が同じ番号を継承して pinned reference として install される
  (`python:3.12-slim` の overlayfs `/tmp` で 20/20 再利用。tmpfs / APFS は 0/20)。
- **だから deferred write error は `fsync` が報告する。** かつて close がその位置に
  いた。`fsync` は `platform_capabilities` に **意図的に入れない** — dir_fd を取らず、
  degrade 先の path-based fallback を持たないため (j#90450 R7-F4)。
- **W13 は `OSError` に限定しない。** 非 OSError の unwind が hook にも swap safety net
  にも届かず staging を残した (j#90472 R10-F1)。`release` が `temp.close` より**先**に
  走る — release は close が手放そうとしている descriptor を必要とする。

### 1.3 Sync 機械 (`sync()`)

1. `audit()` → `blocks_write` なら refuse。出力は "nothing was written."
2. source bind。fd `None` なら refuse。
3. mirror bind (`create=True`)。fd `None` なら refuse。
4. `MIRRORED_REFERENCES` を順に `_replace_one`。**最初の problems で中断**し
   "aborted the legacy project skill mirror sync." を返す。
5. 書き終えたら **再 audit**。`not ok` なら "did not converge"。
6. 成功。

### 1.4 Retention 機械 (`owned_descriptors`)

unwind 中の teardown 失敗を、**失うことなく**、**primary を置き換えることなく**
記録する pure な in-memory 機械。filesystem に一切触れない [実測: 後述の導出器で
この cluster の 19 test が FS 非依存と確定]。

3 つの outcome channel を分離する:

| channel | 例 | 扱い |
| --- | --- | --- |
| **returned failure** | `close()` の `False`、release の非空 violation tuple | ledger に記録 + note。例外ではない |
| **ordinary `Exception`** | cleanup の `OSError` | ledger に記録 + note。primary は不変 |
| **control-flow `BaseException`** | `KeyboardInterrupt` / `SystemExit` / `GeneratorExit` | **primary を outrank**。最初の 1 つを caller が raise |

不変条件 [実測、docstring]:

- **残りの action は常に走る。** action 自身の失敗時だけでなく、secondary を
  *記録している最中*の interrupt でも (j#90492 R14-F1)。
- **teardown 失敗を 1 つも落とさない。** raise / return / 記録中到着のいずれも、
  fallible な処理が触る**前**に object として ledger へ append される。
- **precedence と retention は別問題。** 最初の control-flow が caller の raise する
  もので、以降も ledger には載る (R14-F2 が逆方向に間違えた点)。
- **occurrence は 1 箇所でだけ retain される。** 2 つの action が同じ singleton
  `False` を返したら **2 entry** (j#90517 R17-F2)。retry は 2 つ目ではない (R19-F1)。

carrier の設計 [実測]: `_LEDGER_KEY` は identity object、`_instance_state` は
`BaseException.__dict__["__dict__"].__get__` を bind したもの、`_Ledger` は
exact type 比較。4 つの carrier が測定のうえ破棄された (`setattr` / `__context__` /
`object.__getattribute__` / 文字列 key) 経緯が docstring に残る。

`_RETENTION_ATTEMPTS = 4` が main rail と exit rail の双方を bound する。

### 1.5 Cleanup ownership (`_release_staging`)

| `ownership.resolve()` | 動作 | violation |
| --- | --- | --- |
| `ABSENT` | 何もしない | なし (残骸を騙って主張しない。j#90467 R9-F3) |
| `UNREADABLE` | 触れない | `W` / `CLEANUP_FAILED` "could not be inspected and may still be present" |
| `FOREIGN` | 触れない | `W` / `CLEANUP_FAILED` "now refers to another entry, which was left untouched" |
| `UNPROVEN` | 触れない | `W` / `CLEANUP_FAILED` "could not be proved, so it was left in place" |
| `CONFIRMED` → `unlink` 成功 | 削除 | なし |
| `CONFIRMED` → `FileNotFoundError` | — | なし |
| `CONFIRMED` → その他 `OSError` | — | `W` / `CLEANUP_FAILED` "could not be removed and is still present" |

**`CONFIRMED` だけが unlink する。** 認識されない答えで unlink へ落ちるのは
fail-open であり、これは release が削除の前に参照する値である。

残る window [実測、j#90472 R10-F3 が明示的に受容]: 答えと unlink の間。swap 時の
residual (foreign inode を install しうる) とは**形が違う** — こちらは foreign entry を
**削除**しうる。両者とも「mirror directory を変更できる actor は entry も直接
変更できる」threat model の内側にあり、閉じるには directory-level exclusion が要る。

### 1.6 Retry admissibility [導出]

`sync()` の再実行が収束するかは、直前の失敗が残した状態で決まる。

| 直前の終了状態 | 再実行は収束するか | 根拠 |
| --- | --- | --- |
| preflight refuse (A–E, P) | **しない** — tree を直すまで同じ refuse | `blocks_write` は preflight で評価される |
| W12 成功 | する (idempotent) | `test_clean_tree_passes_and_syncs_idempotently` |
| W0/W1 中断 | する | staging 未作成、mirror 不変 |
| W3–W5, W7–W11 かつ release 成功 | する | staging 削除済。既に replace 済の name は content 一致で no-op |
| **`CLEANUP_FAILED` が出た** (W6 含む) | **しない (operator 介入が要る)** | 残った staging 名は `MIRRORED_REFERENCES` 外 → 次回 rule `D` / `UNPINNED_ENTRY` → `blocks_write` |
| ループ途中の中断 | する | 先行 name は install 済だが `_replace_one` は content べき等 |

**重要な非対称 [導出]:** `MirrorAudit.blocks_write` の docstring は「A–E は
sync 全体を止める — 決して部分的にではない」と述べるが、これは **preflight** の
主張である。W ループ途中の abort は先行 name を install 済のまま残す。abort 文言
"Re-run once the tracked paths are stable." がそれを前提にしている。この 2 つの
「部分性」は別概念であり、実装 Task で混同しないこと。

`clear_residue` の recovery 文が「これは本 tool 自身の残骸なので削除は安全」と
述べる一方、`disposition_unpinned` は「interrupted run の残骸を含め、代わりに
削除はしない」と述べる。**両立している** — 前者は operator に許可を与え、後者は
tool が自動でやらないと宣言する。

---

## 2. テスト現況の導出 inventory

### 2.1 導出方法 (手列挙ではない)

test module の AST を走査し、各 test method が使う注入 surface を、**同一 class 内の
helper method を推移的にたどって**分類した。手で数えた一覧ではなく導出結果であり、
test 総数が `unittest` discovery と一致することで導出の網羅性を確認している
(`127` = `Ran 127 tests`)。

分類する surface: `source_line` (source の**行**を解決して注入) / `trace`
(`sys.settrace`) / `private_symbol` (先頭 `_` の module-private を patch または参照) /
`os_patch` (`os` primitive を patch) / `ast_probe` (`ast` を oracle にする) /
`real_fs` (`_stage()` / `mkdtemp` / tracked tree) / `subprocess`。

> 実装 Task への申し送り [未確認]: この導出器は本 Task の scratchpad で走らせた
> read-only な調査 script であり、repo には commit していない。分割後も同じ
> 分類を継続したいなら、`tests/support/` 配下の共有 oracle として作り直すのが
> 筋である (判断は #14592 実装 Task 側)。

### 2.2 class 別 [実測]

| class | tests | test 本体行 | os_patch | private_symbol |
| --- | ---: | ---: | ---: | ---: |
| `LegacyProjectSkillMirrorTest` (tracked tree) | 7 | 79 | 0 | 0 |
| `LegacyMirrorSyncServiceTest` | 110 | 2,896 | 36 | 23 |
| `LegacyMirrorWrapperCliTest` (wrapper CLI) | 10 | 113 | 0 | 0 |
| 計 | **127** | 3,088 | 36 | 23 |

`_MirrorTreeFixture` (198–279) は test を持たない共有 fixture。

### 2.3 実 collaborator 別の分割 [導出]

| 区分 | tests | 行 |
| --- | ---: | ---: |
| filesystem に触れない (pure in-memory) | 23 | 796 |
| 実 tree を建てる (temp dir / tracked tree) | 96 | 2,193 |
| `subprocess` (wrapper CLI black-box) | 8 | 99 |

実 tree を建てる 96 の内訳 [導出]:

| 内訳 | tests | 行 |
| --- | ---: | ---: |
| `LegacyMirrorSyncServiceTest` で `os` primitive を patch (boundary injection) | 34 | 1,192 |
| `LegacyMirrorSyncServiceTest` で patch なし (純粋な結線) | 53 | 908 |
| `LegacyProjectSkillMirrorTest` (tracked tree そのもの) | 7 | 79 |
| `LegacyMirrorWrapperCliTest` のうち subprocess を使わないもの | 2 | 14 |
| 計 | **96** | **2,193** |

### 2.4 pure cluster の位置 [実測]

FS 非依存 23 test のうち **19 が 2263–3351 行にほぼ連続**して並び、すべて
`owned_descriptors` の retention / ledger 機械を対象とする。この範囲に混在する
FS 依存は 2 件だけ (`test_a_later_control_flow_failure_is_recorded_not_dropped` 2217、
`test_a_broken_note_still_leaves_the_cleanup_failure_reachable` 2317) で、いずれも
「同じ property を実 write path 経由で証明する」もの。

残り 4 の FS 非依存は `platform_capabilities` 側 (3469 / 3549 / 3582 / 3607)。

**この cluster は既に pure な sub-suite として存在している。** 不足しているのは
純粋性ではなく *配置* と *到達手段* である。

---

## 3. Source 行番号 / private call order 依存テストの処遇

### 3.1 source 行番号に依存する test — 3 件 [実測]

いずれも `LegacyMirrorSyncServiceTest`。

| line | test | 注入点 |
| ---: | --- | --- |
| 2779 | `test_an_arrival_survives_a_failure_before_it_reaches_the_queue` | `_Retention._enqueue` の `self._queued.append(` 行 |
| 2972 | `test_a_nested_interrupt_never_skips_a_remaining_action` | `_took_the_interrupt` の**全 executable 行**、2 rail × 3 schedule |
| 3211 | `test_retention_survives_an_interrupt_at_a_commit_boundary` | `_Retention._drain` の指定行 |

**判定: 3 件とも残置。** 理由:

1. **literal な行番号を書いていない。** `_source_line` は source text を検索して
   行を解決し、marker が消えたら `AssertionError(... the probe is stale)` で
   落ちる。`_helper_lines` は `code.co_lines()` から executable 行を列挙し、
   期待する region の並びと一致しなければ resolution が失敗する。
   **静かに発火しなくなる形になっていない** — これが本 doc が残置を許す条件である。
2. **公開 API では表現できない property を測っている。** 主張は「*任意の命令*に
   interrupt が到着しても、残りの teardown action が skip されない」。
   「命令 N で interrupt」を渡せる公開入口は存在しない。
3. **入力経由の代替は測定のうえ棄却されている。** docstring 記載: 3 世代の carrier が
   hostile primary に抜かれ、各 fix が前世代の攻撃入力を到達不能にしたため、
   入力から assert すると「たまたままだ通る攻撃」しか pin できない。
4. **範囲を狭く書くと欠陥が隠れた実績がある。** R23-F1 は注入が priority 代入の
   *後*に座っていたため、R24-F1 は列挙が `try:` header の*後*から始まっていたため
   すり抜けた。R25-F1 は「`try:`/`except:`/`return` に*見える*行」で分類したせいで
   追加された region が黙って escape surface を広げた。

これは #14592 acceptance の「廃止不能な最小境界と理由を文書化する」に該当する。
**廃止対象ではなく、文書化された最小境界として扱う。**

残置の条件 (実装 Task が守るべき) [導出]:

- marker text が消えたら probe が落ちること (現状の性質を退行させない)。
- 注入行の集合は `co_lines()` から**導出**すること。名指し列挙へ戻さない。
- 対象は `owned_descriptors` の retention helper に限る。audit / write 機械へ
  この手法を広げない。

### 3.2 private symbol に到達する test — 23 件 [実測]

`test_the_probe_anchor_is_not_a_directory` / `test_capability_manifest_...` /
`test_each_required_capability_individually_fails_closed` (3 件、
`platform_capabilities` 側) と、`test_ownership_refuses_to_answer_once_the_descriptor_is_closed` /
`test_the_staging_descriptor_still_pins_the_inode_at_every_ownership_question`
(2 件、`_StagingOwnership`)、残り 18 件が `owned_descriptors` の retention 内部。

**判定: 分類を 2 つに割る。**

- **(a) 注入 seam として private を patch している** — `_ledger` / `_Occurrence` /
  `_Retention._enqueue` を差し替えて carrier の失敗を schedule するもの。
  これらは *collaborator を fake で置く* 行為であり、tests-placement policy の
  unit 定義 (「collaborator は fake / stub / 注入 seam で置く」) に**適合している**。
  問題は private 名を掴んでいる点だけである。
  → **残置。ただし #14592 の behavior-change Task で、retention の carrier を
  差し替えるための狭い seam を module の公開面に持ち上げるのが望ましい**
  [未確認: seam の形は設計判断であり本 Task では決めない]。
- **(b) private を assert 対象そのものにしている** — `_LEDGER_KEY` が attribute 名
  でないこと、pickle 境界、hostile `__dict__` descriptor への耐性。
  これらは `teardown_failures()` (公開 read) と ledger 到達可能性で述べ直せる。
  → **公開 API 経由の言い換えを検討する候補**。ただし現状 green であり、
  言い換えは behavior-change ではなく test の書き換えなので、**move commit には
  含めない**。

### 3.3 private call order に依存する test [導出]

**「private の呼び出し順序」を直接 assert している test は 1 件**:
`test_the_staging_release_always_precedes_the_staging_close` (1988, 51 行)。
`os.open` / `os.close` を tracking して release と close の相対順序を観測する。

これは 1.2 で述べた「release が close より先」という **load-bearing な順序契約**
(inode pin が生きている間に ownership を消費する) の直接証明である。
→ **残置。** 順序そのものが契約なので、順序に依存しない書き方は存在しない。

他に「呼び出し回数」を pin するもの: `test_cleanup_helper_runs_exactly_once_when_it_raises`
(3352)。これは `_release_staging` の二重実行 (j#90467 R9-F3) の regression であり、
**regressions への移設候補**。

---

## 4. #14592 の 6 invariant → 検証層の一意対応

#14592 acceptance が挙げる 6 つの不変条件を、**各 1 層に一意に割り当てる**。
複数層に現れる property は「主張の正本を持つ層」を 1 つ選び、他層は派生とする。

層の定義 (tests-placement-discovery-policy の決定木に従う):

- **L1 pure unit** — 実 collaborator 0、filesystem に触れない。`tests/unit/<ctx>/`
- **L2 real-file integration** — 実 tree を建て、patch なしで結線を見る。`tests/integration/<ctx>/`
- **L3 boundary injection** — 実 tree + `os` primitive の限定注入。`tests/unit/<ctx>/`
- **L4 issue regression** — 修正済み defect の再発 pin。`tests/regressions/`
- **L5 support** — 共有 fake / fault schedule。`tests/support/` (test ではない)

| # | #14592 invariant | 正本層 | 主張の形 | 現行の代表 test [実測] |
| --- | --- | --- | --- | --- |
| 1 | 元の正式ファイルを壊さない | **L2** | sync 前後で canonical 側の byte 集合が不変。mirror 外に書き込みが無い | `test_source_parent_swapped_after_audit_writes_no_external_bytes` (654) / `test_mirror_parent_swapped_after_audit_writes_nothing_outside` (683) / `test_symlinked_pinned_entry_is_rejected_without_writing_through` (583) / `test_hardlinked_entry_is_replaced_not_written_through` (615) |
| 2 | 自分の一時ファイルだけを処理する | **L2** | 他 run / 他者の entry が staging 名に居るとき、削除も install もしない | `test_a_file_sharing_the_temp_prefix_is_never_deleted` (387) / `test_a_concurrent_run_neither_deletes_nor_is_deleted` (412) / `test_cleanup_leaves_a_foreign_entry_at_the_staging_name` (1317) / `test_an_unprovable_staging_identity_never_unlinks` (1543) / #14652 の 2 件 (793 / 830) |
| 3 | 最初の障害を失わない | **L1** | `_teardown_during` の返す control-flow が最初の 1 つ。primary が置き換わらない | `test_control_flow_priority_keeps_the_first_and_records_the_rest` (2290) / `test_the_final_flush_surfaces_the_control_flow_it_hits` (3263) |
| 4 | 後続障害を保持する | **L1** | 全 teardown 失敗が `teardown_failures()` から object として読める | `test_a_secondary_that_cannot_be_stringified_is_still_retained` (2391) / `test_an_interrupt_while_recording_a_later_failure_is_retained` (2437) / `test_a_carrier_failure_never_skips_a_remaining_action` (2687) |
| 5 | 同一発生を重複記録しない | **L1** | 1 occurrence = 1 ledger entry。retry は 2 つ目にならない | `test_each_occurrence_is_one_ledger_entry` (2657) / `test_retention_survives_an_interrupt_at_a_commit_boundary` (3211) / `test_an_arrival_survives_a_failure_before_it_reaches_the_queue` (2779) |
| 6 | 再実行可能 | **L2** | 1.6 の admissibility 表どおりに収束する / しない | `test_clean_tree_passes_and_syncs_idempotently` (286) / `test_canonical_only_edit_is_caught_and_repaired` (293) / `test_crash_residue_asks_for_a_reviewed_disposition` (401) / `test_success_is_not_reported_on_an_unverified_tree` (479) |

**一意性の根拠 [導出]:** invariant 1 / 2 / 6 は filesystem の効果についての主張であり、
in-memory では表現できないので L2 が正本。invariant 3 / 4 / 5 は retention 機械の
純粋な性質で、2.4 の実測どおり FS 非依存に証明できるので L1 が正本。

**L3 の役割:** L3 は invariant の正本を持たない。**L2 で到達不能な稀 OS 異常
(短い write / 無進捗 write / fsync 失敗 / close 失敗 / FIFO 差し替え) を L2 の
主張へ到達させるための決定化手段**である。34 件 (1,192 行) がここに属する。

**L4 の役割:** 上表の test の多くは同時に「特定 review round の defect の再発 pin」
でもある。placement policy の決定木は regressions を unit より**上**に置くため、
機械的に適用すると大半が regressions へ落ちる。それは分類として無意味なので、
本 doc は **「invariant の正本を述べる test は L1/L2 に置き、`test_<症状>` 単位で
1 defect だけを pin する test を L4 に置く」** と切る。L4 候補 [導出]:
`test_cleanup_helper_runs_exactly_once_when_it_raises` (3352)、
`test_a_close_unwind_never_closes_a_reused_descriptor_number` (1618)、
`test_the_directory_walk_never_closes_a_reused_descriptor_number` (1720)、
`test_a_supported_host_is_not_refused_by_a_stale_advertisement` (3561)、
`test_the_exact_linux_312_advertisement_is_accepted` (3582)。

> [未確認] L4 への移設は 1 defect = 1 file の命名規約 (`test_issue_<id>_*.py`) を
> 要求する。上記 5 件は 4 つの異なる Redmine issue に属するため file 数が増える。
> 増分を許容するか、`test_<症状>_regression.py` 形にまとめるかは #14592 実装 Task
> の判断とする。

---

## 5. テスト配置 matrix

現行の実体 layout は `bounded-context-map.md` の
`## Redmine-numbered package path map (#12622)` に従う
`e_<order>_<slug>` / `f_<order>_<slug>` 形である
(`tests-placement-discovery-policy.md` の layout 図は #12490 時点の 1 階層形)。

context = `e_130_governance_distribution` / feature = `f_150_skill_plugin_distribution`。

移設元は `test_legacy_project_skill_mirror.py` の 127 test。**行き先の tests 合計は
127、行合計は 3,088 でなければならない** — この表はその分割であり、自己検算できる
形にしてある。

| 層 | 配置 | 内容 | tests | 行 |
| --- | --- | --- | ---: | ---: |
| L1 | 新規 `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_owned_descriptor_teardown.py` | retention / ledger / occurrence 機械 | 19 | 746 |
| L1 | 新規 `.../test_legacy_mirror_platform_probe.py` | capability probe | 4 | 50 |
| L3 | 新規 `.../test_legacy_mirror_fault_injection.py` | 実 tree + `os` primitive 注入 | 34 | 1,192 |
| L2 | 新規 `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_sync.py` | 実 tree 結線 (patch なし) | 53 | 908 |
| L2 | 既存 class `LegacyProjectSkillMirrorTest` → integration | tracked tree そのものの guardrail | 7 | 79 |
| L2 | 既存 class `LegacyMirrorWrapperCliTest` → integration | wrapper CLI black-box (うち 8 が subprocess) | 10 | 113 |
| | **計** | | **127** | **3,088** |

L4 (regressions) への移設は 4 章末尾の 5 件が候補で、上表の L1 / L2 / L3 から
差し引かれる。件数の再配分であって増減ではない。

移設対象外 (既存のまま):

| 層 | 配置 | 内容 | 行 |
| --- | --- | --- | ---: |
| L1 | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_contract.py` | domain の rule / recovery 導出 | 232 |
| L5 | `tests/support/legacy_mirror_fault_schedule.py` (新規) | `_MirrorTreeFixture` (現行 198–279 の 82 行) + 共有 fault schedule fake | — |

**行数は test method 本体のみ**の実測集計である。分割後の各 module には
module docstring / import / fixture 参照が加わるため、実 file 行数はこれより
大きくなる。

`tests/integration/e_130_governance_distribution/` は**既に存在する** [実測] ので
新規 context directory は作らない。`tests/support/` も存在する (helper 7 件)。

### 5.1 discovery 不変条件 [導出]

- discovery 正本コマンド `python -m unittest discover -s tests -v` は**変えない**。
- 分割前後で collected 数が一致すること。現行 `127`。
  分割は test の増減を伴わないので、**移設後の合計も 127** でなければならない。
- 新規 directory には `__init__.py` を置く (欠落は nested test の false green)。
- module 名の一意性: `test_legacy_mirror_sync.py` を integration に置くと
  source module 名 `legacy_mirror_sync.py` と衝突しないが、
  unit 側の既存 `test_legacy_project_skill_mirror.py` とは別名にすること。

### 5.2 module-health gate との関係 [実測]

`module-health-gate.md` の `scope.include` は `src/mozyo_bridge` のみで、
`tests/` は gate 対象外。したがって **3,865 行の test file は health gate に
かかっていない**。分割の駆動理由は placement policy と可読性であって
health gate ではない。

source 側は `legacy_mirror_sync.py` 899 / `owned_descriptors.py` 725 で
`max_module_lines: 1000` 未満。`owned_descriptors.py` はこの閾値を越えたときの
分割の結果である [実測: module docstring]。**#14592 で `legacy_mirror_sync.py` に
状態機械を足すと 1000 を越えうる** [未確認: 増分量は実装次第]。

---

## 6. Hypothesis 採否

### 6.1 判定

**採用しない。** 代わりに、明示的な遷移表を持つ table-driven model test を
標準 `unittest` の `subTest` で書く。再開条件は 6.4 に置く。

### 6.2 根拠 — 依存追加コスト [実測]

- `hypothesis` は `pyproject.toml` の `dependencies` にも
  `optional-dependencies` (`otel` / `typecheck` のみ) にも**無い**。
  実行環境にも import できない (`ModuleNotFoundError`)。
- `.github/workflows/test.yml` の 3 job (`quick` / `integration` / `full-matrix`) は
  いずれも `python -m pip install .` **のみ**で環境を作る。採用すると 3 job すべてに
  install step が要る。`full-matrix` は Python 3.10–3.13 の 4 環境。
- つまり最小でも「新 dev extra + workflow 3 箇所 + matrix 4 環境」の増分になる。
  これは #14592 の scope (test 構造の是正) に対して不相応に大きい。

### 6.3 根拠 — この suite の欠陥を Hypothesis は見つけない [導出]

決定的な点。retention 機械が pin している欠陥は、R15 から R26 まで一貫して
**「control-flow 例外が特定の bytecode 命令に到着した」**という形をしている
(1.4 / 3.1)。docstring が名指しする R19-F1 / R23-F1 / R24-F1 / R25-F1 / R26-F1 は
すべてこの形である。

Hypothesis が生成するのは**値**と**操作列**であり、命令レベルの到着点ではない。
`sys.settrace` の行注入はそれを生成できる。したがって:

- 本 suite が存在する理由になっている欠陥 class を、Hypothesis は**構成できない**。
- Hypothesis で置き換えられるのは、値と操作列で表現できる部分 —
  ledger admission の idempotence、occurrence 数の保存則 — に限られる。
  それは 19 件中おおよそ 5–6 件 (`test_each_occurrence_is_one_ledger_entry` 型) で、
  かつ現行の table-driven 版が既に green である。

「同じ形で複数 round 外し続けた路線は捨てる」という repo の既存判断とも整合する:
本件はまだ外していないが、**欠陥の形と生成器の形が合っていない**時点で採用理由が
成立しない。

### 6.4 根拠 — seed 再現性 / CI 時間 / 保守性 [実測 + 導出]

| 軸 | 評価 |
| --- | --- |
| **seed 再現性** | `derandomize=True` で決定化は可能。ただし既定では `.hypothesis/` の example DB を repo 内に作る。untracked な filesystem 副作用を test が産むのは #14655 が是正したのと同型であり、採用するなら DB 無効化が必須条件になる |
| **CI 時間** | 現行 127 test が **3.30–4.29 秒** (4 run 実測)。property test は 1 property あたり既定 100 例。`full-matrix` は 4 環境 × nightly。桁が変わる |
| **保守性** | repo の既存 idiom は `subTest` による table-driven。導入すると読み手が 2 つの test 語彙を持つ。また `RuleBasedStateMachine` の shrink 結果は「最小反例」であって、本 suite が要求する「全 executable 行を覆った」という**網羅の証明**にはならない |
| **網羅の証明** | 現行 `_helper_lines` は「helper が region を得ても失っても resolution が落ちる」形で網羅を pin する。Hypothesis はサンプリングなので、この保証を提供しない |

### 6.5 代替案 (推奨)

1. **retention 機械の遷移表を明示化する。** state = (queue の内容, ledger の内容,
   first control-flow)。event = `remember` / `flush` / carrier 失敗 (ordinary /
   control-flow) / occurrence 構築失敗。表を data として書き、`subTest` で全組合せを
   回す。現行の 19 件が主張していることを、散在した個別 case ではなく 1 つの表から
   導く。
2. **行注入は表の外に置く。** 3.1 の 3 件は「表のどの遷移も、任意の命令で
   interrupt されても壊れない」という**メタ性質**であり、表とは別の層に残す。
3. **oracle を test の外へ出す** — 網羅の主張 (`co_lines()` からの導出、
   capability manifest の `ast` 導出) は既にそうなっている。分割時にこの性質を
   落とさないこと。

### 6.6 再開条件

次のいずれかが成立したら Hypothesis 採否を再評価する:

- repo が別の理由で `hypothesis` を dev 依存に入れ、CI install が既成事実になる。
- 値・操作列で表現できる state machine が本件以外にも複数現れ、table-driven の
  重複が明確なコストになる。
- 命令レベル注入を要しない新しい pure state machine が #14592 の分離で生まれ、
  そこに property が閉じる。

---

## 7. 実装 Task 分割案と changed-path ownership

**behavior-preserving move** と **behavior change** を別 commit / 別 Task に割る
(`refactor-split-strategy.md` `## Move Commit Rules` 3 / 5)。

| Task | 種別 | 変更 path (排他) | 完了条件 |
| --- | --- | --- | --- |
| **T1** | move-only | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py` (削除/縮小) + 新規 test module 群 + `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/**` + `tests/support/legacy_mirror_fault_schedule.py` | discover 総数が移設前と一致。`src/**` の diff が **byte 0**。commit message に `move-only` |
| **T2** | behavior change | `src/.../application/legacy_mirror_sync.py` + `src/.../domain/**` | 状態遷移を filesystem effect から分離。1.1–1.3 の遷移表が pure に評価できる。T1 の test が無改変で green |
| **T3** | behavior change | `src/.../application/owned_descriptors.py` + T1 が作った L1 module | 3.2(a) の carrier 差し替え seam を公開面へ。private patch を減らす |
| **T4** | test-only 書き換え | T1 が作った L1 module のみ | 3.2(b) を公開 API 経由へ言い換え。`src/**` 不変 |
| **T5** | move-only | `tests/regressions/**` + T1 が作った module | 4 章 L4 候補の移設。discover 総数不変 |

**ownership 規則:**

- `src/**` に触るのは **T2 / T3 のみ**。T1 / T4 / T5 は `src/**` diff が byte 0 で
  なければ失格。
- T2 と T3 は **別 module** を持つので並行可能。ただし両者とも T1 の完了を待つ
  (移設前の test を編集すると move が汚れる)。
- T4 は T3 と**同じ file** に触る可能性があるため、**T3 の後**に直列化する。
- T5 は T1 の後、T2 / T3 と並行可能 (触る file が交わらない)。

依存順: `T1 → {T2, T3, T5}`、`T3 → T4`。

**T2 への申し送り [未確認]:** 5.2 のとおり `legacy_mirror_sync.py` は 899 行で
`max_module_lines: 1000` まで 101 行しかない。状態機械を同 module に足すと gate に
かかる可能性がある。allowlist に逃げず、`domain/` 側へ pure な遷移を出す設計を
先に決めること (`module-health-gate.md` の allowlist は `expires` と `owner_issue`
必須であり、自己承認 bump は認められていない)。

---

## 8. 未確認事項 (実装 Task へ持ち越す)

1. **retention carrier の公開 seam の形** — T3 が導入する注入点を、module の
   公開関数にするか、明示的な injection parameter にするかは設計判断。本 Task の
   read-only scope では決めない。
2. **L4 移設の file 粒度** — `test_issue_<id>_*.py` 命名で 1 defect = 1 file に
   すると file 数が増える。4 章末尾参照。
3. **`legacy_mirror_sync.py` の分割先** — 状態機械を `domain/` に出すか、
   application 内に新 module を作るかは T2 の設計。7 章末尾参照。
4. **導出器の commit 可否** — 2.1 の AST 分類器を `tests/support/` の共有 oracle
   として残すかどうか。
5. **`_StagingOwnership` の公開性** — 現状 module-private だが、invariant 2 の
   正本主張がここに集中している。公開型にするかは T3 の判断。

---

## 参照

- Redmine #14660 (本 Task) / #14592 (親 US) / #14580 / #14651 / #14652 / #14655 / #14656
- `vibes/docs/logics/tests-placement-discovery-policy.md` — 配置決定木 / discovery 不変
- `vibes/docs/logics/refactor-split-strategy.md` — `## Characterization Strategy` / `## Move Commit Rules`
- `vibes/docs/logics/module-health-gate.md` — 閾値 / scope / allowlist 契約
- `vibes/docs/logics/skill-distribution.md` — Mirror Contract
- `vibes/docs/specs/bounded-context-map.md` — `## Redmine-numbered package path map (#12622)`
