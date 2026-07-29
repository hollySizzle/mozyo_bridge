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

#### rail は 4 本ある [実測]

同じ失敗でも、**どの rail を通ったかで出口が変わる**。rail の数は目視ではなく
`_replace_one` の top-level 文を AST で列挙して確定した (Appendix A.6 に導出器):

```
L579  Assign   payload, failure = self._read_bound(source_fd, name)
L580  If         → L581 return                          (W0)
L591  Assign   temp_name = f"...{os.urandom(8).hex()}.tmp"
L593  Try      try: temp = _OwnedDescriptor(os.open(...))
               except OSError → L603 return             (W1)
L612  Assign   ownership = _StagingOwnership(temp)
L736  Try      try: problems = install()
               except BaseException → L749 raise interrupt / L750 raise
L751  Return   return problems + self._close_staging(temp, subject)
```

**L736 の `try` が覆うのは `install()` の呼び出しだけである。** その前 (L579–L593) と
その後 (L751) はいずれも保護域の外にある。

| rail | 範囲 | teardown | 出口 |
| --- | --- | --- | --- |
| **R-A** pre-staging | L579 〜 L593 | **無し** (staging 未作成なので不要) | 非 `OSError` は**そのまま伝播**。`OSError` は W0 / W1 の typed return |
| **R-B** install unwind | L736 `try` の内側 | `_teardown_during(primary, release, temp.close)` | 例外を再送出 (W13) |
| **R-C** typed normal return | `install()` が値を返した | `install()` 内の `release()` | L751 で `_close_staging` を足して返す (W2–W12 + W14) |
| **R-D** post-install close unwind | L751 の評価中 | **無し** | 非 `OSError` が**そのまま伝播** |

rail ごとに、同じ失敗の出方が変わる:

| | R-C (typed normal return) | R-B (install unwind) | R-A / R-D (保護域外) |
| --- | --- | --- | --- |
| cleanup を呼ぶ主体 | `install()` 内の `release()` | `_teardown_during` | **誰も呼ばない** |
| close を呼ぶ主体 | `_close_staging` | `temp.close` (teardown action) | R-A: 未作成 / R-D: `_close_staging` 自身が unwind 源 |
| release の violation tuple | **typed violation になる** | **ledger 止まり** (§1.4 returned failure channel) | — |
| close 失敗 | **`W`/`WRITE_FAILED` "could not be closed cleanly"** | **ledger 止まり** | R-D: 非 `OSError` は typed 化されず伝播 |
| `_replace_one` の出口 | violation tuple を `sync()` へ | 例外を再送出 | 例外を再送出 |

**R-D は実在する経路である [実測]。** `_close_quietly` は `except OSError` のみなので、
`RuntimeError` や control-flow は `_close_staging` から抜ける。
`test_a_close_that_unwinds_still_releases_the_staging` (1583) が実際に駆動している —
`legacy_mirror_sync.os.close` を staging fd で `RuntimeError` を投げるよう差し替え、
`sync()` に `assertRaises(RuntimeError)` を立てる。content drift 前提なので
`install()` は W12 で成功し、**L751 で unwind する**。

以下の表は **R-C (typed normal return)** の event → 出口である。R-B は W13、
R-A / R-D は上の rail 表を読む。

> 前 revision は rail を **2 本**と書き、W13 を「任意時点で unwind」、W14 を
> 「W0–W12 のいずれの後」としていた。どちらも過大である — R-A / R-D が抜けており、
> W0 / W1 は `_close_staging` に到達しない。rail の数を source から導出せず
> 決め打ちしたのが原因なので、上記は AST 列挙から組み直した。

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
| W13 | **`install()` の内側で** `BaseException` unwind (**R-B のみ**) | 再送出 | **無し** — typed violation を返さない | `_teardown_during(primary, release, temp.close)` | **無し** — release も close も ledger 止まり (§1.4) |
| W14 | **R-C のみ**、**W2–W12** のいずれの後 (W0 / W1 は L581 / L603 で直接 return するので到達しない) | — | close 失敗 (`OSError`) 時 `W` / `WRITE_FAILED` "could not be closed cleanly"。非 `OSError` は R-D として伝播 | `_close_staging` | — |

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
ではない。** この 2 つは一致しない、しかも**両方向に**:

- W6 (`FOREIGN`) は `release()` を通らないので `CLEANUP_FAILED` を発行しないが、
  foreign entry は staging 名に残る。→ violation で判定すると**取りこぼす**。
- `UNREADABLE` は `CLEANUP_FAILED` を発行するが、detail は source 自身が
  **"may still be present"** と書くとおり **存在が unknown** である。
  → violation で判定すると**過剰に非収束と断じる**。

残留は 3 値である。**known-present / unknown / absent** を分ける:

| 直前の終了状態 | staging 名の残留 | 再実行は収束するか | 根拠 |
| --- | --- | --- | --- |
| preflight refuse (A–E, P) | absent | **しない** — tree を直すまで同じ refuse | `blocks_write` は preflight で評価される |
| W12 成功 | absent (rename が消費) | する (idempotent) | `test_clean_tree_passes_and_syncs_idempotently` |
| W0 / W1 中断 | absent (未作成) | する | mirror 不変 |
| W2–W5, W7–W11 かつ release が `CONFIRMED` → unlink 成功 / `FileNotFoundError` | absent | する | staging 削除済。既に replace 済の name は content 一致で no-op |
| W2–W5, W7–W11 かつ release が `UNPROVEN` / `CONFIRMED`→unlink `OSError` | **known-present** (自分の残骸。`lstat` は成功している) | **しない (operator 介入が要る)** | 次回 rule `D` / `UNPINNED_ENTRY` → `blocks_write` |
| W2–W5, W7–W11 かつ release が **`UNREADABLE`** | **unknown** (`lstat` 自体が失敗した) | **次 run の audit 次第** | audit が ABSENT を観測すれば収束する。entry を観測できれば rule `D` で止まる。**この run では判定できない** |
| **W6 (`FOREIGN`)** | **known-present (他者の entry)** | **しない (operator 介入が要る)** | `CLEANUP_FAILED` は**出ない**が、staging 名の entry は `MIRRORED_REFERENCES` 外なので同じく rule `D` → `blocks_write` |
| ループ途中の中断 (typed) | 上記いずれかに従う | staging が absent なら する | 先行 name は install 済だが `_replace_one` は content べき等 |

**unknown を「残っている」に丸めない [実測]。** `UNREADABLE` は `os.lstat` が
`FileNotFoundError` 以外の `OSError` で失敗した状態であり、entry の有無そのものが
観測できていない。次 run の audit は同じ `lstat` を試みるので、条件が解消していれば
ABSENT を観測して収束しうる。`CLEANUP_FAILED` の有無で一括判定すると、この場合を
不必要に「operator 介入が要る」と報告することになる。
| **R-B** install unwind (W13) | `release` の結果次第 (ledger にしか出ない) | 次 run の audit が見るものだけで決まる | `_teardown_during` が `release` を走らせるが、成否は ledger 止まり |
| **R-A** pre-staging unwind | absent (staging 未作成) | する | 何も作っていないので mirror 不変 |
| **R-D** post-install close unwind | **W12 の後なら absent** (rename が消費済) | する | 残骸は無い。伝播するのは close の例外だけ |

**保護域外の 3 rail は他の行と前提が違う [実測]。** `_replace_one` が例外を再送出すると
`sync()` の `with` を抜けて呼び出し元へ伝播するので、`sync()` は `(1, (), lines)` を
**返さない**。したがって R-A / R-B / R-D に共通して:

- operator は `report_lines()` を一切見ない。`clear_residue` も `disposition_unpinned` も
  この run では提示されない。
- retry の可否は、この run の出力ではなく **次 run の audit が staging 名に何を見るか**
  だけで決まる。

rail ごとの違い:

- **R-A** — staging を作る前なので残骸は無い。`_teardown_during` も要らない。
- **R-B** — `release` は teardown 経由で走るので片付くことはあるが、その成否は
  **ledger にしか無い**。`teardown_failures(primary)` を読む caller がいなければ
  誰も知らない。残留は上の 3 値表に従う。
- **R-D** — `install()` は既に返っている。W12 (成功) の後なら rename が staging を
  消費済みなので **absent**。W2–W11 の後なら `install()` 内の `release()` が既に
  走っており、残留はその結果に従う。**`_close_staging` 自身が unwind 源**であって、
  片付けに失敗したわけではない。

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

**この導出器が答えるのは surface だけである。§5.0 の配置は導出していない。**

- **導出 (機械)** — test 総数 **127**、および surface 分類 **23 / 96 / 8** (§2.3)。
  Appendix A.2 が source から機械的に出す。ただし A.2 は**下限の検出器**であり、
  §5.0 で 1 件 (3469) の false negative を読んで特定している。
- **著者宣言 (機械導出しない)** — §5.0 の配置。landed policy の裁定 2 / 3 が
  「分岐 2 と分岐 3 は *test が存在する理由* を問うので code に現れない」として
  著者宣言を要求する。分岐 4 / 5 も判定の正本は各 test を読むことであって A.2 の
  出力ではない。

> **`69 / 53 / 5` は historical invalid output である。** R1–R4 は分岐 3 を
> 「docstring が defect anchor を持つか」で機械判定して この 3 値を得ていたが、
> **裁定 1 によりこの述語は無効**である (repo 全体で 95–100% 発火し識別力を持たず、
> 分岐 3 の主語は method でなく file)。Appendix A.3 は再現性のために残してあるが
> **その分岐 3 arm は無効**で、かつ **分岐 2 を評価していない**。詳細は §5.1 と
> A.3 冒頭の注記を読む。**この 3 値を配置として引用しないこと。**

> **再現手順は Appendix A にある。** 導出器の全文、同 head での実行結果、および
> 127 件全件の mapping を載せてある。**確定した配置は A.5 の `宣言` 列**であり、
> 同表の `分岐` / `行き先` 列 (A.3 出力) ではない。

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
(3352)。これは `_release_staging` の二重実行 (j#90467 R9-F3) を由来に持つ。

> **最終 disposition は分岐 5 (integration) である** [§5.0 分岐 3 の宣言]。
> 由来は defect だが、主張しているのは single-shot guard の**契約**
> (「the guard is what keeps that at one call」) なので R3-b を満たさない。
> R5 まで本節は「regressions への移設候補」と書いたままで §5.0 の宣言と
> 接続していなかった。**provenance は分岐 3 の根拠にならない** (裁定 1)。

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

配置は `tests-placement-discovery-policy.md` **のみ**が決める。同 doc の
`## #14660 legacy mirror family 裁定 (Redmine #14662 R4)` が本 family の基準を固定し、
本節はその基準に対する **#14660 の著者宣言**である。基準を本 doc で作らない。

### 5.0 配置 matrix (確定) [著者宣言]

**根拠 policy:** `tests-placement-discovery-policy.md` `## #14660 legacy mirror family 裁定
(Redmine #14662 R4)` (`origin/main-next@6b718673` に land 済、required CI success)。
基準は同節の 5 分類表であり、本節はその基準に対する **#14660 の著者宣言**である。

裁定が定めるとおり、**分岐 2 (scenarios) と分岐 3 (regressions) は判断分岐**であり
機械導出しない。分岐 4 / 5 は family 限定の literal rule (**実外部 collaborator 0 / 1 以上**、
subject は数えない) で判定するが、**判定の正本は各 test を読むことであって
Appendix A.2 の構文的検出ではない**。

| 行き先 | tests | 行 |
| --- | ---: | ---: |
| `tests/scenarios/` (分岐 2) | **7** | 78 |
| `tests/regressions/` (分岐 3) | **4** | 127 |
| `tests/unit/e_130_governance_distribution/` (分岐 4) | **21** | 764 |
| `tests/integration/e_130_governance_distribution/` (分岐 5) | **95** | 2,119 |
| **計** | **127** | **3,088** |

**検算 (裁定が渡した唯一の無条件不変条件):**
`unit + scenarios + regressions + integration = 21 + 7 + 4 + 95 = 127` ✓

全 127 件の per-test 割当は **Appendix A.5** の **`宣言` 列**にある。

#### 分岐 1 (support)

`_MirrorTreeFixture` (現行 198–279、82 行) を `tests/support/legacy_mirror_tree_fixture.py`
へ逐語移動する。裁定 5 の support 閾値「**2 つ以上の移設先 test module** が使う」を満たす —
移設後の integration / scenarios / regressions のいずれからも使われる。**test ではないので
127 の分配先にならない** (partition 恒等式に影響しない)。

#### 分岐 2 (scenarios) — 7 件を宣言する

**宣言:** `LegacyMirrorWrapperCliTest` のうち **wrapper を実行する 7 件**。

| test | 行 | 行数 |
| --- | ---: | ---: |
| `test_check_and_sync_round_trip` | 3756 | 10 |
| `test_check_reports_a_violation_and_writes_nothing` | 3767 | 8 |
| `test_help_exits_zero` | 3776 | 5 |
| `test_unknown_argument_exits_64` | 3782 | 5 |
| `test_repo_cannot_be_redirected_by_operator_argv` | 3788 | 19 |
| `test_repo_env_is_overwritten_by_the_wrapper` | 3808 | 17 |
| `test_wrapper_targets_its_own_repo_not_the_cwd` | 3848 | 14 |

**根拠 — 境界は「wrapper (shell module) → CLI (python module) を越えるか」である。**

これらは `sh scripts/sync_legacy_project_skill.sh` を operator と同じ形で起動する。
wrapper は mirror logic を持たず、`--help` も unknown argument も**自分では処理せず**
`exec "$PYTHON" -m "$module" "$@"` で CLI へ渡す [code fact: `scripts/sync_legacy_project_skill.sh`]。
したがって 7 件はいずれも **shell module → python CLI module** の境界を越える
operator の invocation contract であり、`release check drift` が依存する面そのもの
である。class docstring も「operator-facing contract, black-box」と宣言している。
裁定 3 のとおり `/` は **OR** なので、単一 bounded context
(`e_130_governance_distribution`) に閉じていても分岐 2 は成立する。

> **境界を「service まで到達するか」に置いてはならない。** 7 件のうち `--help` /
> unknown argument / `--repo` 拒否の 3 件は CLI の argument 段で refuse し、
> application service を構築しない。それでも operator が辿る 2 module の入口経路は
> 越えている。landed policy `### scenarios` が課すのは「複数 module または複数
> bounded context をまたぐ operator / coordinator 視点の end-to-end workflow」で
> あって、service 到達でも subprocess surface でもない。

**分岐 2 に該当しないと宣言したもの (裁定は全 127 件の評価を要求する):**

- **`test_module_run_without_the_wrapper_refuses` (3826)** — 同 class かつ subprocess
  だが、**wrapper を意図的に迂回**して `python -m ... cli_legacy_mirror_sync --check` を
  直接起動する。`MOZYO_LEGACY_MIRROR_REPO_ROOT` 不在で CLI は service 構築**前**に
  64 を返すので、越える module 境界が無い (単一 CLI adapter の禁止入口 contract)。
  実 subprocess collaborator を持つので → **分岐 5**。
  *R5 まで本 doc はこれを分岐 2 に入れ、「8 件すべてが service → domain → 実 tree を
  通す」と書いていた。membership も根拠も誤りだったので両方訂正した。*
- `test_wrapper_exists_and_is_executable` (3740) / `test_wrapper_carries_no_mirror_logic`
  (3744) — 同 class だが **wrapper を実行しない**。前者は tracked file の mode、後者は
  wrapper の source text に対する guardrail であり、通し受入ではない。→ 分岐 5。
- `LegacyProjectSkillMirrorTest` の 7 件 — tracked tree が contract を満たすことの
  guardrail であって、operator が辿る workflow を通しで駆動していない。→ 分岐 5。
- `LegacyMirrorSyncServiceTest` の 110 件 — in-process で `service.check()` /
  `service.sync()` を呼ぶ。application service **1 つの公開 API** に対する検証であり、
  operator の入口ではない。→ 分岐 3 / 4 / 5。

#### 分岐 3 (regressions) — 2 file / 4 件を宣言する

適用した判定 (裁定 2 の R3-a ∧ R3-b ∧ R3-c、file 単位):

> **その test を消したとき、検出できなくなるのは「特定の過去の bug の再来」か、
> 「module の約束が破れたこと」か。** 前者だけの file が分岐 3 に該当する。

| 移設先 file | test | 行 | 行数 |
| --- | --- | ---: | ---: |
| `tests/regressions/test_issue_14651_capability_advertisement.py` | `test_a_supported_host_is_not_refused_by_a_stale_advertisement` | 3561 | 20 |
| 〃 | `test_the_exact_linux_312_advertisement_is_accepted` | 3582 | 8 |
| `tests/regressions/test_issue_14580_reused_descriptor_number_close.py` | `test_a_close_unwind_never_closes_a_reused_descriptor_number` | 1618 | 57 |
| 〃 | `test_the_directory_walk_never_closes_a_reused_descriptor_number` | 1720 | 42 |

- **R3-a**: 前者は「stale な `os.supports_dir_fd` advertisement が supported host を
  拒否する」という単一症状 (#14651)。後者は「unwind する close が番号の所有を残し、
  後続の `finally` が再利用された番号を閉じる」という単一症状 (#14580 の R11-F1 /
  R12-F1。R12-F1 の docstring 自身が「R11-F1 が staging descriptor で直した**同じ
  defect**」と書いている)。
- **R3-b**: 4 件とも「その bug が戻らない」ことだけを主張する。前者 2 件は歴史的な
  入力 (CPython 3.12 Linux の advertisement) を名指し、後者 2 件は
  「reused descriptor number を閉じない」という defect の署名を測る。
- **R3-c**: `<id>` は defect を修正した issue = 14651 / 14580。同一 issue の pin を
  同一 file に置いている。命名は必要条件を満たす (filename の `<id>` 1 つ + module
  docstring が同じ `<id>` を名指す)。

**分岐 3 に該当しないと宣言した主要な候補と理由:**

| test | 行 | 宣言 |
| --- | ---: | --- |
| `test_ownership_refuses_to_answer_once_the_descriptor_is_closed` | 793 | #14652 由来だが、主張は `_StagingOwnership` の**契約** (pin が無ければ答えない)。R3-b 不成立 |
| `test_the_staging_descriptor_still_pins_the_inode_at_every_ownership_question` | 830 | 同上。descriptor lifetime の構造的不変条件 = 契約 |
| `test_cleanup_helper_runs_exactly_once_when_it_raises` | 3352 | 主張は single-shot guard の**契約** (「the guard is what keeps that at one call」)。R3-b 不成立 |
| `test_capability_manifest_is_exactly_the_primitives_the_module_calls` | 3469 | manifest と実 call 面の一致 = **契約**。両方向の drift を見る |

**provenance anchor (`j#NNNNN` / `RN-FN` / `Redmine #NNNNN`) は上記いずれの根拠にも
使っていない。** 裁定 1 のとおり anchor は repo 全体の普遍的な記録 convention であり、
bucket 間の識別力を持たない。**R4 まで本 doc が採っていた「anchor = 分岐 3」の導出
(69 / 53 / 5) は無効である** — §5.1 と Appendix A.3 に撤回を明記した。

#### 分岐 4 / 5 — 実外部 collaborator の数で分ける

family 限定の literal rule: **unit = 実外部 collaborator 0** (subject は数えない) /
**integration = 1 以上**で hermetic。実 filesystem は実外部 collaborator として数える。

分岐 4 (unit) は **21 件**:

| test | 行 | 行数 |
| --- | ---: | ---: |
| `test_teardown_continues_when_recording_a_secondary_is_interrupted` | 2263 | 26 |
| `test_control_flow_priority_keeps_the_first_and_records_the_rest` | 2290 | 26 |
| `test_a_secondary_that_cannot_be_stringified_is_still_retained` | 2391 | 45 |
| `test_an_interrupt_while_recording_a_later_failure_is_retained` | 2437 | 30 |
| `test_the_ledger_survives_a_hostile_dict_descriptor` | 2503 | 32 |
| `test_the_carrier_key_is_not_an_attribute_name` | 2536 | 30 |
| `test_the_pickle_boundary_depends_on_the_entries` | 2567 | 29 |
| `test_a_value_at_the_carrier_key_is_never_replaced` | 2597 | 47 |
| `test_reading_the_ledger_does_not_create_one` | 2645 | 11 |
| `test_each_occurrence_is_one_ledger_entry` | 2657 | 29 |
| `test_a_carrier_failure_never_skips_a_remaining_action` | 2687 | 52 |
| `test_an_arrival_survives_a_failure_before_it_reaches_the_queue` | 2779 | 43 |
| `test_a_nested_interrupt_never_skips_a_remaining_action` | 2972 | 117 |
| `test_an_interrupt_during_the_final_admission_still_counts` | 3090 | 55 |
| `test_an_exhausted_retry_still_reaches_the_queue` | 3146 | 37 |
| `test_retention_survives_an_interrupt_at_a_commit_boundary` | 3211 | 51 |
| `test_the_final_flush_surfaces_the_control_flow_it_hits` | 3263 | 36 |
| `test_a_carrier_that_never_recovers_gives_up_the_record_only` | 3300 | 23 |
| `test_the_ledger_survives_a_primary_that_refuses_attributes` | 3324 | 27 |
| `test_an_interrupt_during_the_probe_is_not_a_missing_capability` | 3549 | 11 |
| `test_the_probe_anchor_is_not_a_directory` | 3607 | 7 |

**A.2 の構文的検出を鵜呑みにせず読み直して 1 件動かした [実測]。**
`test_capability_manifest_is_exactly_the_primitives_the_module_calls` (3469) は A.2 が
`real_fs` を付けていないが、`Path(legacy_mirror_sync.__file__).parent` から
`.glob()` / `.read_text()` で **module の source を disk から読む**。実外部
collaborator が 1 以上なので **分岐 5**である。裁定 4 が「判定は各 test を読んで行う」
「A.2 は候補抽出と diagnostic」と書いている、まさにその形の false negative だった。

残る **95 件が分岐 5 (integration)**。実 tree を建てる 96 件から分岐 3 へ出た 3 件
(1618 / 1720 / 3561) を引き、A.2 が pure と誤判定した 3469 と、分岐 2 から外した
3826 を足した数である。

#### diagnostic として使った surface 集計

裁定 5 のとおり **surface 集計は acceptance invariant ではなく調査の trigger** である。
本宣言に対して trigger を引いた結果:

- A.2 が `real_fs` / `subprocess` とした test で unit に置いたものは **0 件**。
- 逆向き (A.2 が pure とした test を unit 以外に置いた) は **2 件** — 3582 (分岐 3)、
  3469 (分岐 5)。3582 は分岐 3 が分岐 4 より先だからで正常、3469 は上記の A.2
  false negative である。**どちらも特定済み**。

### 5.1 R4 までの導出 (69 / 53 / 5) の撤回

R4 まで本 doc は、分岐 3 を「docstring が defect anchor を持つか」で機械判定し、
`regressions 69 / integration 53 / unit 5` を得ていた。**この導出は無効である。**

裁定 1 [出所: #14662 j#92449 / Review j#92458、policy `### regressions`] の実測:

- `tests/**` の既配置 548 file に同じ述語を当てると **全 bucket で 95–100% 発火**し、
  bucket 間の識別力を持たない。
- method 単位で anchor を持つ 247 件のうち **134 件 (54%) が既に regressions 以外**。
- 分岐 3 の主語は policy 上 **test method ではなく file** (「1 ファイル = 1 つの修正済み
  症状」)。file 単位の規則を method 単位の述語へ読み替えたことが誤適用の発生点。

したがって 69 / 53 / 5 は**配置 matrix ではない**。撤回し、§5.0 の著者宣言で置き換えた。
Appendix A.3 の分岐 3 arm も同時に無効化してある (A.3 冒頭の注記)。


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

**この表は A.2 の構文的検出による集計であり、配置の正本ではない。**
`E-pure (capability probe) 4` には
`test_capability_manifest_is_exactly_the_primitives_the_module_calls` (3469) が
含まれるが、§5.0 のとおり同 test は module source を disk から読むので**実際には
実外部 collaborator を持つ**。A.2 の false negative であり、配置では分岐 5 に置いた。

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
| D2 | **repository 全体**の discovery 件数が移設前後で一致 | `python -m unittest discover -s tests -v` (正本、文字列を変えない) | **各移設 Task が自 base で測る `N`** |

**D2 に特定の数値を固定しない [policy correction]。** 本 doc は R1–R4 で
`D2 = 13,207` と書いていたが、これは **本 characterization 自身の base
`fef86cac` で測った snapshot** であって恒久不変条件ではない。`N` は本 family と
無関係な test の増減でも動く — 実際 policy doc の base `dd62e957` では同じ command が
**13,343** を返す [出所: `tests-placement-discovery-policy.md` `### 移設の検算` の
policy correction]。

したがって恒久に残るのは **数値ではなく command** である:

- **D1** = 本 family の test 数 **127**。family 内で閉じた定数であり、§5.0 の
  partition 恒等式と同じ根拠を持つ。
- **D2** = 各物理移設 Task が**自身の exact pre-move base** で
  `unittest.defaultTestLoader.discover('tests').countTestCases()` を測り、
  post-move が**同じ値**であることを検証する。

本 doc の base での実測値 **13,207** は、この characterization の snapshot として
Appendix A.4 と併せて残す (再現の起点であって、移設 Task の目標値ではない)。

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

### 5.5 移設先 module の確定 [著者宣言]

§5.0 の宣言を **exact path** へ落とす。Task の changed-path ownership (§7) は
この表を参照する — 「それを使う module」のような未解決の表現を残すと、dispatch 時に
ownership conflict を判定できない。

`os_patch` 列は、その module が含む「`os` primitive を注入する test」の数である。
T6 (共有 fault schedule fake の新規作成) が触る consumer は**この列が非 0 の module
に限られる**ので、T3 / T4 との交差はこの表から判定できる。

| 行き先 | tests | 行 | module | os_patch |
| --- | ---: | ---: | --- | ---: |
| unit | 19 | 746 | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_owned_descriptor_teardown.py` | 0 |
| unit | 2 | 18 | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_platform_capability_probe.py` | 1 |
| scenarios | 7 | 78 | `tests/scenarios/test_legacy_mirror_wrapper_cli.py` | 0 |
| regressions | 2 | 99 | `tests/regressions/test_issue_14580_reused_descriptor_number_close.py` | 2 |
| regressions | 2 | 28 | `tests/regressions/test_issue_14651_capability_advertisement.py` | 2 |
| integration | 29 | 1,033 | `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_fault_injection.py` | 29 |
| integration | 51 | 864 | `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_sync.py` | 0 |
| integration | 7 | 79 | `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_tracked_tree.py` | 0 |
| integration | 3 | 35 | `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_wrapper_guardrails.py` | 0 |
| integration | 5 | 108 | `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_platform_capability_probe_io.py` | 2 |
| **計** | **127** | **3,088** | **10 module** | **36** |

- **basename 衝突 0** を機械確認済み (policy `### module 名の一意性`)。
- module 分割の基準は **subject** であり、行位置ではない:
  - `test_platform_capability_probe*.py` — **`platform_capabilities` に触れる** test
    (unit 2 / integration 5)。判定は「test 本体または同 class helper が
    `platform_capabilities` / `missing_platform_capabilities` を参照するか」で、
    source から機械導出できる。
  - `test_legacy_mirror_fault_injection.py` — probe 以外で `os` primitive を注入する
    29 件 (E-inject)。
  - `test_legacy_mirror_sync.py` — 残りの service 契約 51 件 (audit / check / sync /
    report の結線。注入なし)。
  - `test_legacy_mirror_tracked_tree.py` — tracked tree guardrail 7 件。
  - `test_legacy_mirror_wrapper_guardrails.py` — wrapper を実行しない 2 件 +
    direct-module refusal 1 件。

> **R7 まで probe module を行境界 (`L >= 3424`) で切っていた。** 3424 は
> `--- R7-F4 / #14651: the capability probe ... ---` の section marker だが、その後ろには
> `--- R6-F3: unreadable state is typed ---` と `--- action-time recheck ---` も続くため、
> **audit / report / sync 契約の 4 件 (59 行) が probe module に混入していた**。
> 「subject で割る」と宣言しながら行位置の代理で導出していたので、上記の
> subject 述語による導出へ差し替えた (4 件が `test_legacy_mirror_sync.py` へ移り、
> probe I/O は 5 件 / 108 行、sync は 51 件 / 864 行になった)。
- `tests/support/legacy_mirror_tree_fixture.py` は `_MirrorTreeFixture` の逐語移動で
  あり test を含まないので、この表の 127 には入らない。

**T3 / T4 と T6 は交差しない [導出]。** T3 / T4 が触る test module は
`test_owned_descriptor_teardown.py` の 1 つである。同 module の `os_patch` は **0** —
retention 機械の 19 件は `owned_descriptors._ledger` / `_Occurrence` /
`_Retention._enqueue` を差し替えるのであって `os` primitive を注入しないためである
[実測: A.2 の surface 分類]。したがって T6 の consumer 集合 (`os_patch` 非 0 の
5 module) に `test_owned_descriptor_teardown.py` は**含まれない**。

> R6 まで §7 は「T6 は T2 / T3 とは触る file が交わらないので並行可能」と
> **根拠なしに断定**していた。consumer を特定していない以上それは導出できない。
> 上の表を出して初めて主張になる。

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

| Task | 種別 | **排他 path** (この Task だけが触る) | **共有 path** (順序で直列化。同時 dispatch しない) | 完了条件 |
| --- | --- | --- | --- | --- |
| **T0** | design consultation | — | — | **完了**: Redmine #14662、Review j#92458 approved |
| **T-P** | policy doc 改訂 | `vibes/docs/logics/tests-placement-discovery-policy.md` | — | **完了**: Redmine #14664、Review j#92528 approved → `origin/main-next@6b718673`、required CI success |
| **T1** | move-only | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py` (**削除。削除 owner は T1 のみ**) + §5.5 の **10 module** + `tests/support/legacy_mirror_tree_fixture.py` | `.mozyo-bridge/docs/catalog.yaml` / `.mozyo-bridge/docs/file_conventions.generated.yaml` / `vibes/docs/logics/legacy-mirror-failure-state-characterization.md` | 127 件を 1 commit で移動。**D1 = 127** / **D2 = 自 base の `N` 前後一致**。`src/**` diff **byte 0**。catalog に移設先 exact path を追加し `--check` green。**Appendix A に `superseded` を明記して retire** (§8)。commit message に `move-only` |
| **T2** | behavior change | `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/application/legacy_mirror_sync.py` + `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/domain/legacy_mirror_contract.py` | — | 状態遷移を filesystem effect から分離。§1.1–1.3 の遷移表が pure に評価できる。T1 の test が無改変で green |
| **T3** | behavior change | `src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/application/owned_descriptors.py` + `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_owned_descriptor_teardown.py` | — | §3.2(a) の carrier 差し替え seam を公開面へ。private patch を減らす |
| **T4** | test-only 書き換え | `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_owned_descriptor_teardown.py` のみ | — | §3.2(b) を公開 API 経由へ言い換え。`src/**` 不変 |
| **T6** | behavior change (test 側) | `tests/support/legacy_mirror_fault_schedule.py` (新規) + §5.5 で `os_patch` 非 0 の **5 module のみ**: `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_mirror_fault_injection.py` (29) / `tests/integration/e_130_governance_distribution/f_150_skill_plugin_distribution/test_platform_capability_probe_io.py` (2) / `tests/regressions/test_issue_14580_reused_descriptor_number_close.py` (2) / `tests/regressions/test_issue_14651_capability_advertisement.py` (2) / `tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_platform_capability_probe.py` (1) | `.mozyo-bridge/docs/catalog.yaml` / `.mozyo-bridge/docs/file_conventions.generated.yaml` / `vibes/docs/logics/legacy-mirror-failure-state-characterization.md` | 共有 fault schedule fake の**新規作成**。個別 mock の重複を縮小。**`os_patch` が 0 の module には触らない** |

**移設を行き先ごとに分割しない [R5-F1 修正]。** R5 まで本 doc は regressions 4 件を
別 Task (T5) に切り、`T1 → T5` を要求していた。**これは実行不能である** — T1 が元
file を削除した時点で 4 件はどこにも存在せず、T1 自身の完了条件 `D1 = 127` を
満たせない。仮に T1 の module へ一時的に置くと、T1 の「§5.0 の宣言どおりに移動」と
path ownership の排他性が壊れる。

原因は、**移設先の分類 (unit / scenarios / regressions / integration) をそのまま
commit の分割単位に写した**ことである。分類は「どこへ置くか」の軸であって
「どの commit で動かすか」の軸ではない。**1 つの file から出ていく test を複数
commit に分けた時点で、途中の commit は必ず D1 を割る。**

→ **T1 が 127 件すべてを 1 commit で移し、T5 を撤去した。** `## Move Commit Rules` 1
「Move one family at a time」に沿う — 本 family は 1 つであり、行き先が 4 種類ある
ことは commit を割る理由にならない。境界が 1 点なので D1 / D2 の検査点も 1 つになり、
元 file の**削除 owner も T1 に一意**である。

**T1 に新規 fake を含めない [F5a 修正]:** `refactor-split-strategy.md`
`## Move Commit Rules` 3 は「No logic edits in move commit except import path
mechanical changes」である。新しい共有 fault-schedule fake を書くのは mechanical
move ではないので、**T1 から外して T6 に分けた**。T1 が `tests/support/` に置くのは
既存 `_MirrorTreeFixture` (現行 198–279 の 82 行) の**逐語移動のみ**であり、
file 名も役割どおり `legacy_mirror_tree_fixture.py` とする。

**ownership 規則:**

- **排他 path は 1 Task だけが触る。共有 path は排他ではない。** 共有 path
  (`.mozyo-bridge/docs/catalog.yaml` / `.mozyo-bridge/docs/file_conventions.generated.yaml` / `vibes/docs/logics/legacy-mirror-failure-state-characterization.md`) は T1 と T6 の両方が触るので、**T1 → T6 の順に
  直列化し、同時 dispatch しない**。T6 は T1 が land した catalog / doc の上に
  追記する。
- `src/**` に触るのは **T2 / T3 のみ**。T1 / T4 / T6 は `src/**` diff が
  byte 0 でなければ失格。
- **T1 の hold は解除された。** T0 (#14662) と T-P (#14664) がともに approved で、
  T-P は `origin/main-next@6b718673` へ land し required CI が success である
  [出所: #14664 Review j#92528 / integration j#92531 / CI j#92536]。
  policy `## #14660 legacy mirror family 裁定` の hold 条件は満たされた。
  移設の着手可否そのもの (lane 再開・dispatch) は coordinator 所有である。
- T2 と T3 は **別 module** を持つので並行可能。ただし両者とも T1 の完了を待つ
  (移設前の test を編集すると move が汚れる)。
- T4 は T3 と**同じ file** に触る可能性があるため、**T3 の後**に直列化する。
- T6 は T1 の後。**T3 / T4 とは交差しない** — T3 / T4 が触る test module は
  `test_owned_descriptor_teardown.py` の 1 つで、その `os_patch` は **0** なので
  T6 の consumer 集合に含まれない (§5.5 の表で判定できる)。したがって並行可能。
  T2 とは `src/**` と `tests/**` で面が分かれる。
- **T5 は撤去した** (T1 に統合)。以降の Task 番号は R5 までの記載と互換のため詰めない。

依存順: `T0 → T-P → T1 → {T2, T3, T6}`、`T3 → T4`。**T0 / T-P は完了済み。**

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
`docs resolve` で解決されない。**T1 / T6 の完了条件**であり、後追いの掃除に
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
2. **`_StagingOwnership` の公開性** — 現状 module-private だが、invariant 2 の
   正本主張がここに集中している。公開型にするかは T3 の判断。
3. **`legacy_mirror_sync.py` の分割先** — 状態機械を `domain/` に出すか、
   application 内に新 module を作るかは T2 の設計。7 章末尾参照。
4. **移設 Task の着手判断** — T0 / T-P が完了し policy 上の hold は解除されたが、
   lane 再開・dispatch そのものは coordinator 所有である。本 doc は移設を開始しない。

> **解決済み (R6):** 「配置 matrix を決める」と「実装 Task 境界 / changed-path
> ownership」は §5.0 / §5.5 / §7 で確定した。R5 の同 2 項目は Review j#92566 の
> F1 / F2 により要修正であり (実行不能な T1→T5 と分岐 2 の 1 件誤分類)、**R6 が
> 初めて** 7 / 95 の matrix と単一 move commit の T1 に直した。R4 まで OPEN としていた T0 は Redmine #14662
> (Review j#92458) が裁定し、その doc 反映 T-P は #14664 (Review j#92528) が
> `origin/main-next@6b718673` へ land させ required CI success を得ている。
> 「`tests/regressions/` 移設の file 粒度」も §5.0 分岐 3 の宣言 (2 file / 4 件) で
> 確定したので未確認事項から外した。

### 導出器の lifecycle [landed policy の裁定]

**landed policy が裁定済みである** [`tests-placement-discovery-policy.md`
`### 導出器 (#14660 Appendix A) の位置づけ`、`origin/main-next@6b718673`]。
本 doc に選択の余地は無いので、その disposition をここに写す:

- **Appendix A に据え置く** (`tests/support/` へ昇格しない)。導出器は移設前の単一
  file を引数に取り、A.3 は `assert len(rows) == 127` を持つので、**移設完了時点で
  subject が消えて実行不能になる** — 恒久 gate に見せかけた一時 gate を CI に足さない。
  `### support` の定義 (test から import される共有 fixture) にも合わない。
- **drift window は「裁定 → T1 完了」に限定**し、その窓の drift 検出は T1 の完了条件
  とする (A.1 再実行 + A.4 / A.5 照合)。
- **T1 完了時に Appendix A へ `superseded` と明記して retire する。** §7 の T1 完了
  条件に入れた。
- **移設後に残る恒久不変条件は D1 / D2 の command** であって script ではない (§5.3)。

> R6 まで本節は案 A / 案 B を並べ「本 Task では決めない。T0 と同じ round で裁定を
> 求める」と書いていた。**その裁定は既に下りて land 済であり、未決へ戻していた。**
> ImplDone / Review Request 側には正しく書きながら doc に反映していなかったもので、
> 「更新を書いた場所以外にも残る」class の再発である。


---

## Appendix A. 導出器と全件 mapping (再現用)

下記 script を `origin/main-next@fef86cac` の tree に対して実行した結果を、
**第三者が同じ head で再実行して検証できるよう**全文と全件 mapping とともに置く。

**この Appendix が根拠づけるのは surface の導出値 (127 / 23 / 96 / 8) と rail 数だけ
である。** §5.0 の配置は著者宣言であり、A.5 の **`宣言` 列**がその正本である。
A.3 が出す `69 / 53 / 5` は **historical invalid output** で、配置ではない
(裁定 1 / §5.1 / A.3 冒頭の注記)。

前 revision はこの導出器を scratchpad に置いたまま repo へ残さず、「再現不能」と
注記して未確認事項へ繰延べていた。繰延べは「後で決めること」には使えるが、
**すでに doc に書いた数値の検証可能性**には使えない — review はこの head に対して
行われる。

### A.1 実行手順

**script は repo に何も書かないが、caller が指定した出力 2 file を書く。**
したがって出力先を repo 外の作業 directory に置く。以下は入力を絶対 path で渡し、
成果物を temp directory に閉じ込める手順である (`$REPO` は checkout root):

```text
WORK="$(mktemp -d)"
# A2.py / A3.py を $WORK へ保存する
TARGET="$REPO/tests/unit/e_130_governance_distribution/f_150_skill_plugin_distribution/test_legacy_project_skill_mirror.py"

python3 "$WORK/A2.py" "$TARGET" > "$WORK/inventory.json"
python3 "$WORK/A3.py" "$TARGET" "$WORK/inventory.json" "$WORK/tree.json"
python3 "$WORK/A6.py" "$REPO/src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/application/legacy_mirror_sync.py"

rm -rf "$WORK"
```

書かれる file は **`$WORK/inventory.json`** (A2 の stdout) と
**`$WORK/tree.json`** (A3 が `sys.argv[3]` へ `write_text`) の 2 つだけである。
`A6.py` は何も書かない。**repo の tracked / untracked いずれも変更しない。**

> 前 revision はこの手順を repo root で相対 path のまま実行させ、かつ
> 「read-only。repo を変更しない」と書いていた。**script が source を変更しない
> ことと、filesystem に何も書かないことは別である** — 記載どおり実行すると
> repo root に `inventory.json` / `tree.json` が残る。撤回して上の形にした。

`A2.py` は stderr に surface ごとの件数を、`A3.py` は stdout に決定木の集計と
分岐 3 に落ちなかった test の一覧を、`A6.py` は §1.2 の rail 導出を出す。
`A3.py` は先頭で `len(rows) == 127` を assert するので、分類漏れがあれば落ちる。

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

> **この script の分岐 3 arm は無効である。撤回済み [裁定 1 / policy
> `### regressions`]。** `ANCHOR` 正規表現による「docstring が defect anchor を持つか」
> の判定は、repo 全体で 95–100% 発火して bucket 間の識別力を持たず、また分岐 3 の
> 主語は method ではなく **file** である。この arm が出す `regressions 69` は
> **配置ではない**。確定 matrix は §5.0 の著者宣言である。
>
> **この script は分岐 2 (scenarios) を評価していない。** 分岐 3 → 4 → 5 しか持たない
> ため、分岐 2 が分岐 4 / 5 より先に評価されるという決定木の順序を反映していない
> [裁定 3 が顕在化させた]。
>
> **有効なのは分岐 4 / 5 の候補抽出だけ**であり、それも A.2 の surface 出力に
> 依存するので下限である。判定は各 test を読んで行う (§5.0 で実際に 1 件動かした)。
> script 本文は R4 時点の再現性のために残す。

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
`ast` = `ast_probe`)。

**`分岐` / `行き先` 列は A.3 の出力であり、無効である** (上記 A.3 の注記)。
**確定した配置は `宣言` 列**であり、§5.0 の著者宣言に対応する:
`scen` = `tests/scenarios/` / `reg` = `tests/regressions/` /
`unit` = `tests/unit/e_130_governance_distribution/` /
`int` = `tests/integration/e_130_governance_distribution/`。
両列が食い違う行があるのは、A.3 の分岐 3 arm が無効であることの帰結である。

| # | class | test | line | 行 | surfaces | 分岐 | 行き先 | 宣言 |
| ---: | --- | --- | ---: | ---: | --- | ---: | --- | --- |
| 1 | Tracked | `test_mirror_reference_dirs_present` | 111 | 3 | real_fs | 5 | int | int |
| 2 | Tracked | `test_mirror_reference_files_match_canonical` | 115 | 25 | real_fs | 5 | int | int |
| 3 | Tracked | `test_mirror_reference_set_is_exactly_the_partial_set` | 141 | 16 | real_fs | 3 | reg | int |
| 4 | Tracked | `test_mirror_references_are_regular_files` | 158 | 14 | real_fs | 3 | reg | int |
| 5 | Tracked | `test_mirror_path_has_no_symlinked_component` | 173 | 10 | real_fs | 3 | reg | int |
| 6 | Tracked | `test_tracked_tree_satisfies_the_contract` | 184 | 4 | real_fs | 5 | int | int |
| 7 | Tracked | `test_adapter_skill_md_present_and_not_a_canonical_copy` | 189 | 7 | real_fs | 5 | int | int |
| 8 | Service | `test_clean_tree_passes_and_syncs_idempotently` | 286 | 6 | real_fs | 5 | int | int |
| 9 | Service | `test_canonical_only_edit_is_caught_and_repaired` | 293 | 15 | real_fs | 5 | int | int |
| 10 | Service | `test_content_drift_does_not_block_the_write` | 309 | 9 | real_fs | 5 | int | int |
| 11 | Service | `test_missing_mirror_directory_is_created_by_the_sync` | 319 | 9 | real_fs | 5 | int | int |
| 12 | Service | `test_sync_never_writes_the_adapter_stub_or_extra_references` | 329 | 9 | real_fs | 5 | int | int |
| 13 | Service | `test_entry_names_are_compared_losslessly` | 341 | 17 | real_fs | 3 | reg | int |
| 14 | Service | `test_a_glob_named_entry_does_not_report_unrelated_paths` | 359 | 9 | real_fs | 5 | int | int |
| 15 | Service | `test_a_newline_named_entry_cannot_forge_a_success_line` | 369 | 10 | real_fs | 5 | int | int |
| 16 | Service | `test_unpinned_subdirectory_is_an_entry_too` | 380 | 4 | real_fs | 5 | int | int |
| 17 | Service | `test_a_file_sharing_the_temp_prefix_is_never_deleted` | 387 | 8 | real_fs | 3 | reg | int |
| 18 | Service | `test_a_directory_sharing_the_temp_prefix_blocks_rather_than_hangs` | 396 | 4 | real_fs | 5 | int | int |
| 19 | Service | `test_crash_residue_asks_for_a_reviewed_disposition` | 401 | 10 | real_fs | 5 | int | int |
| 20 | Service | `test_a_concurrent_run_neither_deletes_nor_is_deleted` | 412 | 28 | real_fs | 3 | reg | int |
| 21 | Service | `test_successful_sync_leaves_no_temp_behind` | 441 | 5 | real_fs | 5 | int | int |
| 22 | Service | `test_failed_sync_cleans_only_its_own_temp` | 447 | 31 | real_fs | 3 | reg | int |
| 23 | Service | `test_success_is_not_reported_on_an_unverified_tree` | 479 | 22 | real_fs | 5 | int | int |
| 24 | Service | `test_written_references_are_mode_644` | 502 | 6 | real_fs | 5 | int | int |
| 25 | Service | `test_invalid_source_never_offers_the_resync` | 511 | 9 | real_fs | 3 | reg | int |
| 26 | Service | `test_content_parity_is_skipped_when_the_source_is_invalid` | 521 | 6 | real_fs | 5 | int | int |
| 27 | Service | `test_symlinked_canonical_reference_is_rejected` | 530 | 14 | real_fs | 3 | reg | int |
| 28 | Service | `test_symlinked_canonical_directory_is_rejected` | 545 | 7 | real_fs | 5 | int | int |
| 29 | Service | `test_non_directory_ancestor_is_topology_not_missing_mirror` | 555 | 14 | real_fs | 3 | reg | int |
| 30 | Service | `test_symlinked_mirror_destination_is_rejected` | 570 | 12 | real_fs | 5 | int | int |
| 31 | Service | `test_symlinked_pinned_entry_is_rejected_without_writing_through` | 583 | 10 | real_fs | 5 | int | int |
| 32 | Service | `test_dangling_symlink_entry_is_rejected` | 594 | 4 | real_fs | 5 | int | int |
| 33 | Service | `test_non_regular_pinned_entries_are_rejected_without_blocking` | 599 | 15 | real_fs | 5 | int | int |
| 34 | Service | `test_hardlinked_entry_is_replaced_not_written_through` | 615 | 14 | real_fs | 3 | reg | int |
| 35 | Service | `test_entry_swapped_after_the_type_audit_is_not_read_through` | 632 | 21 | real_fs | 3 | reg | int |
| 36 | Service | `test_source_parent_swapped_after_audit_writes_no_external_bytes` | 654 | 28 | real_fs | 3 | reg | int |
| 37 | Service | `test_mirror_parent_swapped_after_audit_writes_nothing_outside` | 683 | 28 | real_fs | 3 | reg | int |
| 38 | Service | `test_staging_entry_rebound_mid_sync_is_not_swapped_into_place` | 712 | 37 | real_fs | 3 | reg | int |
| 39 | Service | `test_staging_entry_rebound_to_a_regular_file_is_not_swapped_into_place` | 750 | 40 | real_fs | 3 | reg | int |
| 40 | Service | `test_ownership_refuses_to_answer_once_the_descriptor_is_closed` | 793 | 36 | priv,real_fs | 5 | int | int |
| 41 | Service | `test_the_staging_descriptor_still_pins_the_inode_at_every_ownership_question` | 830 | 58 | os_patch,priv,real_fs | 5 | int | int |
| 42 | Service | `test_a_deferred_write_error_is_reported_before_anything_is_installed` | 889 | 32 | os_patch,real_fs | 3 | reg | int |
| 43 | Service | `test_source_becoming_unreadable_after_the_walk_is_typed` | 922 | 24 | real_fs | 5 | int | int |
| 44 | Service | `test_unreadable_canonical_directory_is_a_typed_violation` | 947 | 12 | real_fs | 5 | int | int |
| 45 | Service | `test_platform_without_the_required_primitives_fails_closed` | 960 | 14 | real_fs | 5 | int | int |
| 46 | Service | `test_abnormal_topology_does_not_leak_descriptors` | 980 | 16 | real_fs | 3 | reg | int |
| 47 | Service | `test_repeated_sync_on_an_invalid_tree_does_not_leak_descriptors` | 997 | 10 | real_fs | 5 | int | int |
| 48 | Service | `test_every_topology_failure_shape_is_descriptor_neutral` | 1008 | 21 | real_fs | 5 | int | int |
| 49 | Service | `test_entry_swapped_to_a_fifo_after_the_type_audit_does_not_block` | 1032 | 32 | real_fs | 3 | reg | int |
| 50 | Service | `test_action_time_type_failure_advises_a_recovery_that_converges` | 1065 | 20 | real_fs | 3 | reg | int |
| 51 | Service | `test_source_swapped_to_a_fifo_is_bounded_in_both_modes` | 1086 | 21 | real_fs | 5 | int | int |
| 52 | Service | `test_replace_onto_a_directory_is_typed_not_raised` | 1110 | 27 | real_fs | 3 | reg | int |
| 53 | Service | `test_payload_is_written_in_full_under_injected_short_writes` | 1138 | 22 | os_patch,real_fs | 3 | reg | int |
| 54 | Service | `test_a_write_that_never_progresses_is_bounded` | 1161 | 21 | os_patch,real_fs | 5 | int | int |
| 55 | Service | `test_late_type_swaps_all_carry_rule_e_weight` | 1185 | 38 | real_fs | 3 | reg | int |
| 56 | Service | `test_close_failure_does_not_escape_either_mode` | 1224 | 19 | os_patch,real_fs | 3 | reg | int |
| 57 | Service | `test_cleanup_failure_is_reported_with_the_primary_failure` | 1244 | 27 | os_patch,real_fs | 3 | reg | int |
| 58 | Service | `test_staging_close_failure_is_not_reported_as_success` | 1297 | 19 | os_patch,real_fs | 3 | reg | int |
| 59 | Service | `test_cleanup_leaves_a_foreign_entry_at_the_staging_name` | 1317 | 31 | os_patch,real_fs | 3 | reg | int |
| 60 | Service | `test_a_transient_cleanup_failure_is_not_reported_as_surviving_residue` | 1349 | 42 | os_patch,real_fs | 3 | reg | int |
| 61 | Service | `test_entry_deleted_between_observation_and_read_is_missing_not_unreadable` | 1392 | 28 | real_fs | 3 | reg | int |
| 62 | Service | `test_a_non_oserror_unwinding_the_write_still_releases_the_staging` | 1428 | 16 | os_patch,real_fs | 3 | reg | int |
| 63 | Service | `test_a_non_oserror_unwind_still_spares_a_foreign_entry` | 1445 | 30 | os_patch,real_fs | 5 | int | int |
| 64 | Service | `test_an_unreadable_staging_name_at_swap_time_releases_the_staging` | 1476 | 36 | os_patch,real_fs | 3 | reg | int |
| 65 | Service | `test_a_staging_entry_gone_before_the_swap_is_reported_without_residue` | 1513 | 29 | real_fs | 3 | reg | int |
| 66 | Service | `test_an_unprovable_staging_identity_never_unlinks` | 1543 | 39 | os_patch,real_fs | 3 | reg | int |
| 67 | Service | `test_a_close_that_unwinds_still_releases_the_staging` | 1583 | 34 | os_patch,real_fs | 3 | reg | int |
| 68 | Service | `test_a_close_unwind_never_closes_a_reused_descriptor_number` | 1618 | 57 | os_patch,real_fs | 3 | reg | reg |
| 69 | Service | `test_a_close_unwind_keeps_the_primary_exception` | 1676 | 43 | os_patch,real_fs | 5 | int | int |
| 70 | Service | `test_the_directory_walk_never_closes_a_reused_descriptor_number` | 1720 | 42 | os_patch,real_fs | 3 | reg | reg |
| 71 | Service | `test_a_walk_close_that_unwinds_leaks_no_descriptor` | 1763 | 46 | os_patch,real_fs | 5 | int | int |
| 72 | Service | `test_a_failing_add_note_does_not_replace_the_primary` | 1810 | 42 | os_patch,real_fs | 3 | reg | int |
| 73 | Service | `test_a_failing_cleanup_does_not_replace_the_primary` | 1853 | 32 | os_patch,real_fs | 3 | reg | int |
| 74 | Service | `test_a_raising_release_does_not_take_the_close_with_it` | 1905 | 50 | os_patch,real_fs | 3 | reg | int |
| 75 | Service | `test_the_staging_release_always_precedes_the_staging_close` | 1988 | 51 | os_patch,real_fs | 3 | reg | int |
| 76 | Service | `test_the_walk_keeps_the_first_close_failure` | 2040 | 32 | os_patch,real_fs | 3 | reg | int |
| 77 | Service | `test_a_typed_cleanup_failure_is_recorded_not_discarded` | 2073 | 31 | os_patch,real_fs | 3 | reg | int |
| 78 | Service | `test_a_typed_close_failure_is_recorded_not_discarded` | 2105 | 30 | os_patch,real_fs | 5 | int | int |
| 79 | Service | `test_an_interrupt_during_teardown_outranks_the_primary` | 2136 | 26 | os_patch,real_fs | 3 | reg | int |
| 80 | Service | `test_an_interrupt_while_recording_still_releases_the_staging_entry` | 2163 | 53 | os_patch,real_fs | 3 | reg | int |
| 81 | Service | `test_a_later_control_flow_failure_is_recorded_not_dropped` | 2217 | 45 | os_patch,real_fs | 3 | reg | int |
| 82 | Service | `test_teardown_continues_when_recording_a_secondary_is_interrupted` | 2263 | 26 | priv | 3 | reg | unit |
| 83 | Service | `test_control_flow_priority_keeps_the_first_and_records_the_rest` | 2290 | 26 | priv | 3 | reg | unit |
| 84 | Service | `test_a_broken_note_still_leaves_the_cleanup_failure_reachable` | 2317 | 73 | os_patch,real_fs | 3 | reg | int |
| 85 | Service | `test_a_secondary_that_cannot_be_stringified_is_still_retained` | 2391 | 45 | priv | 3 | reg | unit |
| 86 | Service | `test_an_interrupt_while_recording_a_later_failure_is_retained` | 2437 | 30 | priv | 3 | reg | unit |
| 87 | Service | `test_the_ledger_survives_a_hostile_dict_descriptor` | 2503 | 32 | priv | 3 | reg | unit |
| 88 | Service | `test_the_carrier_key_is_not_an_attribute_name` | 2536 | 30 | priv | 3 | reg | unit |
| 89 | Service | `test_the_pickle_boundary_depends_on_the_entries` | 2567 | 29 | priv | 3 | reg | unit |
| 90 | Service | `test_a_value_at_the_carrier_key_is_never_replaced` | 2597 | 47 | priv | 3 | reg | unit |
| 91 | Service | `test_reading_the_ledger_does_not_create_one` | 2645 | 11 | - | 3 | reg | unit |
| 92 | Service | `test_each_occurrence_is_one_ledger_entry` | 2657 | 29 | priv | 3 | reg | unit |
| 93 | Service | `test_a_carrier_failure_never_skips_a_remaining_action` | 2687 | 52 | priv | 3 | reg | unit |
| 94 | Service | `test_an_arrival_survives_a_failure_before_it_reaches_the_queue` | 2779 | 43 | priv,line,trace | 3 | reg | unit |
| 95 | Service | `test_a_nested_interrupt_never_skips_a_remaining_action` | 2972 | 117 | priv,line,trace | 3 | reg | unit |
| 96 | Service | `test_an_interrupt_during_the_final_admission_still_counts` | 3090 | 55 | priv | 3 | reg | unit |
| 97 | Service | `test_an_exhausted_retry_still_reaches_the_queue` | 3146 | 37 | priv | 3 | reg | unit |
| 98 | Service | `test_retention_survives_an_interrupt_at_a_commit_boundary` | 3211 | 51 | priv,line,trace | 3 | reg | unit |
| 99 | Service | `test_the_final_flush_surfaces_the_control_flow_it_hits` | 3263 | 36 | priv | 3 | reg | unit |
| 100 | Service | `test_a_carrier_that_never_recovers_gives_up_the_record_only` | 3300 | 23 | priv | 4 | unit | unit |
| 101 | Service | `test_the_ledger_survives_a_primary_that_refuses_attributes` | 3324 | 27 | priv | 4 | unit | unit |
| 102 | Service | `test_cleanup_helper_runs_exactly_once_when_it_raises` | 3352 | 30 | os_patch,real_fs | 3 | reg | int |
| 103 | Service | `test_replace_failure_is_classified_by_what_actually_happened` | 3383 | 23 | os_patch,real_fs | 3 | reg | int |
| 104 | Service | `test_replace_onto_a_changed_type_still_says_so` | 3407 | 16 | real_fs | 5 | int | int |
| 105 | Service | `test_capability_manifest_is_exactly_the_primitives_the_module_calls` | 3469 | 24 | ast,priv | 3 | reg | int |
| 106 | Service | `test_each_required_capability_individually_fails_closed` | 3494 | 29 | priv,real_fs | 5 | int | int |
| 107 | Service | `test_a_scandir_whose_failure_is_deferred_still_fails_closed` | 3524 | 24 | os_patch,real_fs | 5 | int | int |
| 108 | Service | `test_an_interrupt_during_the_probe_is_not_a_missing_capability` | 3549 | 11 | os_patch | 4 | unit | unit |
| 109 | Service | `test_a_supported_host_is_not_refused_by_a_stale_advertisement` | 3561 | 20 | os_patch,real_fs | 3 | reg | reg |
| 110 | Service | `test_the_exact_linux_312_advertisement_is_accepted` | 3582 | 8 | os_patch | 4 | unit | reg |
| 111 | Service | `test_the_probe_writes_nothing_and_leaks_no_descriptor` | 3591 | 15 | real_fs | 5 | int | int |
| 112 | Service | `test_the_probe_anchor_is_not_a_directory` | 3607 | 7 | priv | 4 | unit | unit |
| 113 | Service | `test_a_probe_that_cannot_be_set_up_fails_closed` | 3615 | 16 | os_patch,real_fs | 5 | int | int |
| 114 | Service | `test_unreadable_canonical_reference_is_a_typed_violation` | 3645 | 18 | real_fs | 3 | reg | int |
| 115 | Service | `test_unreadable_mirror_directory_is_a_typed_violation` | 3664 | 12 | real_fs | 5 | int | int |
| 116 | Service | `test_diagnostics_carry_no_host_absolute_paths` | 3677 | 7 | real_fs | 5 | int | int |
| 117 | Service | `test_source_swapped_after_preflight_is_fail_closed` | 3687 | 22 | real_fs | 5 | int | int |
| 118 | Cli | `test_wrapper_exists_and_is_executable` | 3740 | 3 | real_fs | 5 | int | int |
| 119 | Cli | `test_wrapper_carries_no_mirror_logic` | 3744 | 11 | real_fs | 5 | int | int |
| 120 | Cli | `test_check_and_sync_round_trip` | 3756 | 10 | real_fs,subprocess | 5 | int | scen |
| 121 | Cli | `test_check_reports_a_violation_and_writes_nothing` | 3767 | 8 | real_fs,subprocess | 5 | int | scen |
| 122 | Cli | `test_help_exits_zero` | 3776 | 5 | real_fs,subprocess | 5 | int | scen |
| 123 | Cli | `test_unknown_argument_exits_64` | 3782 | 5 | real_fs,subprocess | 5 | int | scen |
| 124 | Cli | `test_repo_cannot_be_redirected_by_operator_argv` | 3788 | 19 | real_fs,subprocess | 3 | reg | scen |
| 125 | Cli | `test_repo_env_is_overwritten_by_the_wrapper` | 3808 | 17 | real_fs,subprocess | 5 | int | scen |
| 126 | Cli | `test_module_run_without_the_wrapper_refuses` | 3826 | 21 | real_fs,subprocess | 5 | int | int |
| 127 | Cli | `test_wrapper_targets_its_own_repo_not_the_cwd` | 3848 | 14 | real_fs,subprocess | 5 | int | scen |

**この表の原資料としての位置づけ:** `surfaces` 列は §2.3 の内訳の、`宣言` 列は
§5.0 の確定 matrix の原資料である。`分岐` / `行き先` 列は A.3 の出力であり、
**分岐 3 arm が無効・分岐 2 未評価なので配置としては使えない** (A.3 の注記)。
`行き先` と `宣言` が食い違う行は、その無効化の帰結である。


### A.6 rail の導出 (§1.2)

`_replace_one` の rail が 4 本であることは、目視ではなく `try: install()` の
guard が top-level 文のどれを覆うかを AST で判定して確定した。前 revision は
rail を 2 本と決め打ちし、保護域外の 2 経路を落としていた。

```python
"""Enumerate the rails of `_replace_one` from the AST, not by eye.

Read-only: parses the module and prints. Writes nothing.

A "rail" here is an exit path with a distinct teardown regime. What decides
that is which top-level statements the `try: install()` guard covers: anything
before it or after it unwinds without `_teardown_during` running at all.

Usage:
    python3 rails.py <abs path to legacy_mirror_sync.py>
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

src = Path(sys.argv[1]).read_text(encoding="utf-8")
lines = src.split("\n")
fn = next(
    n
    for n in ast.walk(ast.parse(src))
    if isinstance(n, ast.FunctionDef) and n.name == "_replace_one"
)

# The guard is the top-level `try` whose handler catches BaseException.
guard = None
for st in fn.body:
    if isinstance(st, ast.Try) and any(
        isinstance(h.type, ast.Name) and h.type.id == "BaseException" for h in st.handlers
    ):
        guard = st
        break
if guard is None:
    raise AssertionError("no BaseException guard at the top level of _replace_one")

print("guard (try: install()) spans lines "
      f"{guard.lineno}-{guard.end_lineno}")
print()
print("top-level statements, and whether the guard protects them:")
for st in fn.body:
    if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
        continue  # docstring
    covered = guard.lineno <= st.lineno <= (guard.end_lineno or guard.lineno)
    where = "INSIDE guard" if covered else "OUTSIDE guard"
    print(f"  L{st.lineno:4d} {type(st).__name__:12s} {where:14s} {lines[st.lineno-1].strip()[:56]}")

before = [s for s in fn.body if s.lineno < guard.lineno and not (
    isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
after = [s for s in fn.body if s.lineno > (guard.end_lineno or guard.lineno)]
print()
print(f"rails: pre-guard statements={len(before)}, post-guard statements={len(after)}")
print("  R-A pre-staging      : unprotected, teardown NOT run")
print("  R-B install unwind   : the guard itself, _teardown_during runs")
print("  R-C typed return     : guard returned a value, _close_staging runs")
print("  R-D post-close unwind: unprotected, teardown NOT run")

# Which typed returns sit before the guard? Those never reach _close_staging.
# Only `_replace_one`'s OWN returns count: `release`/`install` are nested defs
# whose returns are lexically earlier but execute INSIDE the guard, so walking
# the whole subtree would list them and misstate which exits skip the close.
nested = {
    id(x)
    for d in ast.walk(fn)
    if isinstance(d, ast.FunctionDef) and d is not fn
    for x in ast.walk(d)
}
print()
print("_replace_one's own returns before the guard (never reach _close_staging):")
for n in ast.walk(fn):
    if isinstance(n, ast.Return) and id(n) not in nested and n.lineno < guard.lineno:
        print(f"  L{n.lineno}  {lines[n.lineno-1].strip()[:50]}")
```

同 head での出力 [実測]:

```text
guard (try: install()) spans lines 736-750

top-level statements, and whether the guard protects them:
  L 579 Assign       OUTSIDE guard  payload, failure = self._read_bound(source_fd, name)
  L 580 If           OUTSIDE guard  if failure is not None:
  L 590 Assign       OUTSIDE guard  subject = f"{MIRROR_RELATIVE}/{describe_name(name)}"
  L 591 Assign       OUTSIDE guard  temp_name = f"{_TEMP_PREFIX}{os.urandom(8).hex()}.tmp"
  L 592 Assign       OUTSIDE guard  staging_subject = f"{MIRROR_RELATIVE}/{describe_name(tem
  L 593 Try          OUTSIDE guard  try:
  L 612 Assign       OUTSIDE guard  ownership = _StagingOwnership(temp)
  L 613 Assign       OUTSIDE guard  staging_live = True
  L 615 FunctionDef  OUTSIDE guard  def release() -> tuple[Violation, ...]:
  L 629 FunctionDef  OUTSIDE guard  def install() -> tuple[Violation, ...]:
  L 736 Try          INSIDE guard   try:
  L 751 Return       OUTSIDE guard  return problems + self._close_staging(temp, subject)

rails: pre-guard statements=10, post-guard statements=1
  R-A pre-staging      : unprotected, teardown NOT run
  R-B install unwind   : the guard itself, _teardown_during runs
  R-C typed return     : guard returned a value, _close_staging runs
  R-D post-close unwind: unprotected, teardown NOT run

_replace_one's own returns before the guard (never reach _close_staging):
  L581  return (
  L603  return (
```

`L581` / `L603` は W0 / W1 の typed return であり、**`_close_staging` に到達しない** —
§1.2 の W14 が W2–W12 に限られる根拠である。

---

## 参照

- Redmine #14660 (本 Task) / #14592 (親 US) / #14580 / #14651 / #14652 / #14655 / #14656
- `vibes/docs/logics/tests-placement-discovery-policy.md` — 配置決定木 / discovery 不変
- `vibes/docs/logics/refactor-split-strategy.md` — `## Characterization Strategy` / `## Move Commit Rules`
- `vibes/docs/logics/module-health-gate.md` — 閾値 / scope / allowlist 契約
- `vibes/docs/logics/skill-distribution.md` — Mirror Contract
- `vibes/docs/specs/bounded-context-map.md` — `## Redmine-numbered package path map (#12622)`
