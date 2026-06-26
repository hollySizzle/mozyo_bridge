# OOP-first architecture policy

Redmine #12633 / Version #276 `OOP-first architecture and static typing`.

この文書は mozyo_bridge の今後の設計思想を固定する。実装手順書ではない。
個別 module の移動、class 抽出、typing tool 導入、CI gate 追加は後続 issue で扱う。

## 背景

mozyo_bridge は当初、小さな CLI helper と pure function / dataclass 中心の構成で十分だった。
しかし現在の主役は tmux、Redmine、handoff、lane、delegated coordinator、workspace state、
docs catalog、release governance のような外部境界と状態遷移である。

この種の product では、関数の束だけで責務を保つと次の問題が強くなる。

- どの関数が workflow authority / owner approval / routing / send safety を持つかが散る。
- CLI handler が orchestration と policy と presentation を抱え込み、巨大化する。
- test double を差す境界が function monkeypatch に偏り、仕様単位の fake にしにくい。
- 型で表現できる contract を runtime test と docstring に寄せすぎる。
- Redmine-numbered package path の長さと import 粒度が、設計上の境界を読み取りにくくする。

したがって `OOP-first architecture and static typing` planning bucket 以降の architecture work は
**OOP-first** を基本方針にする。Redmine Version 名は planning bucket であり、package release
番号の正本ではない。実際の package version は release gate / tag / release notes で決める。

## 方針

OOP-first とは、すべてを class にするという意味ではない。
公開設計面、状態を持つ協調処理、外部境界、権限を伴う判断を object boundary として表す、
という意味である。

基本形:

```text
CLI parser / command
  -> CommandHandler
    -> UseCase / ApplicationService
      -> Domain Policy / Planner / Value Object
      -> Port Protocol
        -> Adapter Implementation
```

この形では、CLI は入出力変換と exit code に寄せる。
workflow の判断、target resolution、ticket journal write、tmux 操作、docs/catalog validation は
それぞれ named object の責務として持つ。

## 境界モデル

### Command handler

CLI subcommand ごとに、argument namespace を受け取り use case を呼ぶ薄い object にする。
巨大な `commands.py` に business flow を集めない。

Command handler は次を持ってよい。

- CLI argument の読み替え。
- stdout / stderr への表示形式選択。
- use case result から process exit code への変換。

Command handler は次を持たない。

- Redmine / tmux / Git / filesystem の直接操作。
- workflow gate の意味判断。
- owner approval / review / close authority の判定。

### Use case / application service

複数の port を協調させる状態遷移は use case object が持つ。
handoff send、sublane dispatch、review callback、workspace registration、docs audit などはここに入る。

Use case は external adapter を直接 import せず、Protocol 経由で受け取る。
これにより unit test は monkeypatch ではなく fake port で仕様を表現する。

### Domain policy / planner

純粋な判断、計画、分類は domain object として置く。
`Policy`、`Planner`、`Resolver`、`Decision`、`Plan` のような名前で、authority と出力 contract を明示する。

単純な validation helper や deterministic projection は private function として残してよい。
ただし公開 API は named object または value object を優先する。

### Port / adapter

tmux、Redmine、Git、filesystem、subprocess、state store、network は port と adapter に分離する。

- port: `Protocol` または abstract boundary。domain / use case が依存する。
- adapter: live implementation。外部副作用と credential / environment 依存を持つ。
- fake: test implementation。仕様上の状態遷移を表す。

external boundary を naked function の集合として公開しない。
薄い wrapper で十分な場合でも、長期的に injection / fake / typing が必要になる境界は object にする。

### Value object

durable anchor、lane identity、route identity、workflow gate、delivery outcome、module health finding などは
immutable value object として扱う。

dict / str / tuple のまま公開境界を流すのは、互換層または serialization layer に限定する。
内部では field 名と型を持つ object に戻す。

## static typing policy

OOP-first は static typing とセットで導入する。
class だけ増やして型を弱いままにすると、動的 duck typing の object 群になり保守性は上がらない。

段階導入の優先順位:

1. Value object と Result object を型で固定する。
2. External port を `Protocol` で固定する。
3. Use case constructor injection を型で固定する。
4. CLI adapter の `argparse.Namespace` 依存を command input object へ寄せる。
5. `mypy` または `pyright` を bounded context 単位で段階的に適用する。

初期段階で全 repo strict を要求しない。
ただし、新しい architecture tranche で追加する public object boundary は型検査可能にする。

## pure function の扱い

pure function は禁止しない。
以下は function のままでよい。

- value object の小さな正規化。
- decision table の内部 helper。
- serialization の private helper。
- 依存を持たない deterministic projection。

ただし、次に該当するものは object boundary へ寄せる。

- 外部副作用を持つ。
- 複数 step の状態遷移を持つ。
- policy / authority / approval / routing を判断する。
- test double を差したくなる。
- caller が大量の primitive 引数を渡している。
- docstring でしか責務境界を守れていない。

## migration stance

OOP-first migration は behavior-preserving を基本にする。
source-layout correction や sublane lifecycle work に混ぜて進めない。

`OOP-first architecture and static typing` planning bucket 以降の実装 tranche では、まず巨大 orchestration surface を object boundary に分ける。
候補は次の順で扱う。

1. CLI command handler / use case boundary。
2. handoff orchestration。
3. tmux / Redmine / Git external ports。
4. delegated coordinator route planner / executor。
5. docs/catalog and health governance services。

1 commit で全体を class 化しない。
bounded context ごとに、既存 test を維持しながら object boundary を足し、古い function facade は
caller migration が終わるまで compatibility layer として扱う。

## anti-patterns

- `Manager` / `Helper` / `Util` だけを増やし、責務を名前で曖昧にする。
- `argparse.Namespace` を use case deep layer まで流す。
- Redmine / tmux / Git subprocess を domain layer から直接呼ぶ。
- dict payload を authority-bearing decision として公開する。
- mock 前提の test を増やし、port fake で仕様を表さない。
- OOP-first を理由に pure decision helper まで ceremony class にする。

## decision

mozyo_bridge は `OOP-first architecture and static typing` planning bucket 以降、architecture work の基本姿勢を OOP-first とする。
既存の pure function / dataclass 資産は捨てず、value object と deterministic helper として残せるものは残す。
ただし、公開設計面、外部境界、状態遷移、authority-bearing policy は object boundary と static typing で表現する。
