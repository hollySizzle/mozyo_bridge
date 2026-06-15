# Plugin-Ready Built-in Adapter Boundary

Redmine #12001。v0.8 で `mozyo-bridge` を plugin-ready に近づけるため、
external plugin API を公開する前に built-in adapter / provider 境界を分類する設計
正本。

この文書は実装 API ではない。arbitrary code plugin を読み込む仕組み、entry point、
third-party extension contract、互換性保証を公開しない。目的は、core に残すべき
最小 contract と、将来 adapter 化しやすい責務を分けることである。

## Decision

v0.8 の方針は **plugin system first** ではなく **built-in adapter boundary first**。

最初にやること:

- core contract を小さく定義する。
- Redmine / tmux / iTerm2 / YAML / SQLite / OTel などを「内蔵 provider」として分類する。
- provider 間で共有する data shape を明文化する。
- external plugin API は公開しない。

最初にやらないこと:

- third-party Python module を runtime load する。
- user-local arbitrary script を trusted provider として実行する。
- public ABI / semantic version compatibility を約束する。
- private operator policy を provider default として入れる。

## Why Not Arbitrary Code Plugin Yet

arbitrary code plugin は現時点では時期尚早である。理由は次の通り。

- security: provider は Redmine journal、local tmux、workspace path、release flow へ
  触れうる。permission model が無い状態で任意 code を load すると、credential /
  private path / destructive action の境界が曖昧になる。
- workflow authority: Redmine gate、owner approval、Codex audit、handoff routing は
  durable workflow の正本に近い。外部 plugin がここへ直接 write できると
  governance が破れる。
- compatibility: public plugin API を出すと、未成熟な data model でも長期互換を
  背負う。いまは pane-centric cockpit、attention、workspace registry、presentation
  state がまだ設計変動中である。
- distribution: skill / plugin marketplace / scaffold / pip package の配布経路が
  すでに複数ある。ここへ runtime plugin 配布を重ねると、support surface が太る。

したがって、v0.8 では external plugin API を約束せず、core 内の built-in provider
を replaceable な境界へ寄せる。

## Core Contracts

core に残すべき最小 contract は次である。

### Identity contract

- workspace identity: registry + workspace anchor
- lane identity: lane id / checkout id
- role identity: agent role
- runtime target: live pane id + preflight

これは provider へ委譲しない。provider は identity を読むことはできるが、独自の
identity 正本を作らない。

### Durable workflow contract

- issue / journal / comment / owner approval / review request
- handoff marker は durable anchor への pointer
- pane message は正本ではない

ticket provider はこの contract に data を供給する adapter であり、workflow の
意味そのものを勝手に定義しない。

### Presentation contract

- target projection: `agents targets` / TargetRecord / UnitRecord
- attention projection: AttentionRecord / `@mozyo_attention_*`
- event timeline: events envelope

UI provider は projection consumer である。iTerm / WebViewer / terminal layout は
routing、approval、completion の正本にならない。

### Storage contract

- identity: registry.sqlite + workspace anchor
- project docs catalog: YAML catalog + committed Markdown
- runtime / observed state: live tmux, OTel/event stores, projection cache
- desired presentation: future DB tables or event log

storage backend は data type ごとに違ってよい。単一 DB を全状態の正本にしない。

## Adapter Categories

### Ticket adapter

Examples:

- Redmine
- Asana
- future trackers

Core owns:

- durable anchor vocabulary
- review / close / owner approval boundary
- gate state names
- secret / private data rules

Provider owns:

- API calls
- issue / journal / comment fetch
- status update mechanics
- project / version lookup
- provider-specific URL formatting

Boundary:

- provider output must normalize into a small internal record:
  `IssueRef`, `JournalRef`, `CommentRef`, `WorkflowGate`, `OwnerApproval`.
- provider must not bypass role boundary. For example, a provider may expose
  `close_issue`, but core decides whether close approval has been satisfied.

First-candidate score: high. Redmine is already central and Asana history still exists in
docs. Extracting a built-in ticket adapter boundary reduces tracker coupling without
touching tmux geometry.

### Presentation adapter

Examples:

- tmux text / pane user options
- iTerm2 / WebViewer consumer
- future browser dashboard

Core owns:

- TargetRecord / UnitRecord / AttentionRecord
- event envelope
- command preview / confirm semantics
- public/private presentation boundary

Provider owns:

- how to render color, badge, pane title, border, WebViewer UI
- polling or projection cache mechanics
- local-only operator preferences

Boundary:

- presentation adapter is read/projection first.
- it must not define workflow truth, owner approval, or routing authority.
- iTerm-specific policy belongs in private consumer unless converted into generic contract.

First-candidate score: high, but implementation should start read-only. Cockpit dogfooding
needs it, and the pane-centric decision already separates identity from display.

### Terminal runtime adapter

Examples:

- tmux
- future PTY daemon / sidecar
- remote SSH terminal layer

Core owns:

- send safety contract
- target preflight vocabulary
- delivery outcome shape
- fail-closed behavior

Provider owns:

- concrete send / capture / pane listing mechanics
- foreground process inspection
- rendered text observation
- sidecar receiver signal implementation

Boundary:

- runtime adapter may observe liveness but must not become durable identity.
- completion truth should move toward machine-readable receiver signal when available.

First-candidate score: medium. The payoff is high, but send safety is risk-heavy. Do not
start v0.8 here unless ticket adapter / presentation boundary work exposes a small pure
interface first.

### Catalog backend adapter

Examples:

- committed YAML catalog
- generated file conventions
- future SQLite / DB-backed catalog index

Core owns:

- document id vocabulary
- canonical path / related refs semantics
- file convention matching
- generator ownership rules
- source-of-truth ordering for catalog, committed Markdown, generated artifacts,
  and optional local overlays

Provider owns:

- storage and indexing mechanics
- validation / query implementation details
- cache freshness metadata and rebuild mechanics
- read/query acceleration for resolver and audit tooling

Boundary:

- committed docs and catalog remain reviewable source of truth for governance docs.
- DB may cache / index, but must not silently replace committed rule docs.
- generated file conventions remain generator output from the catalog, not DB-owned
  source.
- local overlays remain checkout-local and must not be folded into public DB/index
  snapshots.

First-candidate score: low for v0.8 start. The temptation to centralize all static files into
DB is high, but docs/rules need diffability. Treat DB as index/cache until a concrete query
problem forces more.

#### Cache / Index Boundary (Redmine #12036)

The catalog backend provider may eventually be useful for faster lookup, richer audit
queries, or offline inspection, but it must stay below the governance source-of-truth
line. The provider can materialize an index from already-reviewable inputs; it cannot
be the place where governance decisions are authored.

Source-of-truth order:

1. Committed Markdown rule / design docs are the human-readable policy source.
2. `.mozyo-bridge/docs/catalog.yaml` maps those docs to ids, relationships, and
   file conventions.
3. Generated file conventions are reproducible output from the catalog.
4. A future DB / SQLite backend is a rebuildable cache or query index over those
   inputs.

Provider contract:

- build from committed docs, committed catalog, and generator output only;
- expose freshness, source commit, and rebuild status so stale results can fail closed;
- answer resolver / audit queries without changing the semantics of document ids,
  related refs, file convention matching, or generator ownership;
- keep local overlay data local-only and out of public snapshots;
- treat unreadable, stale, or contradictory index state as `unknown` / rebuild-required,
  not as a reason to bypass catalog validation.

Provider non-goals:

- no DB-authored rule docs;
- no silent replacement of committed Markdown or `.mozyo-bridge/docs/catalog.yaml`;
- no DB-to-generated-file write path that skips the existing generator;
- no public plugin API or arbitrary external backend loading;
- no workflow authority, owner approval, or routing authority.

Implementation split:

- docs-only boundary is enough for this issue;
- runtime DB / SQLite index creation, migration, query command, or cache invalidation
  requires a separate child issue and task-level review;
- any future implementation must keep `mozyo-bridge docs validate`,
  `generate-file-conventions --check`, and `audit-impact --check-generated` as the
  authoritative verification gates.

### Telemetry / runtime observer adapter

Examples:

- tmux capture-pane
- OTel events
- sidecar control events
- managed event log

Core owns:

- observed vs desired vs durable state separation
- attention derivation inputs
- unknown / stale / contradictory fail-safe semantics

Provider owns:

- event ingestion
- observation freshness
- runtime-specific metadata extraction

Boundary:

- observer output is input to a derivation model, not workflow truth by itself.
- unreadable / contradictory input must derive unknown, not healthy.

First-candidate score: medium. Useful after attention projection stabilizes, but not the
first split because observer semantics are still evolving.

### Release helper adapter

Examples:

- local git / build helpers
- GitHub Actions helper
- TestPyPI / PyPI publish helper
- governed preset release policy

Core owns:

- release gate vocabulary
- helper dry-run / execute boundary
- artifact hygiene requirements
- version mirror contract

Provider owns:

- concrete command execution
- CI provider status fetch
- package index publish mechanics

Boundary:

- helper automates mechanics, not release judgment.
- release notes and push / publish approval remain durable workflow decisions.

First-candidate score: low for adapterization. Existing release helper contract is already
well-bounded; turning it into provider architecture now would add more surface than it
removes.

## v0.8 First Adapterization Candidates

### Candidate 1: ticket adapter boundary

Pick this first if v0.8 needs a real architecture cut.

Reason:

- Redmine is currently central to workflow, while old Asana-era documents still exist.
- The desired abstraction is business-meaningful: issue, journal, gate, owner approval.
- It can start as built-in provider classification without external plugin loading.
- It helps future tracker support without changing cockpit geometry or tmux safety.

Acceptable MVP:

- define internal ticket records in docs/spec first;
- keep Redmine as the only provider implementation;
- move provider-specific wording toward adapter-owned code later;
- no third-party provider loading.

### Candidate 2: presentation adapter boundary

Pick this second, and keep it read-only/projection-first at the start.

Reason:

- cockpit / iTerm / WebViewer pressure is real in dogfooding.
- `pane-centric-cockpit-semantics.md` already says display is projection only.
- read-only projection can improve UI without risking routing authority.

Acceptable MVP:

- TargetRecord / UnitRecord / AttentionRecord remain core shapes;
- tmux user options and text output are one built-in projection provider;
- iTerm / WebViewer remains consumer until a generic loopback contract is needed;
- no UI state becomes owner approval or routing truth.

## Non-Candidates For The First Cut

- catalog backend replacement: keep YAML / Markdown diffability. DB can index later.
- terminal runtime abstraction: important but safety-sensitive. Do after the data contracts
  are smaller.
- release helper provider split: current helper contract is already constrained.
- arbitrary code plugin: blocked by security, governance, and compatibility concerns.

## Public / Private Boundary

Built-in adapter classification may be public-safe. It may name generic provider categories
and generic records.

It must not include:

- private project grouping policy
- private iTerm profile / color / shortcut defaults
- internal business workflow
- credential, token, cookie, API key, client secret
- private repository topology or personal path policy

## Implementation Guardrails

When this design moves from docs to code:

1. Start with pure records / protocol-like boundaries, not dynamic plugin loading.
2. Keep one built-in provider at first; do not invent test-only fake plugin loading as a
   public feature.
3. Provider code must not own approval semantics.
4. Provider failure must be explicit: unavailable, unauthorized, ambiguous, unknown.
5. Existing Redmine behavior must remain compatible until the new boundary is proven.
6. Any new provider surface that writes tickets, tmux, release state, or local files requires
   per-task review.

## Implemented Seam (Redmine #12034)

The first concrete cut of the ticket adapter boundary now exists in code. It is
deliberately the smallest seam that makes the design's record concepts explicit
while keeping the existing Redmine-governed workflow and the cockpit read model
byte-compatible.

### Where it lives

- `src/mozyo_bridge/domain/ticket_adapter.py` — **core**. Pure normalized
  records `IssueRef`, `JournalRef`, `CommentRef`, `WorkflowGate`,
  `OwnerApproval`; the `TicketProvider` protocol (the built-in provider
  boundary); the core-owned `WORKFLOW_GATE_KINDS` vocabulary; and the
  core-owned decisions `classify_workflow_gate` and `owner_approval`. No I/O,
  no network, no provider import — the dependency only ever points
  provider -> core.
- `src/mozyo_bridge/infrastructure/redmine_ticket_provider.py` — the built-in
  **Redmine provider**. It converts Redmine API JSON (the `/issues.json`
  object, the `journals` array) and the existing handoff `RedmineAnchor` into
  the normalized records, and owns Redmine-specific URL formatting. It performs
  no network call itself (the trusted-base / credential boundary stays in
  `redmine_context`) and owns no approval or gate semantics.
- `src/mozyo_bridge/redmine_context.py` — the cockpit Redmine read model now
  routes its API response through `RedmineTicketProvider.normalize_issue` and
  projects the record back onto the same minimized `latest_issue` payload
  (numeric id preserved, subject still never surfaced).

### Boundary as enforced in code

- The gate vocabulary is the durable-record subset of the handoff
  `KIND_LABELS` (`implementation_done`, `review_request`, `review_result`),
  sourced from `handoff` so the two cannot diverge. A provider cannot add gate
  names; `WorkflowGate` is only constructible through `classify_workflow_gate`.
- Owner close approval is **not** a gate. Reaching a gate is a
  provider-observable journal fact; "close approval is satisfied" is a core
  decision produced only by `owner_approval`. The built-in provider exposes no
  approval API at all, and tests pin that.

### Non-goals (unchanged, restated for the implementation)

- No third-party or arbitrary-code provider loading; Redmine is the only
  provider implementation.
- No public ABI or long-term compatibility promise for these record shapes —
  they are internal and may change.
- No provider-defined workflow truth, gate names, or approval semantics.
- No second place that sends the Redmine API key anywhere — normalization is
  pure over already-fetched data.

## Follow-up Split

- #12002 should use this document when splitting `commands.py` / `cli.py`: separate core
  command orchestration from provider mechanics.
- #12003 should use this document when defining runtime observability: observer provider
  output is input, not truth.
- #11826 should treat this as the first architecture ledger for v0.8, not as a promise that
  external plugins are supported.
