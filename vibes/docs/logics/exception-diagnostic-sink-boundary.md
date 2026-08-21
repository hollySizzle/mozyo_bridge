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

### 選定 (Redmine #15840 review j#109671 `finding_sinklocationundefined`)

初版は「guard の fingerprint 対象外の位置に置く」とだけ書いていた。これは**除外であって選定ではない**。`MOZYO_BRIDGE_HOME` の外であることは何も保証しない — repo / worktree 内に落ちれば commit されて共有され、stderr のみにすれば親 process に capture されて CI log や journal という durable surface に載り、permission を決めなければ host-local でも他 user から読める。以下を選定として固定する。

**sink**: `${XDG_STATE_HOME:-~/.local/state}/mozyo-bridge/diagnostics/` 配下の **file**。

- **XDG state を選ぶ理由**: 診断は config でも cache でもなく、再生成できないが設定でもない state である。repo には既に XDG 慣行がある (`shared/paths.py` の `CONFIG_HOME = XDG_CONFIG_HOME or ~/.config`) ので、新しい慣行を発明しない。
- **guard 対象外であることの根拠**: shared-home guard の対象は `ambient_homes()` が返す `~/.mozyo_bridge` と `$MOZYO_BRIDGE_HOME` の 2 つだけであり (`test_home_fence.py`)、XDG state 配下はそこに含まれない。したがって sink への追記が `tests run` を鳴らすことはない。
- **permission**: directory は `0700`、file は `0600`。作成時に明示的に設定する (umask 依存にしない)。
- **retention**: 無限成長させない。上限 (件数または総 byte 数) を実装時に固定し、超過分は古い順に破棄する。
- **file 形式**: 1 record 1 行の追記のみ。読む側が壊れた行を読み飛ばせること (診断が診断の障害で失われないため)。

**禁止 surface (明示)**:

| 置いてはならない場所 | 理由 |
| --- | --- |
| repo / worktree 内 | commit されて共有される。持ち出し境界を破る |
| guard された home (`~/.mozyo_bridge` / `$MOZYO_BRIDGE_HOME`) | 上記制約。`tests run` が毎回鳴る |
| stderr のみ | 親 process / CI / pane scrollback に capture され、durable surface へ載りうる |
| 環境変数 / process 引数 | 他 process から読める |

stderr への出力を**併用**すること自体は禁じないが、その場合 stderr へ出してよいのは決定 3 の「durable record へ出してよい field」に限る。raw は sink file にのみ書く。

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

typed outcome (`reason` / `detail` など) は CLI の JSON 出力に乗り、journal へ貼られうる。したがってそこに載せてよいのは、**閉じた語彙から出た値**に限る。

- **載せてよい**: **この repo が書いたリテラルの閉集合**から選ばれた token のみ。例外オブジェクトから導出した文字列は一切含まない。
- **載せてはならない**: `str(exc)` / `repr(exc)` / traceback / `args`、**および `type(exc).__name__`**。

#### 訂正 (Redmine #15840 review j#109671 `finding_unsafeexceptiontype`)

本 doc の初版は「class 名は識別子なので path / token を構造上含まない、これは**型による保証**である」と書いていた。**これは誤りであり、review が反例で示した。**

```python
type("SECRET_TOKEN_VALUE_123", (RuntimeError,), {})
```

`type()` の第 1 引数は**任意の文字列**を取る。したがって class 名は「識別子の形をした呼び出し側データ」でしかなく、信頼できるリテラルではない。`isidentifier()` は形しか見ないので、秘密が識別子の形をしていれば素通りする。初版の実装と regression は、**防ぐと称した漏洩をそのまま受理していた**。

**型による保証を主張できる条件は 1 つだけ**である: 出力される値が、**この repo のソースに書かれたリテラルの閉集合**から出ること。データ由来の文字列を 1 つでも通すなら、それは保証ではなく「そういう名前は付けられないだろう」という仮定にすぎない。

実装は `sublane_retire_application._durable_failure_kind` を正本とし、`(信頼する例外 class, リテラル token)` の固定表を identity 比較で走査して token を返す。未知は固定リテラル (`unclassified`) へ落とす。

#### 終端 handler では raise しうる操作を行わない (`finding_terminalhandlerescape`)

同 review は、初版が broad terminal handler の中で `type(exc).__name__` を読んでいたことも指摘した。metaclass は `__name__` を raise する property として定義でき、その場合 **handler 自身が例外を投げて、呼び出し側は `uncertain` を受け取れない**。`RetireApplicationResult` の契約 (`exceptions never masquerade as a deterministic refusal`) は「例外が typed result として**呼び出し側へ届く**」ことを含むので、これは契約違反である。

したがって分類処理は次を守る:

- **identity 比較 (`is`) のみ**を使う。user code を呼ばない。
- `__name__` / `isinstance` (`__instancecheck__`) / dict lookup (`__hash__`) を使わない。いずれも metaclass が乗っ取れる。
- 走査全体を `except BaseException` で包み、失敗しても固定リテラルへ落とす。**分類は総体 (total) でなければならない。**

metaclass が異様であることは免罪にならない。終端 handler は最後の砦であり、そこで raise しうる操作を実行してはならない。

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
