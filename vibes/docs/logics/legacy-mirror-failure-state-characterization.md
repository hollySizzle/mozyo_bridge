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

1 つの pinned name について、event → 遷移 → **primary violation** → cleanup 経路 →
**cleanup が追加しうる violation**。`staging_live` が cleanup ownership を持つ 1 bit である。

**1 event が 2 つの violation を出しうる。** `install()` の失敗行は
`(Violation(...),) + release()` を返し、`release()` は `_release_staging` 経由で
`CLEANUP_FAILED` を **追加**発行しうる。primary だけを読むと、residue が残ったのか
片付いたのかが分からない。したがって列を分けてある。

| # | event | 次状態 | primary violation | cleanup 経路 | cleanup 追加 violation |
| --- | --- | --- | --- | --- | --- |
| W0 | source `_read_bound` 失敗 | 中断 | `B` / `SOURCE_SWAPPED_DURING_SYNC` | 未作成 | — |
| W1 | `O_CREAT\|O_EXCL\|O_NOFOLLOW` 失敗 | 中断 | `W` / `WRITE_FAILED` "staging file could not be created" | 未作成 | — |
| **W2** | **`ownership.prove()` (`fstat`) 失敗** | **中断** | **`W` / `WRITE_FAILED` "could not be written"** (`flushing` はまだ `False`) | **`release()`** | **`§1.5` の表。`_identity` が `None` のままなので `resolve()` は `UNPROVEN` → `CLEANUP_FAILED` "ownership could not be proved"** |
| W3 | `os.write` ループ失敗 / 無進捗 16 回超 | 中断 | `W` / `WRITE_FAILED` "could not be written" | `release()` | §1.5 の表 |
| W4 | `os.fchmod(0o644)` 失敗 | 中断 | 同上 | `release()` | §1.5 の表 |
| W5 | `os.fsync` 失敗 (`flushing=True`) | 中断 | `W` / `WRITE_FAILED` "could not be flushed to disk" | `release()` | §1.5 の表 |
| W6 | `resolve()` = `FOREIGN` | 中断 | `W` / `WRITE_FAILED` "rebound while the sync held it" | **呼ばない** (`staging_live=False`) | **なし。foreign entry は staging 名に残る** |
| W7 | `resolve()` = `ABSENT` | 中断 | `W` / `WRITE_FAILED` "gone before it could be installed" | `release()` | §1.5 の表 |
| W8 | `resolve()` = `UNREADABLE` | 中断 | `W` / `WRITE_FAILED` "could not be re-validated" | `release()` | §1.5 の表 |
| W9 | `resolve()` = `UNPROVEN` | 中断 | `W` / `WRITE_FAILED` "ownership could not be proved" | `release()` | §1.5 の表 |
| W10 | `os.replace` 失敗 かつ dest が symlink / 非 regular | 中断 | **`E`** / `ENTRY_SYMLINK`\|`ENTRY_NOT_REGULAR` | `release()` | §1.5 の表 |
| W11 | `os.replace` 失敗 (その他) | 中断 | `W` / `WRITE_FAILED` "could not be replaced" | `release()` | §1.5 の表 |
| W12 | `os.replace` 成功 | 完了 | — | 呼ばない (rename が消費、`staging_live=False`) | — |
| W13 | 任意時点で `BaseException` unwind | 再送出 | — | `_teardown_during(primary, release, temp.close)` | ledger へ (§1.4) |
| W14 | 上記いずれの後も必ず | — | close 失敗時 `W` / `WRITE_FAILED` "could not be closed cleanly" | `_close_staging` | — |

W2 について [実測]: `prove()` は `os.fstat(self._descriptor.fileno)` そのものなので
raise しうる。`install()` の `try:` は `prove()` を含み、`flushing = True` は `fsync`
の直前まで立たないため、`prove()` の `OSError` は "could not be written" 側に落ちる。
この分岐は仮想ではなく `test_an_unprovable_staging_identity_never_unlinks` が実際に
駆動しており (staging fd への `fstat` に `OSError(EIO)` を注入)、出力には
`WRITE_FAILED` と `CLEANUP_FAILED` の **両方**が現れる — これがこの表で列を分けている
理由そのものである。

W6 について [実測]: FOREIGN だけが `release()` を **通らない**。したがって W6 は
`CLEANUP_FAILED` を一切発行しない。「片付けに失敗した」のではなく「自分のものでは
ないので触らない」ためであり、結果として foreign entry が staging 名に残る。
retry への影響は §1.6 で扱う (residue が残る点では release 失敗と同じだが、
発行される violation が違うので、判定を `CLEANUP_FAILED` の有無に置いてはならない)。

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

**判定基準は「staging 名に何かが残っているか」であり、`CLEANUP_FAILED` が出たか
ではない。** この 2 つは一致しない: W6 (`FOREIGN`) は `release()` を通らないので
`CLEANUP_FAILED` を発行しないが、foreign entry は staging 名に残る。violation の
種類で判定すると W6 を取りこぼす。

| 直前の終了状態 | staging 名の残留 | 再実行は収束するか | 根拠 |
| --- | --- | --- | --- |
| preflight refuse (A–E, P) | なし | **しない** — tree を直すまで同じ refuse | `blocks_write` は preflight で評価される |
| W12 成功 | なし (rename が消費) | する (idempotent) | `test_clean_tree_passes_and_syncs_idempotently` |
| W0 / W1 中断 | なし (未作成) | する | mirror 不変 |
| W2–W5, W7–W11 かつ release が `CONFIRMED` → unlink 成功 | なし | する | staging 削除済。既に replace 済の name は content 一致で no-op |
| W2–W5, W7–W11 かつ release が `CLEANUP_FAILED` を発行 | **あり** (自分の残骸) | **しない (operator 介入が要る)** | 次回 rule `D` / `UNPINNED_ENTRY` → `blocks_write` |
| **W6 (`FOREIGN`)** | **あり (他者の entry)** | **しない (operator 介入が要る)** | `CLEANUP_FAILED` は**出ない**が、staging 名の entry は `MIRRORED_REFERENCES` 外なので同じく rule `D` → `blocks_write` |
| ループ途中の中断 | 上記いずれかに従う | staging が残らなければする | 先行 name は install 済だが `_replace_one` は content べき等 |

残留した場合に operator が受け取る指示は、**どの run か**で変わる [実測]。

| | 失敗した run (`sync()` の abort 出力) | 次の run (`check()` / `sync()` preflight) |
| --- | --- | --- |
| release が `CLEANUP_FAILED` | `RECOVERY_WRITE_FAILED` + **`RECOVERY_CLEAR_RESIDUE`** (「本 tool 自身の残骸なので削除は安全」) | rule `D` → `RECOVERY_DISPOSITION_UNPINNED` |
| **W6 (`FOREIGN`)** | `RECOVERY_WRITE_FAILED` **のみ** — `CLEANUP_FAILED` が無いので `clear_residue` は導出されない | rule `D` → `RECOVERY_DISPOSITION_UNPINNED` |

失敗した run の時点で **`clear_residue` が出るか出ないかが唯一の差**であり、それは
正しい: 自分の残骸は消してよいが、W6 で残っているのは他者の entry なので「消して
よい」と言ってはならない。次の run では両者とも同じ rule `D` の disposition 要求に
収束する。実装 Task でこの 2 経路を畳むと、foreign entry に対して「削除は安全」と
案内することになる。

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
test 総数が focused module の実行件数と一致することで導出の網羅性を確認している
(`127` = `Ran 127 tests`。これは §5.3 の **D1** であり、repository 全体の
discovery 件数 D2=13,207 とは別の母数である)。

分類する surface: `source_line` (source の**行**を解決して注入) / `trace`
(`sys.settrace`) / `private_symbol` (先頭 `_` の module-private を patch または参照) /
`os_patch` (`os` primitive を patch) / `ast_probe` (`ast` を oracle にする) /
`real_fs` (`_stage()` / `mkdtemp` / tracked tree) / `subprocess`。

§5.0 の配置決定木の適用も、同じ方法で source から導出している (分岐 3 は
docstring の defect anchor、分岐 4 は注入 surface)。

> **再現性の欠如 [未確認]:** この導出器は本 Task の scratchpad で走らせた read-only
> な調査 script であり、repo に commit していない。つまり **本 doc の 127 / 23 / 96 /
> 8 / 69 / 53 / 5 という分類値は、現状では第三者が再実行して確認できない**。
> R1 review j#92353 の Verification 節が指摘したとおりであり、事実として認める。
> 対応方針 (案 A / 案 B) と、どちらも採らない選択肢が無いことは §8 に書いた。

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

### 4.0 「検証層」と「配置」は別の軸である

R1 はこの 2 つを 1 つの記号 (L1–L5) に畳み、その結果 placement policy の決定木を
doc 内で上書きしていた。撤回する。以後、次の 2 軸を分けて扱う。

- **検証層 (evidence kind)** — その test が *どういう証拠* を出すか。本 doc が
  #14592 acceptance 2 に答えるために定義する分類であり、**directory を決めない**。
- **配置 (directory)** — `tests-placement-discovery-policy.md` `## 配置決定木` が
  **単独の正本**として決める。本 doc は決定木を適用するだけで、例外を作らない。

検証層 (本 doc のローカル分類。配置とは独立):

| 記号 | 検証層 | 定義 |
| --- | --- | --- |
| **E-pure** | pure state | filesystem に触れず、in-memory の state 機械だけで証明する |
| **E-file** | real-file | 実 tree を建て、patch なしで結線の効果を見る |
| **E-inject** | boundary injection | 実 tree + `os` primitive の限定注入で、稀 OS 異常を決定化する |

`support` (共有 fixture / fake) は証拠を出さないので検証層を持たない。
配置は決定木の分岐 1 で `tests/support/` に決まる。

### 4.1 invariant → 検証層 (一意)

| # | #14592 invariant | 正本の検証層 | 主張の形 | 現行の代表 test [実測] |
| --- | --- | --- | --- | --- |
| 1 | 元の正式ファイルを壊さない | **E-file** | sync 前後で canonical 側の byte 集合が不変。mirror 外に書き込みが無い | `test_source_parent_swapped_after_audit_writes_no_external_bytes` (654) / `test_mirror_parent_swapped_after_audit_writes_nothing_outside` (683) / `test_symlinked_pinned_entry_is_rejected_without_writing_through` (583) / `test_hardlinked_entry_is_replaced_not_written_through` (615) |
| 2 | 自分の一時ファイルだけを処理する | **E-file** | 他 run / 他者の entry が staging 名に居るとき、削除も install もしない | `test_a_file_sharing_the_temp_prefix_is_never_deleted` (387) / `test_a_concurrent_run_neither_deletes_nor_is_deleted` (412) / `test_cleanup_leaves_a_foreign_entry_at_the_staging_name` (1317) / `test_an_unprovable_staging_identity_never_unlinks` (1543) / #14652 の 2 件 (793 / 830) |
| 3 | 最初の障害を失わない | **E-pure** | `_teardown_during` の返す control-flow が最初の 1 つ。primary が置き換わらない | `test_control_flow_priority_keeps_the_first_and_records_the_rest` (2290) / `test_the_final_flush_surfaces_the_control_flow_it_hits` (3263) |
| 4 | 後続障害を保持する | **E-pure** | 全 teardown 失敗が `teardown_failures()` から object として読める | `test_a_secondary_that_cannot_be_stringified_is_still_retained` (2391) / `test_an_interrupt_while_recording_a_later_failure_is_retained` (2437) / `test_a_carrier_failure_never_skips_a_remaining_action` (2687) |
| 5 | 同一発生を重複記録しない | **E-pure** | 1 occurrence = 1 ledger entry。retry は 2 つ目にならない | `test_each_occurrence_is_one_ledger_entry` (2657) / `test_retention_survives_an_interrupt_at_a_commit_boundary` (3211) / `test_an_arrival_survives_a_failure_before_it_reaches_the_queue` (2779) |
| 6 | 再実行可能 | **E-file** | 1.6 の admissibility 表どおりに収束する / しない | `test_clean_tree_passes_and_syncs_idempotently` (286) / `test_canonical_only_edit_is_caught_and_repaired` (293) / `test_crash_residue_asks_for_a_reviewed_disposition` (401) / `test_success_is_not_reported_on_an_unverified_tree` (479) |

**一意性の根拠 [導出]:** invariant 1 / 2 / 6 は filesystem の効果についての主張であり、
in-memory では表現できないので E-file が正本。invariant 3 / 4 / 5 は retention 機械の
純粋な性質で、2.4 の実測どおり FS 非依存に証明できるので E-pure が正本。

**E-inject の役割:** E-inject は invariant の正本を持たない。**E-file で到達不能な
稀 OS 異常 (短い write / 無進捗 write / fsync 失敗 / close 失敗 / FIFO 差し替え /
prove() 失敗) を E-file の主張へ到達させるための決定化手段**である。34 件
(1,192 行) がここに属する。

**検証層は配置を決めない。** 上表の各 test が最終的にどの directory へ行くかは
§5 の決定木適用で決まり、E-pure の test が `tests/regressions/` に置かれることも
ある。invariant の正本がどの証拠で立つか (本節) と、その test file がどこに座るか
(§5) は独立に読むこと。

---

## 5. テスト配置 matrix

現行の実体 layout は `bounded-context-map.md` の
`## Redmine-numbered package path map (#12622)` に従う
`e_<order>_<slug>` / `f_<order>_<slug>` 形である
(`tests-placement-discovery-policy.md` の layout 図は #12490 時点の 1 階層形)。

context = `e_130_governance_distribution` / feature = `f_150_skill_plugin_distribution`。

配置は `tests-placement-discovery-policy.md` `## 配置決定木` **のみ**が決める。
本 doc は決定木を機械適用した結果を報告し、例外を作らない。

### 5.0 決定木の機械適用 [導出]

決定木 (21–23 行の tie-break「早い分岐が勝つ」を含む) を 127 test に適用した。
分岐の判定は source から導出しており、手で割り当てていない。

- **分岐 3 (regressions)** — test の docstring が既修正 defect を anchor するか。
  anchor = review journal (`j#NNNNN`) / finding id (`RN-FN`) / `Redmine #NNNNN`。
  policy の regressions 定義「過去に確定した defect の再発防止 pin」に対応する。
- **分岐 4 (unit)** — 「単一 unit を隔離検証するか (**collaborator は fake**)」。
  **実 filesystem は fake ではない**ので、実 tree を建てる test はここで落ちる。
- **分岐 5 (integration)** — 残り。

結果 [導出]:

| 決定木の行き先 | tests | 行 | 割合 |
| --- | ---: | ---: | ---: |
| `tests/regressions/` (分岐 3) | **69** | **2,200** | 54% / 71% |
| `tests/integration/<ctx>/` (分岐 5) | 53 | 812 | 42% / 26% |
| `tests/unit/<ctx>/` (分岐 4) | **5** | 76 | 4% / 2% |
| 計 | **127** | **3,088** | |

`tests/unit/` に残るのは 5 件だけである [実測]:
`test_a_carrier_that_never_recovers_gives_up_the_record_only` (3300) /
`test_the_ledger_survives_a_primary_that_refuses_attributes` (3324) /
`test_an_interrupt_during_the_probe_is_not_a_missing_capability` (3549) /
`test_the_exact_linux_312_advertisement_is_accepted` (3582) /
`test_the_probe_anchor_is_not_a_directory` (3607)。

### 5.1 決定木の結論と、それが露出させた設計上の問題 [未確認]

**この配置をそのまま採る。** 決定木は canonical policy であり、本 doc に上書きする
権限はない (R1 はここで doc-local な例外を作っていた。撤回済み)。

同時に、この結果は #14592 acceptance との緊張を露出させる。事実として書く:

- §4.1 が E-pure の正本とした retention 機械 19 件のうち、**大半が分岐 3 で
  `tests/regressions/` に落ちる**。docstring が R15–R26 の finding id を持つためである。
- 結果として「pure state 機械の unit suite」という単位が directory 上には現れない。
  #14592 acceptance の「巨大な単一 test file を**責務と test 種別に従って**分割する」
  は、決定木の出力だけでは満たされない。
- 逆に分岐 3 を弱めると、policy の regressions 定義そのものを変えることになる。

**これは本 Task が決めてよい範囲を越える。** `refactor-split-strategy.md`
`## Move Commit Rules` 6 (「move が隠れた結合を露出したら押し通さず design
consultation を記録する」) に従い、**policy-level の design consultation として
分離する**。論点:

1. 「defect anchor を docstring に持つ」ことは regressions の十分条件か。
   R15–R26 の finding id は *由来* の記録であって、その test が現在主張している
   property が defect pin であるとは限らない (例: `test_each_occurrence_is_one_ledger_entry`
   は R17-F2 由来だが、主張しているのは ledger の恒久的な保存則である)。
2. 十分条件でないなら、regressions と unit/integration を分ける判定を policy 側で
   どう書き直すか。
3. 書き直すまでの間、本 family をどう置くか (決定木どおり置く / 移設を保留する)。

この 3 点が解決するまで **T1 (test 移設) は着手しない**。§7 の依存順に反映済み。

> 本 doc は判断を持たない。上記は「決定木を適用したらこうなった」という実測と、
> それが acceptance と衝突するという観測である。解決は policy 側の正本で行う。

### 5.2 検証層ごとの内訳 (参考、配置とは独立)

決定木の出力とは別に、証拠の種類で見た内訳。§4.1 の invariant 対応はこちらを読む。

| 検証層 | tests | 行 |
| --- | ---: | ---: |
| E-pure (retention 機械) | 19 | 746 |
| E-pure (capability probe) | 4 | 50 |
| E-inject (実 tree + `os` 注入) | 34 | 1,192 |
| E-file (`LegacyMirrorSyncServiceTest`、patch なし) | 53 | 908 |
| E-file (`LegacyProjectSkillMirrorTest`、tracked tree) | 7 | 79 |
| E-file (`LegacyMirrorWrapperCliTest`、うち 8 が subprocess) | 10 | 113 |
| 計 | **127** | **3,088** |

**行数は test method 本体のみ**の実測集計である。分割後の各 module には
module docstring / import / fixture 参照が加わるため、実 file 行数はこれより
大きくなる。

移設対象外 (既存のまま): `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_contract.py`
(232 行、domain の rule / recovery 導出)。

`tests/integration/e_130_governance_distribution/` は**既に存在する** [実測] ので
新規 context directory は作らない。`tests/support/` も存在する (helper 7 件)。
`tests/regressions/` も存在する。

### 5.3 discovery 不変条件 [導出]

**母数は 2 つあり、それぞれ別の command に属する。** 混ぜないこと — R1 は
「正本コマンドは `discover -s tests`」と「現行 127」を同じ箇条書きに並べ、正本
コマンドの母数が 127 であるかのように書いていた。**127 は focused module の件数**
であって repository 全体の discovery 件数ではない。

| # | 不変条件 | 検証 command | base での実測値 |
| --- | --- | --- | ---: |
| D1 | **family focused** の件数が移設前後で一致 | 移設前: `python -m unittest tests.unit.e_130_governance_distribution.f_150_skill_plugin_distribution.test_legacy_project_skill_mirror`。移設後: 行き先 module 群を列挙した `python -m unittest <targets...>` | **127** |
| D2 | **repository 全体**の discovery 件数が移設前後で一致 | `python -m unittest discover -s tests -v` (正本、文字列を変えない) | **13,207** |

D2 の base 値は read-only に計測した [実測]:
`python3 -c "import unittest; print(unittest.defaultTestLoader.discover('tests').countTestCases())"` → `13207`。

- **D1 だけでは不十分。** 移設先の directory に `__init__.py` を置き忘れると、
  focused target を直接指定した D1 は通るのに、`discover` が nested package を
  拾えず D2 が減る (policy `## Anti-patterns`「サブディレクトリの `__init__.py` を
  省いて nested test を false green にする」)。
- **D2 だけでも不十分。** 全体件数が合っていても、family の test が別 module へ
  取りこぼされたことは検出できない。
- 分割は test の増減を伴わないので、両方とも**等値**が条件であり、増減の許容幅は
  ない。
- module 名の一意性 (policy `### module 名の一意性`): 移設後の module basename が
  `tests/` 配下で衝突しないこと。`test_legacy_project_skill_mirror.py` から
  分かれる module は互いに、また既存 `test_legacy_mirror_contract.py` と別名にする。

### 5.4 module-health gate との関係 [実測]

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

前提を明示する: 以下は **test 専用の dev extra として入れる**案のコストである。
`hypothesis` を runtime dependency (`[project] dependencies`) に入れる案は、
配布物に test 専用ライブラリを載せることになるため検討対象から外している。
その除外自体は自明ではないので、前提として書く。

- `hypothesis` は `pyproject.toml` の `dependencies` にも
  `optional-dependencies` (`otel` / `typecheck` のみ) にも**無い**。
  実行環境にも import できない (`ModuleNotFoundError`)。
- `.github/workflows/test.yml` の 3 job (`quick` / `integration` / `full-matrix`) は
  いずれも `python -m pip install .` **のみ**で環境を作る。採用すると 3 job すべてに
  install step が要る。`full-matrix` は Python 3.10–3.13 の 4 環境。
- つまり最小でも「新 dev extra + workflow 3 箇所 + matrix 4 環境」の増分になる。

これは補助根拠である。決定的な根拠は 6.3 に置く。

### 6.3 根拠 — 網羅の証明を失う [導出]

> **R1 の論証は誤っていたので撤回する。** R1 は「Hypothesis は値と操作列を生成する
> ので、命令レベルの到着点を**構成できない**」と書いた。これは成立しない —
> 行番号は整数値であり、`RuleBasedStateMachine` の `@rule` は strategy 引数を取れる
> ので、`sampled_from(executable_lines)` で既存の `sys.settrace` injector を駆動する
> 構成は書ける。誤った根拠を残すと「Hypothesis が強力になれば採用」という成立し
> ない再開条件を残すことになるため、論証ごと差し替える。
> [未確認] 本環境に `hypothesis` は未 install のため、この構成可能性は API 仕様に
> 基づく判断であり、実行して確認したものではない。

成立する差は **網羅の証明** にある。

retention 機械が pin している欠陥は、R15 から R26 まで一貫して
**「control-flow 例外が特定の bytecode 命令に到着した」**形をしている (1.4 / 3.1)。
docstring が名指しする R19-F1 / R23-F1 / R24-F1 / R25-F1 / R26-F1 はすべてこの形で
あり、しかもそれぞれ **前の round が「ここは大丈夫」と見なした行**で起きている。

現行 `_helper_lines` は `code.co_lines()` から executable 行を **列挙** し、
helper が region を得ても失っても resolution が `AssertionError` で落ちる形にして
ある。つまり主張は「サンプルした行では壊れなかった」ではなく
**「この関数の executable 行はこの集合であり、その全部で壊れない」**である。

Hypothesis はサンプリングであり、この主張を出さない:

- `sampled_from` は与えた集合から**選ぶ**。既定 100 例では全行を引く保証がない。
- 仮に全行を引いたとしても、それは今回の run の性質であって、**関数が region を
  獲得したときに落ちる**という現行の性質は得られない。R24-F1 / R25-F1 はまさに
  「関数が region を増やしたのに列挙が追随しなかった」欠陥である。
- shrink が返すのは最小反例であって、網羅の証明ではない。

したがって Hypothesis は現行の導出列挙を**置換できない**。併用すれば
「列挙で網羅を証明し、Hypothesis で値空間を広げる」形はありうるが、それは
6.2 のコストを払って現行の主張に何も足さない。

置換できる範囲は、値と操作列で表現でき、かつ網羅証明を必要としない部分 —
ledger admission の idempotence、occurrence 数の保存則 — に限られる。
それは 19 件中おおよそ 5–6 件 (`test_each_occurrence_is_one_ledger_entry` 型) で、
現行の table-driven 版が既に green である。

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
| **T0** | design consultation | 変更なし (`vibes/docs/logics/tests-placement-discovery-policy.md` の改訂を伴う場合はその file) | §5.1 の 3 論点に policy-level の裁定を得る。**T1 / T5 の前提** |
| **T1** | move-only | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py` (削除/縮小) + 移設先 test module 群 + `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/**` + `tests/support/legacy_mirror_tree_fixture.py` | D1=127 / D2=13,207 の両方が一致。`src/**` diff **byte 0**。commit message に `move-only` |
| **T2** | behavior change | `src/.../application/legacy_mirror_sync.py` + `src/.../domain/**` | 状態遷移を filesystem effect から分離。1.1–1.3 の遷移表が pure に評価できる。T1 の test が無改変で green |
| **T3** | behavior change | `src/.../application/owned_descriptors.py` + T1 が作った E-pure module | 3.2(a) の carrier 差し替え seam を公開面へ。private patch を減らす |
| **T4** | test-only 書き換え | T1 が作った E-pure module のみ | 3.2(b) を公開 API 経由へ言い換え。`src/**` 不変 |
| **T5** | move-only | `tests/regressions/**` + T1 が作った module | §5.0 分岐 3 の移設。D1 / D2 不変 |
| **T6** | behavior change (test 側) | `tests/support/legacy_mirror_fault_schedule.py` (新規) + それを使う test module | 共有 fault schedule fake の**新規作成**。個別 mock の重複を縮小 |

**T1 に新規 fake を含めない [F5a 修正]:** `refactor-split-strategy.md`
`## Move Commit Rules` 3 は「No logic edits in move commit except import path
mechanical changes」である。新しい共有 fault-schedule fake を書くのは mechanical
move ではないので、**T1 から外して T6 に分けた**。T1 が `tests/support/` に置くのは
既存 `_MirrorTreeFixture` (現行 198–279 の 82 行) の**逐語移動のみ**であり、
file 名も役割どおり `legacy_mirror_tree_fixture.py` とする。

**ownership 規則:**

- `src/**` に触るのは **T2 / T3 のみ**。T1 / T4 / T5 / T6 は `src/**` diff が
  byte 0 でなければ失格。
- **T1 / T5 は T0 の裁定を待つ。** §5.1 のとおり決定木の適用結果が #14592
  acceptance と衝突しており、裁定前に移設すると、policy 改訂で再移設になる。
- T2 と T3 は **別 module** を持つので並行可能。ただし両者とも T1 の完了を待つ
  (移設前の test を編集すると move が汚れる)。
- T4 は T3 と**同じ file** に触る可能性があるため、**T3 の後**に直列化する。
- T6 は T1 の後。T2 / T3 とは触る file が交わらないので並行可能。

依存順: `T0 → T1 → {T2, T3, T5, T6}`、`T3 → T4`。

**T1 / T5 完了時の catalog 更新は完了条件である [F5b 修正]:** §5.0 の行き先が
確定した時点で、その exact path を `fc-legacy-mirror-sync` の `patterns` に加え、
`mozyo-bridge docs generate-file-conventions` を再生成し `--check` を green に
すること。これを怠ると、移設後の file を編集しても本 doc が `docs resolve` で
解決されなくなる。**T1 / T5 の完了条件に含める** (後追いの掃除にしない)。

**T2 への申し送り [未確認]:** §5.4 のとおり `legacy_mirror_sync.py` は 899 行で
`max_module_lines: 1000` まで 101 行しかない。状態機械を同 module に足すと gate に
かかる可能性がある。allowlist に逃げず、`domain/` 側へ pure な遷移を出す設計を
先に決めること (`module-health-gate.md` の allowlist は `expires` と `owner_issue`
必須であり、自己承認 bump は認められていない)。

---

## 8. 未確認事項 (実装 Task へ持ち越す)

1. **retention carrier の公開 seam の形** — T3 が導入する注入点を、module の
   公開関数にするか、明示的な injection parameter にするかは設計判断。本 Task の
   read-only scope では決めない。
2. **`tests/regressions/` 移設の file 粒度** — 決定木の分岐 3 は 69 件を
   regressions へ送る (§5.0)。`test_issue_<id>_*.py` 命名で 1 defect = 1 file に
   すると file 数が大きく増える。粒度は T0 の裁定に含める。
3. **`legacy_mirror_sync.py` の分割先** — 状態機械を `domain/` に出すか、
   application 内に新 module を作るかは T2 の設計。7 章末尾参照。
4. **`_StagingOwnership` の公開性** — 現状 module-private だが、invariant 2 の
   正本主張がここに集中している。公開型にするかは T3 の判断。
5. **§5.1 の policy 裁定 (T0)** — 「docstring の defect anchor は regressions の
   十分条件か」。本 doc は判断を持たず、決定木の出力をそのまま報告している。

### 導出器の再現性 [R1 review j#92353 Verification 節より]

R1 の 23 / 96 / 8 等の分類は scratchpad の AST 導出器で得たもので、その script を
repo に残していない。**したがって R1 時点では第三者が同じ導出を再実行できなかった。**
これは finding として立てられていないが、事実として正しい。

本 doc は R2 でさらに §5.0 の決定木適用 (69 / 53 / 5) を導出しており、再現性の
欠如は R1 より重くなっている。対応は 2 案あり、**T0 と同じ round で決める**:

- **案 A** — 導出器を `tests/support/` へ commit し、共有 oracle にする。
  「列挙が漏れたら oracle を自分の外へ出す」という repo の既存方針と整合し、
  分割後も同じ分類を再実行できる。ただし support 配下に「test ではないが CI で
  実行されない script」を置くことになり、腐敗検出の手段が別途要る。
- **案 B** — 導出器を doc の付録として貼り、実行可能な形で残す。commit 対象は
  doc だけになるが、doc と script の drift を検出する仕組みが無い。

**本 Task では決めない。** ただし、どちらも採らずに R1 の状態のままにする選択肢は
無い — 導出結果を根拠に実装 Task を分割する以上、導出は再現可能でなければならない。

---

## 参照

- Redmine #14660 (本 Task) / #14592 (親 US) / #14580 / #14651 / #14652 / #14655 / #14656
- `vibes/docs/logics/tests-placement-discovery-policy.md` — 配置決定木 / discovery 不変
- `vibes/docs/logics/refactor-split-strategy.md` — `## Characterization Strategy` / `## Move Commit Rules`
- `vibes/docs/logics/module-health-gate.md` — 閾値 / scope / allowlist 契約
- `vibes/docs/logics/skill-distribution.md` — Mirror Contract
- `vibes/docs/specs/bounded-context-map.md` — `## Redmine-numbered package path map (#12622)`
