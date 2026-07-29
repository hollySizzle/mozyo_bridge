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

`staging_live` が cleanup ownership を持つ 1 bit である。

#### rail は 2 本ある [実測]

同じ失敗でも、**どちらの rail を通ったかで出口が変わる**。`_replace_one` の末尾:

```
try:
    problems = install()
except BaseException as primary:
    interrupt = _teardown_during(primary, release, temp.close)
    if interrupt is not None:
        raise interrupt
    raise                                   # ← ここで終わる
return problems + self._close_staging(temp, subject)
```

| | **normal-return rail** (`install()` が値を返した) | **unwind rail** (`install()` が `BaseException` を投げた) |
| --- | --- | --- |
| cleanup を呼ぶ主体 | `install()` 内の `release()` | `_teardown_during(primary, release, temp.close)` |
| close を呼ぶ主体 | **`_close_staging`** | `temp.close` (teardown action の 1 つ) |
| release の violation tuple | **caller への typed violation になる** | **ledger に retain されるだけ** (§1.4 の returned failure channel) |
| close 失敗 | **`W` / `WRITE_FAILED` "could not be closed cleanly"** | **ledger に retain されるだけ。typed violation にならない** |
| `_replace_one` の出口 | violation tuple を `sync()` へ返す | 例外を再送出。`sync()` を貫通して呼び出し元へ |

**`_close_staging` は unwind rail を通らない** — `except` 節は `raise` で終わるため、
`return problems + self._close_staging(...)` に到達しない。同じ close 失敗が、
rail によって typed violation になったり ledger 止まりになったりする。

以下の表は **normal-return rail** の event → 出口である。unwind rail は W13 の 1 行に
まとめ、詳細は上の rail 表と §1.4 を読む。

**1 event が 2 つの violation を出しうる** (normal-return rail)。`install()` の失敗行は
`(Violation(...),) + release()` を返し、`release()` は `_release_staging` 経由で
`CLEANUP_FAILED` を **追加**発行しうる。primary だけを読むと、residue が残ったのか
片付いたのかが分からない。したがって列を分けてある。

| # | event | 次状態 | primary violation | cleanup 経路 | cleanup 追加 violation |
| --- | --- | --- | --- | --- | --- |
| W0 | source `_read_bound` 失敗 | 中断 | `B` / `SOURCE_SWAPPED_DURING_SYNC` | 未作成 | — |
| W1 | `O_CREAT\|O_EXCL\|O_NOFOLLOW` 失敗 | 中断 | `W` / `WRITE_FAILED` "staging file could not be created" | 未作成 | — |
| **W2** | **`ownership.prove()` (`fstat`) 失敗** | **中断** | **`W` / `WRITE_FAILED` "could not be written"** (`flushing` はまだ `False`) | **`release()`** | **§1.5 の表のうち到達可能な 3 値のみ** (下記) |
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
| W13 | 任意時点で `BaseException` unwind | **再送出 (rail が変わる)** | **無し** — typed violation を返さない | `_teardown_during(primary, release, temp.close)` | **無し** — release も close も ledger 止まり (§1.4) |
| W14 | **normal-return rail のみ**、W0–W12 のいずれの後 | — | close 失敗時 `W` / `WRITE_FAILED` "could not be closed cleanly" | `_close_staging` | — |

W2 について [実測]: `prove()` は `os.fstat(self._descriptor.fileno)` そのものなので
raise しうる。`install()` の `try:` は `prove()` を含み、`flushing = True` は `fsync`
の直前まで立たないため、`prove()` の `OSError` は "could not be written" 側に落ちる。

**W2 後に到達しうる cleanup 結果は 3 値だけである。** `resolve()` は `os.lstat` を
**先に**実行し、`FileNotFoundError` → `ABSENT` / `OSError` → `UNREADABLE` を pin 参照
**前**に返す。`identity is None` の判定はその後なので:

| resolve | 到達可否 | cleanup 追加 violation |
| --- | --- | --- |
| `ABSENT` | 到達する (entry が消えていた) | **無し** |
| `UNREADABLE` | 到達する | `CLEANUP_FAILED` "could not be inspected and may still be present" |
| `UNPROVEN` | 到達する (entry が在る) | `CLEANUP_FAILED` "ownership could not be proved, so it was left in place" |
| `CONFIRMED` | **到達しない** | — (`_identity` が `None` なので identity 比較に進めない) |
| `FOREIGN` | **到達しない** | — (同上) |

`test_an_unprovable_staging_identity_never_unlinks` が駆動するのは `UNPROVEN` の
場合であり (staging fd への `fstat` に `OSError(EIO)` を注入し、entry は在る)、出力には
`WRITE_FAILED` と `CLEANUP_FAILED` の **両方**が現れる。ただし **これは 3 値のうち
1 つ**であって、W2 の結果ではない — 前 revision はこの 1 例を W2 行に無条件の結果として
書いていた。

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
| ループ途中の中断 (typed) | 上記いずれかに従う | staging が残らなければする | 先行 name は install 済だが `_replace_one` は content べき等 |
| **W13 unwind rail** | **`release` の結果次第 (ledger にしか出ない)** | **次 run の audit が見るものだけで決まる** | **`sync()` は値を返さず例外が貫通する。operator は report 行も recovery 行も受け取らない** |

**W13 は他の行と前提が違う [実測]。** `_replace_one` が `BaseException` を再送出すると
`sync()` の `with` を抜けて呼び出し元へ伝播するので、`sync()` は `(1, (), lines)` を
**返さない**。したがって:

- operator は `report_lines()` を一切見ない。`clear_residue` も `disposition_unpinned` も
  この run では提示されない。
- `release` は `_teardown_during` 経由で走るので staging が片付くことはあるが、その
  成否は **ledger にしか無い**。`teardown_failures(primary)` を読む caller がいなければ
  誰も知らない。
- よって retry の可否は、この run の出力ではなく **次 run の audit が staging 名に
  何を見るか**だけで決まる。上の表の他の行と同じ結論 (残っていれば rule `D` で
  blocks_write) だが、**そこへ至る情報経路が違う**。

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

> **再現手順は Appendix A にある。** 2 つの導出器の全文、同 head での実行結果、
> および 127 件全件の mapping (test 名 / 行 / 行数 / surfaces / 決定木の分岐 /
> 行き先) を載せてある。分類値 127 / 23 / 96 / 8 / 69 / 53 / 5 はそこから再現できる。

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

### 5.1 配置 matrix は **未決** である [OPEN]

**現時点で配置 matrix は確定していない。** §5.0 は「決定木を機械適用したらこう出た」
という**測定結果**であって、採用した配置ではない。

> 前 revision はここに「この配置をそのまま採る」と書きながら、同じ節の末尾で
> 「本 doc は判断を持たない」とも書いていた。両立しない。撤回し、**未決である**に
> 一本化する。R1 の「doc が policy を上書きする」を直した反動で、今度は決定したのか
> していないのか読めない形にしていた。

したがって **#14660 acceptance の「配置 matrix を決める」は本 revision では
満たしていない。** 満たしたと書かない。確定には §5.1.1 の裁定が要る。

決定木は canonical policy であり、本 doc に上書きする権限はない。同時に、その出力は
#14592 acceptance と衝突する。事実として書く:

- §4.1 が E-pure の正本とした retention 機械 19 件のうち、**大半が分岐 3 で
  `tests/regressions/` に落ちる**。docstring が R15–R26 の finding id を持つためである。
- 結果として「pure state 機械の unit suite」という単位が directory 上には現れない。
  #14592 acceptance の「巨大な単一 test file を**責務と test 種別に従って**分割する」
  は、決定木の出力だけでは満たされない。
- 逆に分岐 3 を弱めると、policy の regressions 定義そのものを変えることになる。

### 5.1.1 求める裁定 (T0 design consultation)

**これは本 Task が単独で決めてよい範囲を越える。** `refactor-split-strategy.md`
`## Move Commit Rules` 6 (「move が隠れた結合を露出したら押し通さず design
consultation を記録する」) に従い、policy-level の裁定を求める。論点と、本 doc の
**推奨案**を添える (推奨であって決定ではない):

**論点 1. 「docstring に defect anchor がある」は regressions の十分条件か。**

policy `### regressions` は「過去に確定した defect の**再発防止 pin**」「1 ファイル =
1 つの修正済み症状」「**新規機能の通常テストは regressions に置かない**」と定義する。
R15–R26 の finding id は *由来* の記録であり、その test が**現在主張している property**
が defect pin であるとは限らない。例:

- `test_each_occurrence_is_one_ledger_entry` — R17-F2 由来だが、主張は
  「1 occurrence = 1 ledger entry」という ledger の**恒久的な保存則**である。
- `test_the_directory_walk_never_closes_a_reused_descriptor_number` — j#90482 由来で、
  主張も「あの defect が戻らない」に近い。

→ **推奨: 十分条件ではない。** 由来 (provenance) と主張 (property) は別軸であり、
決定木の分岐 3 は後者で判定されるべきである。

**論点 2. 十分条件でないなら、判定を policy 側でどう書き直すか。**

→ **推奨: 「その test が失われたとき何が検出できなくなるか」で切る。**
失われるのが *特定の過去の欠陥の再来* なら regressions、*module の contract そのもの*
なら unit / integration。ただしこれは docstring から機械導出できない (意図の判断)。
policy に判定基準として書くなら、**機械判定ではなく著者宣言** (例: docstring の
所定行、あるいは module 配置そのもの) を根拠にする形になる。

**論点 3. 裁定までの間、本 family をどう置くか。**

→ **推奨: 移設を保留する。** 決定木どおり 69 件を `tests/regressions/` へ動かすと、
論点 1 が「十分条件ではない」と裁定された場合に再移設になる。移設は D1 / D2 の
両不変条件を要する重い操作であり、往復させる価値がない。

**裁定が済むまで T1 / T5 (test 移設) に着手しない。** §7 の依存順に反映済み。

> 上の推奨は本 doc の意見であり、**裁定ではない**。裁定は policy 側の正本
> (`tests-placement-discovery-policy.md`) で行い、その結果を受けて §5.0 の測定値を
> 配置 matrix へ確定させる。

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

**移設 Task の完了条件に catalog 更新を含める:** `fc-legacy-mirror-sync` の
`patterns` は **現行の exact path 2 件のみ**である。将来 path を予測して先置き
しない — 予測 pattern は裁定次第で存在しない path を指し続け、feature directory
全体への glob は無関係な test (`test_plugin_marketplace.py` /
`test_skill_workflow_guidance.py`) にまで本 doc を配布する。後者は実測で確認し、
`.mozyo-bridge/rules/docs_catalog_governance.yaml` の「対象ファイル変更時に実際に
読むべき docs に絞る」「patterns を広げる時は agent へのノイズ増加も評価する」に
反するので撤回した。

代わりに、**file を動かす Task が、動かした先の exact path を同じ commit で
`patterns` へ追加し**、`mozyo-bridge docs generate-file-conventions` を再生成して
`--check` を green にする。これを怠ると移設後の file を編集しても本 doc が
`docs resolve` で解決されない。**T1 / T5 / T6 の完了条件**であり、後追いの掃除に
しない。

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
   十分条件か」。本 doc は §5.1.1 に**推奨案**を書いたが、裁定は policy 側で行う。
   **これが済むまで配置 matrix は未決であり、acceptance の当該項目は OPEN である。**

### 導出器の恒久的な置き場 [未確認]

**本 revision で再現性そのものは閉じた** — Appendix A に導出器の全文、同 head での
実行結果、127 件全件の mapping を載せたので、第三者は script を写して再実行し、
各 test の分岐理由まで独立に検証できる。

残る問いは**恒久的な置き場**である。Appendix A は「この head の測定を検証できる」を
満たすが、**doc と script の drift を検出する仕組みが無い** — test file が変われば
Appendix A の数値は古くなるのに、それを落とす gate が無い。

- **案 A** — 導出器を `tests/support/` へ移し、共有 oracle にする。
  「列挙が漏れたら oracle を自分の外へ出す」という repo の既存方針と整合し、
  分割後も同じ分類を再実行できる。ただし support 配下に「test ではないが CI で
  実行されない script」を置くことになり、腐敗検出の手段が別途要る。
- **案 B** — Appendix A のまま据え置き、更新を各移設 Task の完了条件にする。
  追加の実行面を作らないが、drift 検出は人手に依存する。

**本 Task では決めない** (allowed scope が docs/catalog-only であり、案 A は
`tests/` への書き込みを要する)。T0 と同じ round で裁定を求める。

---

## Appendix A. 導出器と全件 mapping (再現用)

本 doc の分類値 (127 / 23 / 96 / 8 / 69 / 53 / 5) は、下記 2 script を
`origin/main-next@fef86cac` の tree に対して実行した結果である。**第三者が同じ head で
再実行して検証できるよう、全文と全件 mapping をここに置く。**

前 revision はこの導出器を scratchpad に置いたまま repo へ残さず、「再現不能」と
注記して未確認事項へ繰延べていた。繰延べは「後で決めること」には使えるが、
**すでに doc に書いた数値の検証可能性**には使えない — review はこの head に対して
行われる。

### A.1 実行手順

repo root で、下の 2 script を任意の作業ディレクトリへ保存して実行する
(いずれも read-only。repo を変更しない):

```text
python3 A2.py tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py > inventory.json
python3 A3.py tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py inventory.json tree.json
```

`A2.py` は stderr に surface ごとの件数を、`A3.py` は stdout に決定木の集計と、
分岐 3 に落ちなかった test の一覧を出す。`A3.py` は先頭で `len(rows) == 127` を
assert するので、分類漏れがあれば落ちる。

### A.2 注入 surface 分類器

```python
"""Derive, from the test module's AST, which injection surfaces each test uses.

Read-only. Emits a machine-checkable inventory instead of a hand list, because a
hand list is exactly what this characterisation must not ship (the acceptance
asks which tests depend on source lines / private call order, and a missed entry
is invisible).

Surfaces classified per test method, transitively through helper methods defined
in the same class:

  source_line      - resolves a source LINE (inspect.getsourcelines / co_lines /
                     settrace on f_lineno) to place an injection
  private_symbol   - patches or reads a module-private name (leading underscore)
  os_patch         - patches an os primitive on the module under test
  trace            - installs sys.settrace
  ast_probe        - parses source with `ast` as the oracle
  real_fs          - builds a real tree via the fixture / tmp dir
  subprocess       - shells out (wrapper CLI tests)
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

TEST = Path(sys.argv[1])
tree = ast.parse(TEST.read_text(encoding="utf-8"))

SOURCE_LINE_CALLS = {"getsourcelines", "findsource", "getsource", "co_lines"}
OS_NAMES = {
    "open", "close", "write", "read", "lstat", "fstat", "fchmod", "fsync",
    "replace", "unlink", "mkdir", "scandir", "urandom", "pipe",
    "supports_dir_fd", "supports_fd",
}


def attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


class BodyScan(ast.NodeVisitor):
    """Surfaces used directly in one function body, plus same-class helper calls."""

    def __init__(self) -> None:
        self.surfaces: set[str] = set()
        self.helpers: set[str] = set()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in SOURCE_LINE_CALLS:
            self.surfaces.add("source_line")
        if node.attr == "f_lineno":
            self.surfaces.add("source_line")
        if node.attr == "settrace":
            self.surfaces.add("trace")
        # self._helper / cls._helper -> transitive edge
        chain = attr_chain(node)
        if len(chain) == 2 and chain[0] in {"self", "cls"}:
            self.helpers.add(chain[1])
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "ast":
            self.surfaces.add("ast_probe")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        chain = attr_chain(node.func)
        dotted = ".".join(chain)
        if dotted.endswith("patch.object") or dotted.endswith("mock.patch"):
            if node.args:
                target = node.args[0]
                tchain = attr_chain(target)
                # patch.object(<obj>, "<name>", ...)
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    name = node.args[1].value
                    if isinstance(name, str):
                        if name.startswith("_"):
                            self.surfaces.add("private_symbol")
                        if name in OS_NAMES:
                            self.surfaces.add("os_patch")
                if tchain and tchain[-1].startswith("_"):
                    self.surfaces.add("private_symbol")
        if dotted.startswith("ast."):
            self.surfaces.add("ast_probe")
        if "run" in chain and "subprocess" in chain:
            self.surfaces.add("subprocess")
        if dotted.endswith("check_output") or dotted.endswith("Popen"):
            self.surfaces.add("subprocess")
        self.generic_visit(node)


def scan(fn: ast.FunctionDef) -> BodyScan:
    s = BodyScan()
    for stmt in fn.body:
        s.visit(stmt)
    # a private module symbol referenced directly (owned_descriptors._foo)
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute):
            chain = attr_chain(node)
            if len(chain) >= 2 and chain[0] in {
                "owned_descriptors",
                "legacy_mirror_sync",
                "platform_capabilities",
            }:
                if any(p.startswith("_") for p in chain[1:]):
                    s.surfaces.add("private_symbol")
    # A real tree is built only through the fixture's staging helper, or the
    # tracked repo tree itself (ROOT / SYNC_SCRIPT_PATH).
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in {
            "_stage",
            "mkdtemp",
            "TemporaryDirectory",
            "NamedTemporaryFile",
            "mkstemp",
        }:
            s.surfaces.add("real_fs")
        if isinstance(node, ast.Name) and node.id in {"ROOT", "SYNC_SCRIPT_PATH"}:
            s.surfaces.add("real_fs")
        if isinstance(node, ast.Attribute) and node.attr in {
            "canonical_ref_dir",
            "mirror_ref_dir",
            "mirror_skill_dir",
        }:
            s.surfaces.add("real_fs")
    return s


report: dict[str, dict] = {}
for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
    members = {
        n.name: n
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    scans = {name: scan(fn) for name, fn in members.items()}

    def resolve(name: str, seen: set[str]) -> set[str]:
        if name in seen or name not in scans:
            return set()
        seen.add(name)
        out = set(scans[name].surfaces)
        for helper in scans[name].helpers:
            out |= resolve(helper, seen)
        return out

    tests = {}
    for name, fn in members.items():
        if not name.startswith("test_"):
            continue
        surfaces = resolve(name, set())
        body_lines = (fn.end_lineno or fn.lineno) - fn.lineno + 1
        tests[name] = {
            "line": fn.lineno,
            "lines": body_lines,
            "surfaces": sorted(surfaces),
        }
    if tests:
        report[cls.name] = tests

print(json.dumps(report, indent=2, ensure_ascii=False))

totals: dict[str, int] = {}
for cls, tests in report.items():
    for name, info in tests.items():
        for s in info["surfaces"] or ["<none>"]:
            totals[s] = totals.get(s, 0) + 1
print("=== surface totals ===", file=sys.stderr)
for k in sorted(totals):
    print(f"{k}: {totals[k]}", file=sys.stderr)
print(
    f"tests: {sum(len(t) for t in report.values())} in {len(report)} classes",
    file=sys.stderr,
)
```

### A.3 配置決定木の適用

```python
"""Apply the canonical placement decision tree mechanically to each test.

Read-only. `tests-placement-discovery-policy.md` `## 配置決定木` is the authority:

  1. shared helper/fixture (not a test)      -> tests/support/
  2. cross-module/context workflow acceptance -> tests/scenarios/
  3. pin for an ALREADY-FIXED defect          -> tests/regressions/
  4. single unit in isolation, collaborators faked -> tests/unit/<ctx>/
  5. otherwise (several real collaborators, hermetic) -> tests/integration/<ctx>/

Tie-break (policy lines 21-23): the EARLIER branch wins when the type boundary
is ambiguous.

Branch 3 is decided from the test's own docstring: a test whose docstring
anchors a fixed defect (a `j#NNNNN` review-round journal, an `RN-FN` finding
id, or `Redmine #NNNNN`) is a regression pin by the policy's definition. This is
derived from the source text, not hand-assigned.

Branch 4 vs 5 is decided from the injection surfaces: the filesystem is a REAL
collaborator, never a fake, so any test that builds a tree or shells out fails
branch 4 and falls to 5.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

TEST = Path(sys.argv[1])
INVENTORY = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

tree = ast.parse(TEST.read_text(encoding="utf-8"))

# A fixed-defect anchor in the docstring: review journal, finding id, or issue.
ANCHOR = re.compile(r"j#\d{5}|\bR\d+-F\d+\b|Redmine #\d+|#14\d{3}")

rows = []
for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
    for fn in cls.body:
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
            continue
        info = INVENTORY.get(cls.name, {}).get(fn.name)
        if info is None:
            raise AssertionError(f"inventory missing {cls.name}.{fn.name}")
        doc = ast.get_docstring(fn) or ""
        surfaces = set(info["surfaces"])

        # branch 3: does the docstring anchor an already-fixed defect?
        anchors = ANCHOR.findall(doc)
        if anchors:
            branch, dest = 3, "tests/regressions/"
        # branch 4: single unit isolated, collaborators faked.
        elif not (surfaces & {"real_fs", "subprocess"}):
            branch, dest = 4, "tests/unit/<ctx>/"
        else:
            branch, dest = 5, "tests/integration/<ctx>/"

        rows.append(
            {
                "cls": cls.name,
                "name": fn.name,
                "line": info["line"],
                "lines": info["lines"],
                "branch": branch,
                "dest": dest,
                "anchors": sorted(set(anchors))[:3],
                "surfaces": sorted(surfaces),
            }
        )

assert len(rows) == 127, f"expected 127 tests, classified {len(rows)}"

by_dest: dict[str, list] = {}
for r in rows:
    by_dest.setdefault(r["dest"], []).append(r)

print("=== canonical decision tree applied mechanically ===")
for dest in sorted(by_dest):
    rs = by_dest[dest]
    print(f"{dest:28s} tests={len(rs):4d} lines={sum(r['lines'] for r in rs):5d}")
print(f"{'TOTAL':28s} tests={len(rows):4d} lines={sum(r['lines'] for r in rows):5d}")

print()
print("=== branch 3 (regressions) share ===")
b3 = by_dest.get("tests/regressions/", [])
print(f"{len(b3)}/127 = {100*len(b3)/127:.0f}% of tests, "
      f"{sum(r['lines'] for r in b3)}/3088 = {100*sum(r['lines'] for r in b3)/3088:.0f}% of lines")

print()
print("=== NOT branch 3: what survives into unit / integration ===")
for dest in ("tests/unit/<ctx>/", "tests/integration/<ctx>/"):
    for r in sorted(by_dest.get(dest, []), key=lambda r: r["line"]):
        print(f"  {dest:26s} {r['line']:5d} {r['lines']:4d} {r['name']}")

Path(sys.argv[3]).write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
```

### A.4 同 head での実行結果 [実測]

`A2.py` (stderr):

```text
=== surface totals ===
<none>: 1
ast_probe: 1
os_patch: 36
private_symbol: 23
real_fs: 104
source_line: 3
subprocess: 8
trace: 3
tests: 127 in 3 classes
```

> **`real_fs: 104` と §2.3 の「実 tree 96」は別の数え方である。** ここは surface
> 単位の集計で、1 test が複数 surface を持てば各々に計上される。`subprocess` の 8 件は
> すべて `real_fs` でもあるため `96 + 8 = 104` になる。§2.3 は
> `subprocess > real_fs > pure` の順に振り分けた**排他**集計である。
>
> `<none>: 1` は `test_reading_the_ledger_does_not_create_one` — 公開 read
> (`teardown_failures`) だけを呼び、patch も tree 構築もしない唯一の test である。

`A3.py` (stdout 冒頭):

```text
=== canonical decision tree applied mechanically ===
tests/integration/<ctx>/     tests=  53 lines=  812
tests/regressions/           tests=  69 lines= 2200
tests/unit/<ctx>/            tests=   5 lines=   76
TOTAL                        tests= 127 lines= 3088

=== branch 3 (regressions) share ===
69/127 = 54% of tests, 2200/3088 = 71% of lines
```

### A.5 全 127 件の mapping

`surfaces` は A.2 の分類 (`line` = `source_line`、`priv` = `private_symbol`、
`ast` = `ast_probe`)。`分岐` は A.3 の決定木分岐番号、`行き先` は
`reg` = `tests/regressions/` / `unit` = `tests/unit/<ctx>/` /
`int` = `tests/integration/<ctx>/`。

| # | class | test | line | 行 | surfaces | 分岐 | 行き先 |
| ---: | --- | --- | ---: | ---: | --- | ---: | --- |
| 1 | Tracked | `test_mirror_reference_dirs_present` | 111 | 3 | real_fs | 5 | int |
| 2 | Tracked | `test_mirror_reference_files_match_canonical` | 115 | 25 | real_fs | 5 | int |
| 3 | Tracked | `test_mirror_reference_set_is_exactly_the_partial_set` | 141 | 16 | real_fs | 3 | reg |
| 4 | Tracked | `test_mirror_references_are_regular_files` | 158 | 14 | real_fs | 3 | reg |
| 5 | Tracked | `test_mirror_path_has_no_symlinked_component` | 173 | 10 | real_fs | 3 | reg |
| 6 | Tracked | `test_tracked_tree_satisfies_the_contract` | 184 | 4 | real_fs | 5 | int |
| 7 | Tracked | `test_adapter_skill_md_present_and_not_a_canonical_copy` | 189 | 7 | real_fs | 5 | int |
| 8 | Service | `test_clean_tree_passes_and_syncs_idempotently` | 286 | 6 | real_fs | 5 | int |
| 9 | Service | `test_canonical_only_edit_is_caught_and_repaired` | 293 | 15 | real_fs | 5 | int |
| 10 | Service | `test_content_drift_does_not_block_the_write` | 309 | 9 | real_fs | 5 | int |
| 11 | Service | `test_missing_mirror_directory_is_created_by_the_sync` | 319 | 9 | real_fs | 5 | int |
| 12 | Service | `test_sync_never_writes_the_adapter_stub_or_extra_references` | 329 | 9 | real_fs | 5 | int |
| 13 | Service | `test_entry_names_are_compared_losslessly` | 341 | 17 | real_fs | 3 | reg |
| 14 | Service | `test_a_glob_named_entry_does_not_report_unrelated_paths` | 359 | 9 | real_fs | 5 | int |
| 15 | Service | `test_a_newline_named_entry_cannot_forge_a_success_line` | 369 | 10 | real_fs | 5 | int |
| 16 | Service | `test_unpinned_subdirectory_is_an_entry_too` | 380 | 4 | real_fs | 5 | int |
| 17 | Service | `test_a_file_sharing_the_temp_prefix_is_never_deleted` | 387 | 8 | real_fs | 3 | reg |
| 18 | Service | `test_a_directory_sharing_the_temp_prefix_blocks_rather_than_hangs` | 396 | 4 | real_fs | 5 | int |
| 19 | Service | `test_crash_residue_asks_for_a_reviewed_disposition` | 401 | 10 | real_fs | 5 | int |
| 20 | Service | `test_a_concurrent_run_neither_deletes_nor_is_deleted` | 412 | 28 | real_fs | 3 | reg |
| 21 | Service | `test_successful_sync_leaves_no_temp_behind` | 441 | 5 | real_fs | 5 | int |
| 22 | Service | `test_failed_sync_cleans_only_its_own_temp` | 447 | 31 | real_fs | 3 | reg |
| 23 | Service | `test_success_is_not_reported_on_an_unverified_tree` | 479 | 22 | real_fs | 5 | int |
| 24 | Service | `test_written_references_are_mode_644` | 502 | 6 | real_fs | 5 | int |
| 25 | Service | `test_invalid_source_never_offers_the_resync` | 511 | 9 | real_fs | 3 | reg |
| 26 | Service | `test_content_parity_is_skipped_when_the_source_is_invalid` | 521 | 6 | real_fs | 5 | int |
| 27 | Service | `test_symlinked_canonical_reference_is_rejected` | 530 | 14 | real_fs | 3 | reg |
| 28 | Service | `test_symlinked_canonical_directory_is_rejected` | 545 | 7 | real_fs | 5 | int |
| 29 | Service | `test_non_directory_ancestor_is_topology_not_missing_mirror` | 555 | 14 | real_fs | 3 | reg |
| 30 | Service | `test_symlinked_mirror_destination_is_rejected` | 570 | 12 | real_fs | 5 | int |
| 31 | Service | `test_symlinked_pinned_entry_is_rejected_without_writing_through` | 583 | 10 | real_fs | 5 | int |
| 32 | Service | `test_dangling_symlink_entry_is_rejected` | 594 | 4 | real_fs | 5 | int |
| 33 | Service | `test_non_regular_pinned_entries_are_rejected_without_blocking` | 599 | 15 | real_fs | 5 | int |
| 34 | Service | `test_hardlinked_entry_is_replaced_not_written_through` | 615 | 14 | real_fs | 3 | reg |
| 35 | Service | `test_entry_swapped_after_the_type_audit_is_not_read_through` | 632 | 21 | real_fs | 3 | reg |
| 36 | Service | `test_source_parent_swapped_after_audit_writes_no_external_bytes` | 654 | 28 | real_fs | 3 | reg |
| 37 | Service | `test_mirror_parent_swapped_after_audit_writes_nothing_outside` | 683 | 28 | real_fs | 3 | reg |
| 38 | Service | `test_staging_entry_rebound_mid_sync_is_not_swapped_into_place` | 712 | 37 | real_fs | 3 | reg |
| 39 | Service | `test_staging_entry_rebound_to_a_regular_file_is_not_swapped_into_place` | 750 | 40 | real_fs | 3 | reg |
| 40 | Service | `test_ownership_refuses_to_answer_once_the_descriptor_is_closed` | 793 | 36 | priv,real_fs | 5 | int |
| 41 | Service | `test_the_staging_descriptor_still_pins_the_inode_at_every_ownership_question` | 830 | 58 | os_patch,priv,real_fs | 5 | int |
| 42 | Service | `test_a_deferred_write_error_is_reported_before_anything_is_installed` | 889 | 32 | os_patch,real_fs | 3 | reg |
| 43 | Service | `test_source_becoming_unreadable_after_the_walk_is_typed` | 922 | 24 | real_fs | 5 | int |
| 44 | Service | `test_unreadable_canonical_directory_is_a_typed_violation` | 947 | 12 | real_fs | 5 | int |
| 45 | Service | `test_platform_without_the_required_primitives_fails_closed` | 960 | 14 | real_fs | 5 | int |
| 46 | Service | `test_abnormal_topology_does_not_leak_descriptors` | 980 | 16 | real_fs | 3 | reg |
| 47 | Service | `test_repeated_sync_on_an_invalid_tree_does_not_leak_descriptors` | 997 | 10 | real_fs | 5 | int |
| 48 | Service | `test_every_topology_failure_shape_is_descriptor_neutral` | 1008 | 21 | real_fs | 5 | int |
| 49 | Service | `test_entry_swapped_to_a_fifo_after_the_type_audit_does_not_block` | 1032 | 32 | real_fs | 3 | reg |
| 50 | Service | `test_action_time_type_failure_advises_a_recovery_that_converges` | 1065 | 20 | real_fs | 3 | reg |
| 51 | Service | `test_source_swapped_to_a_fifo_is_bounded_in_both_modes` | 1086 | 21 | real_fs | 5 | int |
| 52 | Service | `test_replace_onto_a_directory_is_typed_not_raised` | 1110 | 27 | real_fs | 3 | reg |
| 53 | Service | `test_payload_is_written_in_full_under_injected_short_writes` | 1138 | 22 | os_patch,real_fs | 3 | reg |
| 54 | Service | `test_a_write_that_never_progresses_is_bounded` | 1161 | 21 | os_patch,real_fs | 5 | int |
| 55 | Service | `test_late_type_swaps_all_carry_rule_e_weight` | 1185 | 38 | real_fs | 3 | reg |
| 56 | Service | `test_close_failure_does_not_escape_either_mode` | 1224 | 19 | os_patch,real_fs | 3 | reg |
| 57 | Service | `test_cleanup_failure_is_reported_with_the_primary_failure` | 1244 | 27 | os_patch,real_fs | 3 | reg |
| 58 | Service | `test_staging_close_failure_is_not_reported_as_success` | 1297 | 19 | os_patch,real_fs | 3 | reg |
| 59 | Service | `test_cleanup_leaves_a_foreign_entry_at_the_staging_name` | 1317 | 31 | os_patch,real_fs | 3 | reg |
| 60 | Service | `test_a_transient_cleanup_failure_is_not_reported_as_surviving_residue` | 1349 | 42 | os_patch,real_fs | 3 | reg |
| 61 | Service | `test_entry_deleted_between_observation_and_read_is_missing_not_unreadable` | 1392 | 28 | real_fs | 3 | reg |
| 62 | Service | `test_a_non_oserror_unwinding_the_write_still_releases_the_staging` | 1428 | 16 | os_patch,real_fs | 3 | reg |
| 63 | Service | `test_a_non_oserror_unwind_still_spares_a_foreign_entry` | 1445 | 30 | os_patch,real_fs | 5 | int |
| 64 | Service | `test_an_unreadable_staging_name_at_swap_time_releases_the_staging` | 1476 | 36 | os_patch,real_fs | 3 | reg |
| 65 | Service | `test_a_staging_entry_gone_before_the_swap_is_reported_without_residue` | 1513 | 29 | real_fs | 3 | reg |
| 66 | Service | `test_an_unprovable_staging_identity_never_unlinks` | 1543 | 39 | os_patch,real_fs | 3 | reg |
| 67 | Service | `test_a_close_that_unwinds_still_releases_the_staging` | 1583 | 34 | os_patch,real_fs | 3 | reg |
| 68 | Service | `test_a_close_unwind_never_closes_a_reused_descriptor_number` | 1618 | 57 | os_patch,real_fs | 3 | reg |
| 69 | Service | `test_a_close_unwind_keeps_the_primary_exception` | 1676 | 43 | os_patch,real_fs | 5 | int |
| 70 | Service | `test_the_directory_walk_never_closes_a_reused_descriptor_number` | 1720 | 42 | os_patch,real_fs | 3 | reg |
| 71 | Service | `test_a_walk_close_that_unwinds_leaks_no_descriptor` | 1763 | 46 | os_patch,real_fs | 5 | int |
| 72 | Service | `test_a_failing_add_note_does_not_replace_the_primary` | 1810 | 42 | os_patch,real_fs | 3 | reg |
| 73 | Service | `test_a_failing_cleanup_does_not_replace_the_primary` | 1853 | 32 | os_patch,real_fs | 3 | reg |
| 74 | Service | `test_a_raising_release_does_not_take_the_close_with_it` | 1905 | 50 | os_patch,real_fs | 3 | reg |
| 75 | Service | `test_the_staging_release_always_precedes_the_staging_close` | 1988 | 51 | os_patch,real_fs | 3 | reg |
| 76 | Service | `test_the_walk_keeps_the_first_close_failure` | 2040 | 32 | os_patch,real_fs | 3 | reg |
| 77 | Service | `test_a_typed_cleanup_failure_is_recorded_not_discarded` | 2073 | 31 | os_patch,real_fs | 3 | reg |
| 78 | Service | `test_a_typed_close_failure_is_recorded_not_discarded` | 2105 | 30 | os_patch,real_fs | 5 | int |
| 79 | Service | `test_an_interrupt_during_teardown_outranks_the_primary` | 2136 | 26 | os_patch,real_fs | 3 | reg |
| 80 | Service | `test_an_interrupt_while_recording_still_releases_the_staging_entry` | 2163 | 53 | os_patch,real_fs | 3 | reg |
| 81 | Service | `test_a_later_control_flow_failure_is_recorded_not_dropped` | 2217 | 45 | os_patch,real_fs | 3 | reg |
| 82 | Service | `test_teardown_continues_when_recording_a_secondary_is_interrupted` | 2263 | 26 | priv | 3 | reg |
| 83 | Service | `test_control_flow_priority_keeps_the_first_and_records_the_rest` | 2290 | 26 | priv | 3 | reg |
| 84 | Service | `test_a_broken_note_still_leaves_the_cleanup_failure_reachable` | 2317 | 73 | os_patch,real_fs | 3 | reg |
| 85 | Service | `test_a_secondary_that_cannot_be_stringified_is_still_retained` | 2391 | 45 | priv | 3 | reg |
| 86 | Service | `test_an_interrupt_while_recording_a_later_failure_is_retained` | 2437 | 30 | priv | 3 | reg |
| 87 | Service | `test_the_ledger_survives_a_hostile_dict_descriptor` | 2503 | 32 | priv | 3 | reg |
| 88 | Service | `test_the_carrier_key_is_not_an_attribute_name` | 2536 | 30 | priv | 3 | reg |
| 89 | Service | `test_the_pickle_boundary_depends_on_the_entries` | 2567 | 29 | priv | 3 | reg |
| 90 | Service | `test_a_value_at_the_carrier_key_is_never_replaced` | 2597 | 47 | priv | 3 | reg |
| 91 | Service | `test_reading_the_ledger_does_not_create_one` | 2645 | 11 | - | 3 | reg |
| 92 | Service | `test_each_occurrence_is_one_ledger_entry` | 2657 | 29 | priv | 3 | reg |
| 93 | Service | `test_a_carrier_failure_never_skips_a_remaining_action` | 2687 | 52 | priv | 3 | reg |
| 94 | Service | `test_an_arrival_survives_a_failure_before_it_reaches_the_queue` | 2779 | 43 | priv,line,trace | 3 | reg |
| 95 | Service | `test_a_nested_interrupt_never_skips_a_remaining_action` | 2972 | 117 | priv,line,trace | 3 | reg |
| 96 | Service | `test_an_interrupt_during_the_final_admission_still_counts` | 3090 | 55 | priv | 3 | reg |
| 97 | Service | `test_an_exhausted_retry_still_reaches_the_queue` | 3146 | 37 | priv | 3 | reg |
| 98 | Service | `test_retention_survives_an_interrupt_at_a_commit_boundary` | 3211 | 51 | priv,line,trace | 3 | reg |
| 99 | Service | `test_the_final_flush_surfaces_the_control_flow_it_hits` | 3263 | 36 | priv | 3 | reg |
| 100 | Service | `test_a_carrier_that_never_recovers_gives_up_the_record_only` | 3300 | 23 | priv | 4 | unit |
| 101 | Service | `test_the_ledger_survives_a_primary_that_refuses_attributes` | 3324 | 27 | priv | 4 | unit |
| 102 | Service | `test_cleanup_helper_runs_exactly_once_when_it_raises` | 3352 | 30 | os_patch,real_fs | 3 | reg |
| 103 | Service | `test_replace_failure_is_classified_by_what_actually_happened` | 3383 | 23 | os_patch,real_fs | 3 | reg |
| 104 | Service | `test_replace_onto_a_changed_type_still_says_so` | 3407 | 16 | real_fs | 5 | int |
| 105 | Service | `test_capability_manifest_is_exactly_the_primitives_the_module_calls` | 3469 | 24 | ast,priv | 3 | reg |
| 106 | Service | `test_each_required_capability_individually_fails_closed` | 3494 | 29 | priv,real_fs | 5 | int |
| 107 | Service | `test_a_scandir_whose_failure_is_deferred_still_fails_closed` | 3524 | 24 | os_patch,real_fs | 5 | int |
| 108 | Service | `test_an_interrupt_during_the_probe_is_not_a_missing_capability` | 3549 | 11 | os_patch | 4 | unit |
| 109 | Service | `test_a_supported_host_is_not_refused_by_a_stale_advertisement` | 3561 | 20 | os_patch,real_fs | 3 | reg |
| 110 | Service | `test_the_exact_linux_312_advertisement_is_accepted` | 3582 | 8 | os_patch | 4 | unit |
| 111 | Service | `test_the_probe_writes_nothing_and_leaks_no_descriptor` | 3591 | 15 | real_fs | 5 | int |
| 112 | Service | `test_the_probe_anchor_is_not_a_directory` | 3607 | 7 | priv | 4 | unit |
| 113 | Service | `test_a_probe_that_cannot_be_set_up_fails_closed` | 3615 | 16 | os_patch,real_fs | 5 | int |
| 114 | Service | `test_unreadable_canonical_reference_is_a_typed_violation` | 3645 | 18 | real_fs | 3 | reg |
| 115 | Service | `test_unreadable_mirror_directory_is_a_typed_violation` | 3664 | 12 | real_fs | 5 | int |
| 116 | Service | `test_diagnostics_carry_no_host_absolute_paths` | 3677 | 7 | real_fs | 5 | int |
| 117 | Service | `test_source_swapped_after_preflight_is_fail_closed` | 3687 | 22 | real_fs | 5 | int |
| 118 | Cli | `test_wrapper_exists_and_is_executable` | 3740 | 3 | real_fs | 5 | int |
| 119 | Cli | `test_wrapper_carries_no_mirror_logic` | 3744 | 11 | real_fs | 5 | int |
| 120 | Cli | `test_check_and_sync_round_trip` | 3756 | 10 | real_fs,subprocess | 5 | int |
| 121 | Cli | `test_check_reports_a_violation_and_writes_nothing` | 3767 | 8 | real_fs,subprocess | 5 | int |
| 122 | Cli | `test_help_exits_zero` | 3776 | 5 | real_fs,subprocess | 5 | int |
| 123 | Cli | `test_unknown_argument_exits_64` | 3782 | 5 | real_fs,subprocess | 5 | int |
| 124 | Cli | `test_repo_cannot_be_redirected_by_operator_argv` | 3788 | 19 | real_fs,subprocess | 3 | reg |
| 125 | Cli | `test_repo_env_is_overwritten_by_the_wrapper` | 3808 | 17 | real_fs,subprocess | 5 | int |
| 126 | Cli | `test_module_run_without_the_wrapper_refuses` | 3826 | 21 | real_fs,subprocess | 5 | int |
| 127 | Cli | `test_wrapper_targets_its_own_repo_not_the_cwd` | 3848 | 14 | real_fs,subprocess | 5 | int |

**この表は §5.0 の 69 / 53 / 5 と §2.3 の内訳の両方の原資料である。**
配置の確定は §5.1 の裁定待ちであり、`行き先` 列は決定木の**出力**であって
採用した配置ではない。

---

## 参照

- Redmine #14660 (本 Task) / #14592 (親 US) / #14580 / #14651 / #14652 / #14655 / #14656
- `vibes/docs/logics/tests-placement-discovery-policy.md` — 配置決定木 / discovery 不変
- `vibes/docs/logics/refactor-split-strategy.md` — `## Characterization Strategy` / `## Move Commit Rules`
- `vibes/docs/logics/module-health-gate.md` — 閾値 / scope / allowlist 契約
- `vibes/docs/logics/skill-distribution.md` — Mirror Contract
- `vibes/docs/specs/bounded-context-map.md` — `## Redmine-numbered package path map (#12622)`
