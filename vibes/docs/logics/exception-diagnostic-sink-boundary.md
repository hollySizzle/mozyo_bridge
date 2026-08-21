# Exception Diagnostic Sink Boundary

Redmine #15840 (親 US #15839)。**捕まえて捨てている例外の証拠をどこに置き、そこから何を持ち出してよいか**を決める設計正本。

この doc は `runtime-observability-boundary.md` と主題が異なる。あちらは「runtime (agent / pane / lane) の状態をどう観測するか」であり、こちらは「コードが catch して破棄した例外をどう残すか」である。前者は観測対象が外界、後者は自分自身の制御フローである。

## なぜあるか (実害の記録)

2026-08-20 時点の実測:

```
src/ 全体で logging を import している箇所        : 0
broad except (Exception / BaseException) handler : 769
  例外に一切触れず握りつぶし                      : 578 (75%)
  print / stderr へ出す                           :   6 (0.8%)
  re-raise                                        :  84 (11%)
```

握りつぶし 578 のうち **124 が mutating 系 path** (retire / actuation / CAS / lifecycle / close / dispatch / launch / hibernate) にある。

最悪の実例は `sublane_retire_application.run_retire_application` の終端である:

```python
except Exception:  # noqa: BLE001 - an exception may be after a side effect
    return RetireApplicationResult(
        state=RETIRE_RESULT_UNCERTAIN,
        reason=REASON_APPLICATION_ERROR,
        uncertain=True,
    )
```

コメント自身が「副作用の後かもしれない」と宣言しているのに、例外の型も message も traceback も残らない。**運用上もっとも知りたい場面が、唯一何も残らない場面になっている。**

実測された損失 (#15789 j#109134): 実験が `application_state=uncertain / reason=retire_application_error` を返し原因が特定できず、自前 harness に log を仕込んで再実行して初めて `git worktree add` の失敗と判明した。その 1 行が #15789 の修正全体の要になった論証の出発点だった。class 名 (`CalledProcessError`) が見えていれば、その往復は不要だった。

## 決定 1: typed outcome は置き換えない

typed refusal / blocked reason の体系は**この doc の対象外であり、変更しない**。

期待される失敗については typed outcome が log より優れている:

- 呼び出し側が値で分岐できる (文字列の正規表現一致ではない)
- review で定数名を名指して照合できる
- test で pin でき、mutation で非空虚性を測れる

本 doc が扱うのは **typed outcome に載らない想定外例外**だけである。両者は競合しない。

## 決定 2: sink は guard された home の外に置く

`mozyo-bridge tests run` の shared-home guard は home 配下の**全 SQLite の table 別 row 数**を fingerprint する:

> `identity` — per-table row counts of every home SQLite

したがって診断 sink を **home 配下の SQLite table にしてはならない**。test 実行のたびに row が増え、guard が毎回 `operator shared home changed` で FAIL する。これは #15789 の作業中に 2 回踏んだ偽 FAIL と同じ現象になる。

**採る形**: guard の fingerprint 対象外の位置に置く。

**却下した形と理由**:

- **home 配下の SQLite table** — 上記のとおり guard を毎回鳴らす。既存 store (delivery ledger / lane lifecycle) の idiom に沿うという利点はあるが、その idiom は「状態」を持つためのもので、診断の追記を想定していない。
- **既存 store への相乗り** — 同上に加え、状態の store に診断が混ざると、store の schema 変更が診断都合で発生するようになる。責務が混ざる。
- **`logging` の global handler** — 出力先が実行環境依存になり、決定 3 の持ち出し境界を構造的に保証できない。sink の実現手段として `logging` を使うこと自体は禁じないが、その場合も出力先と field 構成は本 doc の決定に従う。

## 決定 3: host-local には raw を持ってよい。durable record へ写してはならない

repo は既に同じ線を引いている。`lane_metadata.LaneMetadataRecord` の宣言:

> ``worktree_path`` is host-local private state; never copy it into a durable Redmine record.

herdr の locator / assigned name、Redmine token、operator の絶対 path も同じ経路を通る。組織 baseline も log への秘密記録を禁止している。

**この線は「記録するな」ではなく「持ち出すな」である。** したがって:

- **host-local sink に raw (message / traceback を含む) を持つことは、この線の内側であり許される。**
- **sink の内容を durable record (Redmine journal / ticket / commit message / 配布物) へ写すことは禁止する。**
- 持ち出す必要が生じた場合は、**写すのではなく再現手順を書く**。sink は host-local な調査の出発点であって、共有の証拠物ではない。

### 系: durable record へ出る field は構造的に安全なものに限る

typed outcome (`reason` / `detail` など) は CLI の JSON 出力に乗り、journal へ貼られうる。したがってそこに載せてよいのは、**型として秘密を含み得ないもの**に限る。

- **載せてよい**: 例外の **class 名** (`type(exc).__name__`)。Python の class 名は識別子であり、path / token / 個人情報を構造上含まない。
- **載せてはならない**: `str(exc)` / `repr(exc)` / traceback / `args`。これらは path や外部コマンドの出力を含みうる。実例: `git worktree add` の失敗 message は recorded worktree の**絶対 path を含む**。

この区別は「今は安全そうだから」ではなく**型による保証**である。判断を都度させない。

## 決定 4: 自由 interpolation を許さない

sink に書く内容は固定 field 構成とする。呼び出し側が任意の文字列を組み立てて渡す形にしない。

理由: 自由 interpolation を許すと、決定 3 の境界が呼び出し箇所ごとの判断になる。578 箇所に判断を配ると必ず漏れる。

field は最小で次の 4 つとする (拡張は本 doc の改訂を伴う):

| field | 内容 | durable record へ |
| --- | --- | --- |
| `exception_type` | `type(exc).__name__` | 可 |
| `exception_message` | `str(exc)` | 不可 |
| `traceback` | formatted traceback | 不可 |
| `durable_anchor` | `redmine:issue=N:journal=M` 等、既に durable な識別子 | 可 |

## 決定 5: 書き始める前に決定 2〜4 を確定する

一度書かれた log は、後から方針を変えても**既に書かれたものが残る**。したがって sink の実装より先に本 doc を確定させる。この順序は逆にできない。

本 doc の初版 (#15840) は決定 2〜4 の確定までを扱い、実際の sink 実装と 578 箇所の捕捉は親 US #15839 の後続 slice で行う。#15840 が入れる捕捉は、決定 3 により **sink の完成を待たずに安全な class 名のみ**に限る。

## 適用範囲

- 対象: `src/mozyo_bridge/**` の broad except handler が破棄している想定外例外。
- 非対象: typed refusal / blocked reason、Redmine journal、herdr delivery ledger、lane lifecycle store。いずれも「状態」の記録であり、例外の記録ではない。

## 関連

- 親 US: Redmine #15839
- 発端 evidence: Redmine #15789 j#109134 / j#109143
- 隣接問題圏: Redmine #15241 (副作用が起きたかもしれない中断を durable に扱う)
- 主題の異なる観測 doc: `runtime-observability-boundary.md`
