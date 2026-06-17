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

## Internal Provider Registry Skeleton (Redmine #12035)

The ticket adapter seam (#12034) classified *one* provider. #12035 adds the
smallest place to classify *all* built-in providers, so future ticket /
presentation / catalog / telemetry providers have a home before they are
written. It is a classification skeleton, not a plugin system.

### Where it lives

- `src/mozyo_bridge/domain/provider_registry.py` — **core**, pure. It defines
  the core-owned `ProviderCategory` vocabulary (`ticket`, `presentation`,
  `terminal_runtime`, `catalog`, `telemetry`, `release_helper` — the design
  doc's Adapter Categories), the frozen `BuiltinProvider` *description*
  (category, provider id, capabilities, safety constraints, experimental flag),
  the in-memory `BuiltinProviderRegistry`, and a module-level
  `BUILTIN_PROVIDER_REGISTRY` seeded with the providers this codebase actually
  ships today (`redmine` ticket, `tmux` terminal runtime). It imports no
  provider implementation, so the dependency only ever points provider -> core.

### Internal-only, by construction

- **No external plugin loading.** Registration takes a pure `BuiltinProvider`
  description, never a module path or callable, so no registration path can
  import, load, or execute foreign code. There is no entry point, no
  third-party contract, and no user-script load. This is the explicit non-goal:
  external plugin loading is out of scope.
- **No public ABI / compatibility promise.** The category names and record
  shapes are internal and may change with no deprecation window.
- **Empty categories are still expressible.** A category with no built-in
  provider yet (`catalog`, `telemetry`, …) is a valid classification a future
  provider slots into — that is the point of a skeleton. No placeholder
  provider is invented for an unwritten category.

### Authority stays core-owned

The registry classifies providers; it does not hand them authority.
`FORBIDDEN_PROVIDER_AUTHORITIES` enumerates the decisions core never delegates —
`workflow_authority`, `owner_approval`, `close_approval`, `routing_authority` —
and a `BuiltinProvider` that lists any of them as a capability is rejected at
construction (`ProviderRegistryError`). This is the same boundary the ticket
seam enforces functionally (`classify_workflow_gate` / `owner_approval` are core
functions, never provider methods); the registry makes it a checked invariant
for every classified provider. Tests pin both the forbidden set and the
rejection.

## Implemented Presentation Seam (Redmine #12156)

The presentation adapter boundary's first concrete cut (the v0.8 "Candidate 2"
slice) now exists in code, deliberately the smallest read-only/projection-first
seam that makes the design's presentation-provider concept explicit without
giving display any routing or approval authority.

### Where it lives

- `src/mozyo_bridge/domain/presentation_adapter.py` — **core**, pure. The
  recognized projection-surface vocabulary (`tmux_user_option`, `text`); the
  normalized records `ProjectionField` and `SurfaceProjection`; and the
  `PresentationProvider` protocol (the built-in presentation-provider boundary).
  It imports no provider implementation, so the dependency only ever points
  provider -> core. The core projection shapes (`TargetRecord` / `UnitRecord` /
  `AttentionRecord`, the event envelope) are unchanged and stay core-owned.
- `src/mozyo_bridge/application/tmux_attention_presentation_provider.py` — the
  built-in **tmux presentation provider**. It projects an already-derived
  `AttentionRecord` into a `SurfaceProjection` for the `tmux_user_option`
  surface, reusing the canonical `@mozyo_attention_*` option names from
  `attention_projection` (Redmine #11954) as the single source of truth. It runs
  no tmux I/O; the `set-option` argv mechanics stay in `attention_projection`.
- `src/mozyo_bridge/domain/provider_registry.py` — the `presentation` category
  is no longer empty: the registry classifies `tmux-presentation` (matching the
  provider's `name`) with `projection_only` / `no_routing_authority` /
  `no_owner_approval_decision` safety constraints.

### Boundary as enforced in code

- **Projection only.** `SurfaceProjection` rejects any field whose key names a
  core-owned authority. The forbidden set is reused verbatim from the registry
  seam's `FORBIDDEN_PROVIDER_AUTHORITIES` (`workflow_authority`,
  `owner_approval`, `close_approval`, `routing_authority`), so a projection and a
  registered provider are checked against the same list and cannot drift. Display
  state therefore can never become workflow, owner, close, or routing truth.
- **No invented surfaces.** A provider cannot construct a projection on an
  unrecognized surface; the surface vocabulary is core-owned.
- **Read-only protocol.** `PresentationProvider` exposes only `project`; there is
  no `send` / `route` / `approve` method, and tests pin that the tmux provider
  grows none. The option values remain a re-derivable cache, never consulted for
  routing / handoff preflight (unchanged from #11954).

### Non-goals (unchanged, restated for the implementation)

- No third-party or arbitrary-code provider loading; the tmux projection is the
  only presentation provider implementation.
- No public ABI or long-term compatibility promise for these record shapes.
- No presentation-defined workflow truth, owner approval, or routing authority;
  iTerm / WebViewer stay consumers until a generic loopback contract is needed.

## Internal CLI Module Registry / Configuration-Aware Baseline (Redmine #12155)

The provider registry (#12035) classifies built-in *providers*. #12155 adds the
parser-composition analogue: an internal, built-in **CLI command family**
registry so `build_parser()` composes the family modules from a registry instead
of a hand-ordered inline sequence, and so the codebase has a configuration-aware
baseline (module selection / feature flags) before any external plugin surface
exists. It is a classification + composition skeleton, not a plugin system.

### Why now

The feature-family parser split (#12153 / #12154) already moved each command
family into its own module with a `register(sub)` entry point. Those modules
were still wired into `build_parser()` by a fixed, hand-ordered call sequence.
#12155 turns that sequence into a registry the core walks, which (a) makes the
"core small and hard, families addable/swappable" goal concrete, and (b) gives a
single place to express *which* built-in families a composition includes —
without inventing the machinery a real plugin system would need.

### Where it lives

- `src/mozyo_bridge/domain/module_registry.py` — **core**, pure. It defines the
  frozen `CliFamily` *description* (name, summary, the core-owned authorities the
  family's commands participate in, `core` / `experimental` flags), the
  `CliCompositionConfig` (module-selection-only config), the insertion-ordered
  `BuiltinCliModuleRegistry`, and `CORE_OWNED_AUTHORITIES`. It imports no
  application or argparse code; the dependency only points application -> domain,
  exactly like `provider_registry`.
- `src/mozyo_bridge/application/cli_modules.py` — the application-layer binding.
  It maps each classified family *name* to the built-in registrar callable that
  adds its subparsers (`cli_core` plus the feature-family modules), seeds
  `BUILTIN_CLI_MODULE_REGISTRY` in the exact pre-registry order, and exposes
  `compose_parser(sub, config)`.
- `src/mozyo_bridge/application/cli_core.py` — the residual inline `build_parser()`
  blocks (status/list, pane I/O, keys, init/doctor/sublane), moved verbatim into
  four ordered registrars so the core command set composes through the registry
  like the feature families. `build_parser()` now only builds the root options
  and calls `compose_parser(sub)`.

### Internal-only, by construction

- **No external plugin loading.** The registry classifies families by pure
  `CliFamily` descriptions; the name -> registrar binding references only
  statically-imported built-in functions, never a runtime-resolved module path,
  entry point, or user script. Composing the CLI can never import or execute
  foreign code. This is the explicit non-goal: arbitrary external plugin loading
  / dynamic registration is out of scope.
- **No public ABI / compatibility promise.** Family names, the config shape, and
  the record shapes are internal and may change with no deprecation window.
- **Default composition is behavior-preserving.** The seeded order reproduces the
  prior inlined `build_parser()` subcommand sequence exactly; the default config
  disables nothing, so the full recursive `--help` tree is byte-identical to the
  pre-registry CLI.

### Authority stays core-owned (config cannot weaken it)

The configuration surface is limited to module selection / feature flags — a
config may name non-mandatory families to disable, nothing more. It cannot
reorder, add a family, supply a registrar, or grant authority.
`CORE_OWNED_AUTHORITIES` enumerates the decisions config never makes
configurable — `workflow_authority`, `owner_approval`, `review_authority`,
`close_approval`, `send_safety`, `routing_authority`. A family that carries any
of them (the send / handoff / routing / release families) and the hard core
command set are **mandatory**: `resolve_enabled` rejects a config that tries to
disable a mandatory family (and one that names an unknown family), so owner
approval / review / close / send safety can never be configured away. This is the
CLI-composition counterpart to the provider registry's
`FORBIDDEN_PROVIDER_AUTHORITIES` rejection. Tests pin the mandatory set and the
rejection.

### Provider selection vs module selection

"Provider selection" in the configuration scope is the provider-registry concern
(#12035); this CLI module registry owns "module selection / feature flags" for
parser families. The two registries are deliberately separate: one classifies
adapter providers, the other composes command families. Neither exposes an
external plugin API.

## Follow-up Split

- #12002 should use this document when splitting `commands.py` / `cli.py`: separate core
  command orchestration from provider mechanics.
- #12003 should use this document when defining runtime observability: observer provider
  output is input, not truth.
- #11826 should treat this as the first architecture ledger for v0.8, not as a promise that
  external plugins are supported.
