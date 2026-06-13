# Public / Private Boundary

Redmine #11809。この rule は、open-source project である `mozyo-bridge`
repository に private cockpit や operator 固有の前提が混入するのを防ぐための
project-local guardrail である。

## Rule

`mozyo-bridge` は public infrastructure project として扱う。提供してよいのは、
汎用 CLI primitive、JSON / event facts、tmux / session inventory、safety gate、
documented extension point である。private workspace path、private 実装詳細、
社内運用規約、credential、operator 固有 runbook を product default として公開してはならない。

設計判断が `mozyo-bridge` 側か private consumer 側かで迷う場合は、次の分界で判断する。

- `mozyo-bridge` は、private consumer を知らなくても有用な汎用 primitive を持つ:
  workspace / session identity、pane discovery、handoff safety、JSON / text facts、
  event storage、command contract。
- private consumer は、表示と運用方針を持つ:
  business label、cockpit UI composition、comment-stream view、lane rule、
  user-specific default、private path convention、internal workflow decision。
- private consumer が必要とする capability であっても、他の利用者にも安全かつ汎用的に有用な場合だけ、
  `mozyo-bridge` は狭い primitive または metadata field として提供してよい。
- private workflow だから存在する値は、private repository、private ticket、
  または operator runbook に置く。OSS default として encode しない。

## Public Record Constraints

OSS work を支える tracked `mozyo-bridge` file や Redmine journal には、次を記録しない。

- personal home path または private project absolute path;
- credential、token、API key、cookie、secret-shaped example;
- high-level consumer role name を超える private repository internal;
- customer、staff、executive 固有の operating instruction;
- generic integration contract に抽象化されていない private GUI 実装詳細。

境界説明に必要な場合は、"private consumer"、"internal cockpit"、named consumer role
のような high-level term を使ってよい。ただし説明は、public repository が review、
release、reuse されても private operation を漏らさない抽象度に留める。

## Issue Triage

新規 ticket を作るときは、次の routing を使う。

- business UX、lane policy、presentation、operator workflow を定義する作業は、
  private consumer project に主 ticket を置く。
- reusable primitive、safety constraint、inventory field、event schema、CLI behavior
  を定義する作業は、`mozyo-bridge` に ticket を置く。
- 両方が関係する場合は、business decision を private consumer ticket に先に置き、
  `mozyo-bridge` には狭い reusable primitive の dependency だけを切る。

design consultation では、少なくとも次を明示する。

- source of truth の owner;
- UI / presentation の owner;
- CLI / API / event contract の owner;
- safety / privacy boundary の owner;
- test または verification responsibility。

## Examples

- comment-stream cockpit view は private presentation である。ただし `mozyo-bridge` が
  generic event tail / query source だけを提供する場合は、`mozyo-bridge` 側の primitive として扱える。
- parallel work lane policy は private operating policy である。ただし lane を安全に区別するための
  host / workspace / session metadata は `mozyo-bridge` が提供してよい。
- local cockpit と remote SSH cockpit は別 tmux host として分離する。`mozyo-bridge` は
  host-aware inventory を提供してよいが、private operator の window layout を規定しない。

## Verification

docs、catalog、README、skill、release 変更を commit する前に、diff が private path
や secret-shaped value を導入していないことを確認する。`vibes/docs/logics/release-flow.md`
の release check は distribution gate として引き続き strict に扱う。本 rule はそれより前段の
design-time filter である。
