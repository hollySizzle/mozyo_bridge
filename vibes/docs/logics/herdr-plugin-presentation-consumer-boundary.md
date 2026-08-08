# Herdr Plugin Presentation Consumer Boundary

Redmine #14631。Herdr の plugin surface を、mozyo-bridge の
workflow / provider plugin ではなく **presentation consumer** として扱う境界、
sublane UX の方向、engine-first の実装順序を固定する設計正本。

## Status

本書は presentation consumer の設計境界である。識別表示の最初の実装は
`herdr-unit-board.md`、safe pane movement は #14605 系を正本とする。0.7.5 の
初期実測事実は #14614 journal #91226、採用した実装分割は #14617 / #14618 /
#14619、本書に記録した owner UX intent は #14631 を durable anchor とする。

## Decision

Herdr が plugin を load できることと、mozyo-bridge が arbitrary external code を
provider として load することは別である。

```text
mozyo-bridge core
  -> public-safe Unit / Target / attention / workflow projection
  -> preview-first safe action
  -> Herdr plugin / iTerm WebViewer / private cockpit UI
```

- Herdr plugin は Herdr host 上で動く presentation consumer であり、
  mozyo-bridge の built-in provider registry へ登録しない。
- mozyo-bridge は plugin module、callable、install command、DOM state を読み込まない。
- Herdr plugin、iTerm / WebViewer、private cockpit UI は、同じ public-safe
  projection を読む兄弟 consumer とする。
- consumer は表示と navigation を所有できるが、identity、routing、workflow、
  approval、completion の正本を作らない。

従って、`plugin-ready-adapter-boundary.md` と
`modular-config-driven-refactor.md` が禁止する arbitrary plugin loading は維持する。
Herdr-hosted plugin の存在を理由に、その境界を緩めない。

## State Ownership

| state | authority / source of truth | consumer がしてよいこと |
| --- | --- | --- |
| workflow / review / owner approval / close | Redmine issue / journal | read-only projection、filter、badge |
| workspace / lane / role identity | registry / workspace anchor / managed lane record | stable keyとして参照。再定義しない |
| runtime target / liveness | action-time live Herdr inventory + process preflight | observed stateを表示。cached stateだけで送信しない |
| desired portable presentation | repo-local typed config | default / seedとして読む |
| operator-local presentation | home-scoped presentation state | position、pin、hide、collapse等を保存 |
| live geometry | observed Herdr pane / tab geometry | drift表示。identityへ昇格しない |
| delivery authorization / exact-once / generation | mozyo-bridge workflow engine | safe actionの結果を表示。consumer独自送信をしない |

Repo-local config は runtime state の保存先ではない。project 共通で review 可能な
default / seed に限る。頻繁に変える個人表示は operator-local presentation state に
置く。live geometry は observation であり、どちらの desired state の正本にも
しない。

## UX Intent

### Unit composition

operator の主な表示像は、1 Unit を縦長の pair とし、複数 Unit を横へ並べる形である。

```text
+----------------+  +----------------+  +----------------+
| coordinator    |  | coordinator    |  | coordinator    |
+----------------+  +----------------+  +----------------+
| implementation |  | implementation |  | implementation |
+----------------+  +----------------+  +----------------+
```

- 上段は coordinator role、下段は implementation role。
- operator-facing label は `assistant` 等へ変更できるが、表示 label を role
  identity にしない。
- orientation、Unit 内 split ratio、Unit の列順、相対幅は typed desired
  presentation として扱う。
- Unit 数 / pane 数の product 上限、自動折返し、密度制限は本方針では設けない。
  実モニターに合わない場合は operator が設定を調整する。
- モニター別 profile、色、shortcut 等の個人値を OSS / repo default に
  hard-code しない。

### View customization

presentation consumer が扱ってよい候補:

- Project / coordinator / sublane の grouping。
- Unit の `position` / `pinned` / `hidden` / `collapsed`。
- public-safe `label_override`、attention badge、delegation depth / tree。
- `implementing` / `review_waiting` / `blocked` / callback due 等の filter。
- focus / navigation。
- desired と live geometry の drift 表示。
- engine が提供する preview-first の Unit move / rebalance / reconcile action。

「同じ tab に見える」「隣にいる」「上段 / 下段である」は display fact であり、
cross-lane direct send の承認ではない。handoff は引き続き target-lane coordinator /
gateway と action-time preflight を通す。

## Consumer Contract

### Read side

consumer-stable として目指す入力は、内部 DB や pane text の直接読みにしない。

- Unit / Target projection (`agents targets` 系の public-safe record)。
- workflow / next-action projection。
- attention projection。
- event timeline envelope。
- desired presentation record と live geometry の明示的に区別された projection。

consumer が不足 field を要求した場合は、まず generic な public-safe projection として
core に追加できるかを判断する。private field、absolute path、credential、
provider session 本文を UI 都合で公開しない。

### Action side

consumer は pane / DB / Redmine へ直接 write しない。将来の action surface は次を
満たす。

1. Unit / desired-state key を入力とし、volatile pane locator の推測を要求しない。
2. preview を既定とする。
3. apply 時に live target / identity / generation を再確認する。
4. ambiguous / stale / unsupported を typed fail-closed outcome にする。
5. action result は workflow completion や approval を含意しない。

focus、move、rebalance、reconcile はこの action surface の候補である。
agent input への prompt は presentation action ではなく delivery engine の責務とする。

## Plugin Capability Policy

| capability class | managed lane disposition |
| --- | --- |
| read-only presentation | allow candidate |
| navigation / focus | allow candidate。live identity preflight必須 |
| preview-first safe action client | engine contract完成後にallow candidate |
| pane / DB / Redmineへの直接write | deny |
| agent input writer | deny。#14618のdelivery authorityを迂回させない |
| review verdict / owner approval / close / retire | deny |
| arbitrary build / remote artifact | #14619のpin / provenance preflight対象 |

0.7.5 では installed / enabled plugin state が session 単位ではなく user 単位
global である (#14614 j#91226)。従って「この workspace だけ enable」と表示または
推定しない。managed / unmanaged workspace が同じ user scope に存在する場合の
allow / deny と supply-chain preflight は #14619 が所有する。

## Engine-First Sequence

UX 実装を先行させない。ただし、engine を UI から利用不能な内部 APIだけで固める
こともしない。

1. 本書で state ownership、consumer contract、UX intent を固定する。
2. #14617 で protocol skew を fail-closed にし、pane-target 二段 launch と
   `interactive_ready` を確立する。
3. #14618 で native `agent.prompt` を atomic transport として採用し、
   exact-once / already-working admission / generation fence を core に維持する。
4. Unit / workflow / attention / desired-vs-live projection と、preview-first safe
   action を consumer-stable にする。
5. read-only の小さな sublane UX plugin spike で contract を検証する。
6. spike 後に本格 UI / UX を別 US として切る。

#14604 の Unit 列順 / 相対幅、#14567 の shared-tab topology、#14569 の pair ratio は
この UX の入力になるが、本書へ実装 scope を移さない。

## Current / Proposed Boundary

| surface | state at #14631 |
| --- | --- |
| Unit / Target、event、attentionの基本projection | existing |
| repo-local presentation seed / home-scoped presentation state first slice | existing |
| shared / separate表示方針、position / pin / hide等のschema | existing |
| Herdr 0.7.5 pane-target launch | proposed in #14617 |
| native atomic prompt transport | proposed in #14618 |
| Herdr plugin allow / deny preflight | proposed in #14619 |
| consumer-stable desired-vs-live projectionの完成 | proposed |
| preview-first plugin action API | proposed |
| read-only sublane UX plugin spike | proposed |
| 本格 UI / UX implementation | future separate US |

Redmine #15114 で、Herdr 0.8.0 の display metadata と plugin-owned popup を使う
read-only `mozyo Unit board` を最初の presentation consumer として実装した。
責務・role・project・lane work label の projection と display metadata 更新のみを
扱い、pane movement は #14605 系へ分離する。詳細は `herdr-unit-board.md` を読む。

## Non-Goals

- 本書で UI / plugin runtime を実装すること。
- community plugin を operator 環境へ install / enable すること。
- mozyo-bridge に arbitrary external plugin loader を追加すること。
- UI state を routing、review、approval、completion の正本にすること。
- #14617 / #14618 / #14619 / #14604 の実装を混ぜること。
- production publish、system Herdr update、operator HOME mutation。

## References

- `vibes/docs/logics/plugin-ready-adapter-boundary.md`
- `vibes/docs/logics/modular-config-driven-refactor.md`
- `vibes/docs/logics/unit-presentation-state-db.md`
- `vibes/docs/logics/iterm-webviewer-presentation-boundary.md`
- `vibes/docs/logics/pane-centric-cockpit-semantics.md`
- `vibes/docs/logics/delegated-coordinator-cockpit-display.md`
- `vibes/docs/logics/herdr-unit-board.md`
- Redmine #14614 journal #91226
- Redmine #14617 / #14618 / #14619 / #14604
