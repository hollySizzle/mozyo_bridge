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
- receiver / assistant-turn observability should move toward machine-readable runtime
  signal when available; task completion truth remains the ticket workflow record.

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
- `src/mozyo_bridge/application/text_attention_presentation_provider.py`
  (Redmine #12185) — the built-in **text presentation provider**, the second
  built-in provider. It projects the same already-derived `AttentionRecord` onto
  the `text` surface, using stable human-readable label keys (`state` /
  `severity` / `reason` / `updated_at`) that carry the same four logical cells,
  in the same order, as the tmux projection, so the two surfaces cannot drift in
  which facts they show. It also exposes `render_surface_text`, a pure renderer
  that turns *any* `SurfaceProjection` (tmux or text) into a deterministic
  `key: value` text block — the "text output" half of the presentation MVP. It
  runs no I/O and, to minimise #12184 merge risk, does not register itself in
  `provider_registry`; it stands on its own as a built-in projection.
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

- No third-party or arbitrary-code provider loading; the built-in tmux and text
  (Redmine #12185) projections are the only presentation provider
  implementations.
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

## Internal Provider Selection Config (Redmine #12184)

The provider registry (#12035) classifies built-in *providers* and the CLI
module registry (#12155) added module selection for command families. #12184
adds the missing provider-side piece the v0.9 direction calls for
(`vibes/docs/logics/modular-config-driven-refactor.md`, "provider selection for
built-in adapters and backends"): the smallest typed config that *selects* a
built-in provider per category, resolved fail-closed against the registry. It is
internal-only selection of already-registered built-ins, not a plugin loader.

### Where it lives

- `src/mozyo_bridge/domain/provider_registry.py` — **core**, pure. It adds the
  frozen `ProviderSelectionConfig` (a `category -> chosen provider id` typed
  record, normalized to sorted pairs) and the registry resolvers
  `resolve_selection` (all populated categories) / `resolve_provider` (one
  category). No new I/O, import, or provider code — the dependency still only
  points provider -> core.

### Behavior-preserving default

- The default config (empty `selections`) resolves every populated category to
  its current built-in default — the sole provider registered in that category
  (`ticket` -> `redmine`, `terminal_runtime` -> `tmux`, `presentation` ->
  `tmux-presentation`). Empty categories (`catalog` / `telemetry` /
  `release_helper`) simply have no resolution. Current built-in behavior is
  unchanged when no config is supplied.

### Internal-only, by construction

- A selection may only name a `provider_id` **already registered** in the
  built-in registry and sitting in the selected category. There is no module
  path, callable, entry point, or dynamic import — selection can never introduce
  foreign code, the same non-goal the registry skeleton enforces.
- No public ABI / compatibility promise; the config shape and category vocabulary
  are internal and may change with no deprecation window.

### Fail-closed surface

`ProviderSelectionConfig` rejects, at construction, a non-mapping/ill-typed
record, an unknown top-level key (`from_record`), and any key/value naming a
member of `FORBIDDEN_PROVIDER_AUTHORITIES` (authority-shaped fields). The
registry rejects, at resolution, an unknown category, an unknown provider id, a
category/provider mismatch, and an ambiguous category (more than one provider,
no selection — no implicit default). Authority stays core-owned: this is
selection of classified providers, never a grant of `workflow_authority`,
`owner_approval`, `close_approval`, or `routing_authority`.

## Repo-Local YAML Config Wiring (Redmine #12189 / #12190 / #12191 / #12249)

The registries above (#12155 module selection, #12184 provider selection) define
*typed* config records but no on-disk source. The v0.9.1 batch adds the
repo-local YAML source and connects it to the CLI, in three staged lanes that
keep the existing fail-closed boundaries:

- **#12189 — schema.** `src/mozyo_bridge/domain/repo_local_config.py` defines the
  closed top-level record for `.mozyo-bridge/config.yaml`: `version`, `cli`,
  `providers`, `presentation` only. `cli` -> `CliCompositionConfig` (#12155),
  `providers` -> `ProviderSelectionConfig` (#12184), `presentation` ->
  `PresentationSelectionConfig` (projection-surface selection only:
  `tmux_user_option` default, `text` optional). It does no file IO; an unknown
  key, unsupported version, non-mapping record, or any module / callable / entry
  point / authority / target / pane / credential-shaped field fails closed
  through `RepoLocalConfigError`.
- **#12190 — loader.** `src/mozyo_bridge/application/repo_local_config_loader.py`
  is the thin file-IO/parse layer: it resolves `.mozyo-bridge/config.yaml` under
  the standard repo root, reads it with `yaml.safe_load` only, and hands the
  parsed mapping to the schema. A missing or empty file is the
  behavior-preserving default; a malformed document or unreadable present file is
  re-raised as `RepoLocalConfigLoadError` (a `RepoLocalConfigError` subclass), so
  one `except RepoLocalConfigError` at the call site catches every
  repo-local-config failure — parse, IO, and schema.

### #12191 — CLI composition entrypoint wiring

`main()` (in `src/mozyo_bridge/application/cli.py`) reads the repo-local config
through the #12190 loader and composes the parser from it. The contract:

- `build_parser(config=None)` now takes an optional `RepoLocalConfig`. With
  `config is None` — the default, and every direct `build_parser()` caller in the
  codebase and tests — it composes the full CLI exactly as before, so the change
  is transparent to existing callers. With a config it threads `config.cli` into
  `compose_parser(sub, config.cli)`; `main()` is the only caller that supplies one.
- **Config source honors the root-level `--repo`.** Composition must be decided
  before argparse parses the real arguments, yet the config lives under the repo
  root that the documented root-level `--repo` may override. `main()` resolves
  that override first (`_root_repo_override`, a tiny pre-parser that reads only
  the root-level `--repo` and routes everything from the subcommand onward into a
  REMAINDER tail), then loads the config from that root. So
  `mozyo-bridge --repo <target>` reads `<target>/.mozyo-bridge/config.yaml`,
  preserving the `--repo` contract; a *subcommand-local* `--repo` applies to that
  command and never changes which families compose.
- **Config-absent is unchanged.** A repo with no `.mozyo-bridge/config.yaml`
  resolves to `RepoLocalConfig.default()`, whose `cli` disables nothing, so the
  top-level help / subcommand tree is byte-identical to the pre-#12191 CLI.
- **Config-present may disable only a non-mandatory CLI family.** Disabling an
  optional family drops exactly its subcommands; the registry's `resolve_enabled`
  still rejects disabling a mandatory (core / authority-bearing) family, so owner
  approval / review / close / send safety stay non-configurable.
- **Fail-closed with actionable text.** A parse / schema / family-resolution
  failure (`RepoLocalConfigError` or `ModuleRegistryError`) is converted by
  `main()` into a single actionable stderr line (what failed, the config path,
  how to recover) and exit code `2` — never a raw traceback and never a silent
  fall-through to the default CLI. Fail-closed is global: a broken config blocks
  even `--version`, so a misconfigured repo cannot run any subset of the CLI as
  if the config were valid.
- **Staged scope.** At #12191 only the `cli` family composition is wired at the
  parser entrypoint, because that is the surface with a live composition seam.
  `providers` and `presentation` are read and validated by the loader, but have
  no runtime resolution seam yet (the provider registry is not consumed at
  runtime and the presentation providers hardcode their surface); wiring their
  selection into runtime resolution is a later stage (`providers` lands in
  #12249, below). No dynamic import, entry point, callable, module path, or
  external plugin API is introduced — the same non-goal the registries already
  enforce.

### #12249 — provider selection runtime resolution

`main()` (in `src/mozyo_bridge/application/cli.py`) now also resolves the
repo-local `providers` selection against the live built-in provider registry,
closing the staged gap above. This is the first time
`BUILTIN_PROVIDER_REGISTRY` is consumed at runtime. The contract mirrors the
#12191 CLI family resolution:

- **The resolution seam.** A thin application module,
  `src/mozyo_bridge/application/provider_runtime.py`, exposes
  `resolve_builtin_providers(config)`, which delegates to
  `BUILTIN_PROVIDER_REGISTRY.resolve_selection`. `main()` calls it on
  `config.providers` inside the same try-block that composes the parser, so the
  provider selection is resolved at the same entrypoint and from the same
  `--repo`-honoring config source as the CLI family selection.
- **Config-absent / default is unchanged.** The default (empty) selection
  resolves every populated category to its current built-in default
  (`ticket` -> `redmine`, `terminal_runtime` -> `tmux`, `presentation` ->
  `tmux-presentation`), so a repo with no `.mozyo-bridge/config.yaml`, or one
  whose `providers` block is absent, runs byte-identically to before. No
  provider dispatch path consumes the resolved mapping yet; the connection is
  the fail-closed *resolution* seam, the provider analogue of how the read layer
  resolves CLI families purely for the validation side effect.
- **Config-present fails closed on an unrealizable selection.** Schema
  validation (`ProviderSelectionConfig`) already rejects the exact core-owned
  authority names and module / callable / target-shaped tokens; runtime
  resolution additionally rejects what shape-only validation cannot see — a
  selection naming an unknown provider id, an unknown category, or a registered
  provider in a different category than the one selecting it. Each raises
  `ProviderRegistryError`, which `main()` converts (alongside
  `RepoLocalConfigError` / `ModuleRegistryError`) into the same single actionable
  stderr line and exit code `2`, never a raw traceback and never a silent
  fall-through.
- **No new machinery.** The registry maps ids to pure `BuiltinProvider`
  *descriptions*, never to a module path, callable, or entry point, so the
  connection introduces no dynamic import, no public extension ABI, and no
  delegation of `workflow_authority` / `owner_approval` / `close_approval` /
  `routing_authority` — the same boundary the provider registry already enforces.
- **Still-staged.** `presentation` selection remains read-and-validated only at
  #12249; its runtime resolution lands next in #12251 (below). The
  `terminal_runtime` and `ticket` providers are resolvable through the same
  registry call but are still consumed at their existing call sites
  (`REDMINE_TICKET_PROVIDER`, direct `run_tmux`); routing those call sites
  through the resolved provider is a later stage, out of #12249 scope.

### #12251 — presentation selection runtime resolution

`main()` (in `src/mozyo_bridge/application/cli.py`) now also resolves the
repo-local `presentation` selection to the built-in projection provider that
owns the selected surface, closing the last staged gap above. The contract is
the presentation analogue of the #12249 provider resolution:

- **The resolution seam.** A thin application module,
  `src/mozyo_bridge/application/presentation_runtime.py`, exposes
  `resolve_presentation_provider(config)`, which maps the configured surface to
  the built-in `PresentationProvider` that already projects onto it. The
  surface -> provider table is built from the providers' own `surface`
  attributes (`tmux_user_option` -> `tmux-presentation`, `text` ->
  `text-presentation`), so the resolution and the providers can never drift
  apart. `main()` calls it on `config.presentation` inside the same try-block
  that composes the parser and resolves provider selection, from the same
  `--repo`-honoring config source.
- **Config-absent / default is unchanged.** The default surface
  (`tmux_user_option`) resolves to the tmux presentation provider, so a repo
  with no `.mozyo-bridge/config.yaml`, or one whose `presentation` block is
  absent, projects byte-identically to before. A realizable non-default
  selection (`text`) resolves to the existing text provider — not a new one. No
  projection dispatch path consumes the resolved provider yet; the connection is
  the fail-closed *resolution* seam, exactly like the provider resolution.
- **Config-present fails closed on an unrealizable selection.** Schema
  validation (`PresentationSelectionConfig`) already rejects any surface outside
  the core-owned `PRESENTATION_SURFACES` vocabulary, plus target / pane / route /
  send / approve / credential-shaped keys, so an unknown or authority-shaped
  surface never reaches runtime resolution (it surfaces as
  `RepoLocalConfigError`). Runtime resolution additionally fails closed on what
  shape-only validation cannot see — a core-recognized surface with no built-in
  provider — raising `PresentationRuntimeError`, which `main()` converts
  (alongside the other repo-local config errors) into the same single actionable
  stderr line and exit code `2`, never a raw traceback and never a silent
  fall-through.
- **No new machinery, projection-first.** The providers are imported built-in
  singletons, never a module path / callable / entry point, so the connection
  introduces no dynamic import, no public extension ABI, and no delegation of
  workflow / owner approval / close / routing authority. A presentation selection
  can only choose *how* core records are displayed, never *what* is true — the
  read / projection-first boundary the presentation adapter has enforced since
  #12156.

## Static Plugin Manifest Schema / Validator (Redmine #12250)

The registries and config records above classify and select *built-in* providers.
#12250 adds the first piece aimed at a *future external* plugin: a static,
non-executable **plugin manifest schema / validator** that lets the codebase
*describe and review* a candidate plugin as declarative metadata, long before any
runtime loading exists. It is review metadata, not a plugin loader.

### Where it lives

- `src/mozyo_bridge/domain/plugin_manifest.py` — **core**, pure. It defines the
  closed `PluginManifest` record (`plugin_id`, `summary`, `categories`,
  `capabilities`, `declared_permissions`, `safety_constraints`, `experimental`,
  `manifest_version`), the `PluginManifestError` fail-closed error, and the
  validator entry point `validate_plugin_manifest(record)` /
  `PluginManifest.from_record(record)`. It imports only the sibling
  provider-registry vocabulary (`ProviderCategory`,
  `FORBIDDEN_PROVIDER_AUTHORITIES`), so the dependency only points within the
  domain layer. It does **no** file IO and runs **no** manifest code — the
  validator reads an already-parsed mapping.

### Declarative-only, by construction

- **No execution, ever — and not merely by omission.** The manifest carries
  declarative metadata only; there is no dynamic import, entry point, callable,
  shell command, install / build / run hook, or runtime loading. Any *key* shaped
  like one (`import` / `module` / `entry_point` / `callable` / `exec` / `eval` /
  `script` / `shell` / `command` / `subprocess` / `spawn` / `install` /
  `uninstall` / `build` / `run` / `hook` …) is rejected at validation, at any
  nesting depth, through `PluginManifestError`. The same executable-behavior token
  set is also rejected as a *label value* in `capabilities`,
  `safety_constraints`, and `declared_permissions` — these fields are exactly
  where a plugin would otherwise spell executable behavior as a string (e.g.
  `"dynamic_import"` / `"shell_exec"` / `"entry_point_loader"`), so a behavior
  label fails closed there too (fail-closed regardless of a `no_` prefix, which
  the validator does not interpret). This is the explicit non-goal of the design
  doc made into a checked invariant.
- **No invented categories.** A claimed `category` must be a known core-owned
  `ProviderCategory` value; the category vocabulary stays core-owned exactly as
  for the provider registry.
- **No public ABI / compatibility promise.** The closed key set, the category
  vocabulary, and the record shapes are internal and may change with no
  deprecation window.

### Fail-closed boundary surface

- **Private path / secret value.** Any string — key or value, at any depth — that
  looks like an absolute / home / drive filesystem path, or that names a
  credential (token / secret / password / api key / credential …), is rejected. A
  static review manifest declares no paths and carries no secrets.
- **Authority-shaped permission.** A `declared_permission` that names a core-owned
  authority — workflow / owner / close / review / routing / send — or a
  destructive / install / shell behavior is rejected. The exact forbidden set is
  sourced from the provider registry's `FORBIDDEN_PROVIDER_AUTHORITIES`, so a
  manifest permission and a registered provider are screened against the same
  core-owned list and cannot drift. Authority stays core-owned; the manifest can
  describe a plugin, never grant it authority.

### No second source of truth for packaging metadata

A plugin's packaging identity (`name` / `version` / `description` / `author` /
`owner` / `license` / `homepage` / `repository` / `keywords` / `source` /
`category`) already has a source of truth in `.claude-plugin/marketplace.json`
and `plugins/*/.claude-plugin/plugin.json` (covered by
`tests/test_plugin_marketplace.py`). This review manifest stores **none** of
them: such a key is rejected with a dedicated "duplicate packaging metadata"
message, so there is no duplicated field and no sync obligation. `plugin_id` is a
free correlation handle for review, deliberately *not* bound to the packaging
`name`, so it introduces no drift risk either — satisfying the acceptance rule
"no duplicate field without a sync/check story" by simply not duplicating.

### Non-goals (unchanged, restated for the implementation)

- No plugin install command, no runtime loading, no dynamic provider
  registration — the manifest is inert metadata.
- No stable public API / ABI promise for the record shapes.
- No second packaging-metadata source; no workflow / owner / close / routing /
  send authority granted to a manifest.

## Implemented Delivery-Record Persistence Seam (Redmine #12311)

The handoff primitive already produced the structured `DeliveryOutcome` and the
pasteable `build_delivery_record` markdown, but persisting that record into the
durable ticket system (a Redmine journal note / an Asana comment) was a manual
paste step left as a follow-up. #12311 adds the *core-owned persistence
boundary* for it — the smallest fail-closed seam that makes the integration
explicit while keeping the existing send byte-compatible and writing no ticket.

### Where it lives

- `src/mozyo_bridge/domain/delivery_record_sink.py` — **core**, pure. The
  record-class constant `RECORD_CLASS_DELIVERY` (`delivery_notification`); the
  normalized `DeliveryRecordNote` (built from a `DeliveryOutcome` + the redacted
  record body) and `DeliveryReceipt`; the explicit failure vocabulary
  (`PERSIST_FAILURE_REASONS`); the `DeliveryRecordSink` protocol and the narrow
  `RedmineNoteTransport` write seam; the built-in sinks (`Null`, `Unsupported`,
  `Unwired`, `Redmine`); and the fail-closed `resolve_delivery_record_sink`. It
  imports no provider implementation and performs no I/O — the dependency only
  points provider -> core, like `ticket_adapter`.
- `src/mozyo_bridge/application/commands.py` — `orchestrate_handoff` gains the
  opt-in `_maybe_persist_delivery_record` call on the typed terminal paths
  (`pending_input` / `sent`), behind `--persist-delivery`, plus `_emit_receipt`.

### Boundary as enforced in code

- **A delivery record is a notification pointer, never an authority.**
  `DeliveryRecordNote` fail-closes (`DeliveryRecordError`) on any
  `record_class` other than `delivery_notification`, so a persisted record can
  never be smuggled in as a workflow gate (`implementation_done` /
  `review_request` / `review_result`) or an owner approval — those stay the
  separate `WorkflowGate` / `OwnerApproval` core constructs. The receipt carries
  no gate/approval semantics either.
- **Source semantics are not mixed.** A Redmine note is a journal note; an Asana
  note is a comment. The Redmine sink refuses a non-Redmine note
  (`unsupported_source`); resolution maps `source=asana` to the fail-closed
  `UnsupportedSourceDeliveryRecordSink`. v0.8 keeps Redmine as the only write
  provider category.
- **No credential, ever.** Neither the note nor the receipt carries a token, API
  key, base URL, or any secret; the note body is the already-redacted pasteable
  record (`build_delivery_record` keeps absolute / private paths out of pasteable
  text). The opt-in durable sink path additionally renders the body WITHOUT the
  user-supplied free-text `--record-command` (Finding 1, j#62549) — that field
  can carry a private path or credential-shaped argument, so it stays in the
  printed stdout record for human audit-replay but is never auto-journaled. The
  seam does no credential handling at all.
- **Explicit failure.** A transport reports failure through
  `DeliveryTransportError` with a reason normalized to `PERSIST_FAILURE_REASONS`
  (`provider_unavailable` / `credential_missing` / `unauthorized` /
  `unsupported_source` / `no_anchor` / `transport_error`) — never a silent
  success (Implementation Guardrail #4).

### Staged: live write was a deferred follow-up (wired in #12347)

At #12311, per Implementation Guardrail #6 (any new provider surface that
**writes tickets** requires per-task review) and `redmine_context`'s
read-only-by-design boundary, the live, credential-gated Redmine journal-write
transport was deliberately **not** wired: production resolved to
`UnwiredDeliveryRecordSink` (`provider_unavailable`) — the same staged-resolution
posture as provider selection (#12249/#12251) — and the full persisted path was
exercised in tests through an injected `RedmineNoteTransport` fake, so no network
ran. That live transport is now implemented under #12347 (next section); the
staged fallback remains the byte-compatible default whenever the explicit
live-write opt-in is unset.

### Non-goals (unchanged, restated for the implementation)

- the pane message is never the durable source of truth; persistence is an
  opt-in, best-effort pointer that never blocks or alters the send;
- a delivery ACK is never task completion / review / approval;
- no credential is ever logged, journaled, or carried on a record / receipt;
- no third-party / arbitrary-code provider loading; no public ABI promise for
  these record shapes.

## Wired Live Redmine Delivery-Record Transport (Redmine #12347)

#12347 wires the live, credential-safe Redmine journal-write transport the
#12311 seam left staged, closing the deferred follow-up under its own per-task
review and direct owner close approval. It is the smallest live write that keeps
every #12311 invariant: a persisted delivery record stays notification metadata,
no user-supplied free text is auto-journaled, and no credential reaches a repo
file, log, or journal.

### Where it lives

- `src/mozyo_bridge/infrastructure/redmine_note_transport.py` — the built-in
  **Redmine note write provider**, the single concrete `RedmineNoteTransport`
  implementation (`RedmineNoteHttpTransport`). It performs the
  provider-owned network write only; core (`delivery_record_sink`) still owns the
  record class, source semantics, and receipt shaping. The dependency points
  provider -> core (it imports the core failure vocabulary, never the reverse).
- `src/mozyo_bridge/application/commands.py` — `_maybe_persist_delivery_record`
  builds the transport from the environment for a Redmine-sourced outcome and
  injects it into `resolve_delivery_record_sink`; the persistence call stays
  best-effort and never alters the pane send.

### Explicit opt-in (the "明示 opt-in")

The live network write is gated **twice**, so it is always a deliberate decision
and a plain `--persist-delivery` stays byte-compatible:

1. `--persist-delivery` selects the persistence seam (the existing #12311 CLI
   opt-in).
2. `MOZYO_REDMINE_DELIVERY_WRITE` (an explicit truthy env value) enables the live
   write. The second gate lives in the **trusted environment**, not a repo file
   or CLI argument, so it sits inside the same boundary as the credentials — a
   hostile checkout can never turn `--persist-delivery` into a live write.

When the env opt-in is unset, `redmine_delivery_transport_from_env()` returns
`None`, resolution falls back to `UnwiredDeliveryRecordSink`, and the receipt is
the byte-compatible `provider_unavailable`.

### Credential boundary (reused verbatim from `redmine_context`)

- The trusted base URL comes **only** from `MOZYO_REDMINE_URL` (routed through
  `normalize_base_url`), so the write destination host is fixed by the daemon
  environment and nothing else. Only the issue id — the URL path, taken from the
  durable handoff anchor on that same trusted Redmine — is caller-supplied, and
  it is percent-quoted so it cannot inject a host/query segment.
- The API key comes only from `MOZYO_REDMINE_API_KEY`, is sent only in the
  request header, and is never echoed into a payload, log, receipt, or the
  `DeliveryTransportError` reason. Credentials are read lazily at write time.

### Fail-closed surface

Every failure is an explicit `DeliveryTransportError` reason
(`PERSIST_FAILURE_REASONS`), surfaced through the existing
`RedmineDeliveryRecordSink` reason-mapping, never a silent success:

- no/invalid trusted base URL -> `provider_unavailable`;
- no API key -> `credential_missing`;
- HTTP 401 / 403 -> `unauthorized`;
- any other HTTP status, network error, or unexpected failure -> `transport_error`.

A successful notes-only `PUT /issues/<id>.json` returns `204 No Content` with no
journal id, so the transport returns the empty id and the sink records a
`redmine:issue=<id>` location pointer.

### Non-goals (unchanged, restated for the implementation)

- the delivery record is never review / completion / approval / close truth; the
  transport writes a `delivery_notification` journal note only;
- Asana live write is out of scope — `source=asana` still fails closed with
  `unsupported_source` (journal vs comment semantics are not mixed);
- no third-party / arbitrary-code provider loading; this is the single built-in
  write provider for v0.8, with no public ABI promise;
- no release / publish / tag.

## Implemented Terminal Runtime Transport Seam (Redmine #13245)

The "Terminal runtime adapter" category above was deliberately deferred (medium
first-cut score: high payoff, send-safety risk-heavy). #13245 lands its first
concrete cut, now that the ticket (#12034) and presentation (#12156) seams have
proven the "small pure port, one built-in provider, fail-closed" shape, and now
that the #13175 herdr PoC
(`vibes/docs/logics/herdr-poc-13175-experiment-log.md`) has validated a second
terminal backend candidate end-to-end. It is a **staged seam**: a stable port +
a pure fail-closed herdr adapter + a default-off backend selection, so the
follow-up herdr state (#13246) / identity (#13247) / turn-start (#13248) US's
have a fixed interface to build on. The existing tmux path is untouched.

### Where it lives

- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/domain/terminal_transport.py`
  — **core**, pure. The core-owned backend vocabulary (`tmux` default /
  `herdr`), the pane-read source vocabulary (`visible` / `recent` /
  `recent-unwrapped`, PoC E11), the closed `TRANSPORT_FAILURE_REASONS` set, the
  target guard (`valid_target`), the fail-closed result records
  (`TransportResult` / `PaneReadResult`, with an enforced ok/reason invariant),
  the three-primitive `TerminalTransportPort` protocol
  (`send_text` / `send_keys` / `read_pane`), and the default-off
  `TerminalTransportConfig`. It imports no provider, so the dependency only ever
  points provider -> core.
- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/infrastructure/herdr_transport.py`
  — the built-in **herdr CLI provider** (`HerdrCliTransport`) plus the
  fail-closed selection resolver `resolve_terminal_transport`. It wraps the herdr
  CLI (`pane send-text` / `pane send-keys` / `agent read`, PoC E8 / E11) as an
  argv subprocess through an injectable runner. Dependency points provider ->
  core.
- `src/mozyo_bridge/e_130_governance_distribution/f_140_rules_docs_catalog/domain/repo_local_config.py`
  — adds the `terminal_transport` block to the closed repo-local config schema,
  behaviour-preserving by default (tmux / off).

### Design decisions (enforced in code)

- **CLI over socket protocol.** The PoC proved both the herdr CLI and the raw
  Unix-socket JSON protocol underneath it. The adapter binds the **CLI**: it is
  the documented, stable surface, whereas the socket wire protocol is an
  internal, unpublished herdr detail (E2) carrying no compatibility promise.
  Same posture as the Redmine note transport (#12347) over the documented HTTP
  API. Recorded in the adapter docstring.
- **Default off, no silent fallback.** `terminal_transport.backend` defaults to
  `tmux`; only an explicit `herdr` selection constructs the adapter. When herdr
  is selected but its binary is unconfigured or unresolvable,
  `resolve_terminal_transport` raises `TerminalTransportError`
  (`binary_unconfigured` / `binary_not_found`) — it never silently falls back to
  tmux (Implementation Guardrail #4).
- **Binary from the trusted environment, not the repo.** Running an arbitrary
  executable is a code-execution vector, so the herdr binary path comes **only**
  from `MOZYO_HERDR_BINARY` in the trusted environment, never a repo-local
  config field. The repo-local config only *selects* the backend; it can never
  point the runtime at a binary. This mirrors the delivery-write trusted-env
  credential boundary (#12347).
- **Not registered in `BUILTIN_PROVIDER_REGISTRY` yet.** Adding herdr as a second
  `terminal_runtime` provider would make that category *ambiguous*, and
  `resolve_selection` fails closed on an ambiguous category with no selection —
  breaking the behaviour-preserving default resolution the CLI entrypoint
  (#12249) relies on. Herdr is therefore an opt-in *backend flag* (default off),
  not a registry entry, until a later US wires an explicit terminal-runtime
  selection through the same fail-closed resolution the provider/presentation
  selections use.

### Scope (staged — kept explicit)

- **In scope:** the port, the result / reason vocabulary, the default-off backend
  config, and the pure herdr CLI adapter + fail-closed resolver, all covered by a
  fake-port contract test and an injected-runner adapter test (no live binary).
- **Out of scope (later US's):** turn-start / wait semantics (#13248 — the
  check-then-wait rail and the Codex Enter-resend rail from PoC E9 / E12–E14 are
  *not* built here; `send_text` is a bare primitive), `agent_status` mapping
  (#13246), durable identity naming (#13247), any live-herdr test, and any
  installer / distribution.

### Non-goals (unchanged, restated for the implementation)

- no third-party / arbitrary-code provider loading; herdr is the only built-in
  terminal-transport provider and it is default off;
- no public ABI or long-term compatibility promise for these record shapes;
- no terminal-transport-defined workflow truth, owner approval, or routing
  authority — a transport observes liveness and delivers sends; it never becomes
  durable identity.

## Follow-up Split

- #12002 should use this document when splitting `commands.py` / `cli.py`: separate core
  command orchestration from provider mechanics.
- #12003 should use this document when defining runtime observability: observer provider
  output is input, not truth.
- #11826 should treat this as the first architecture ledger for v0.8, not as a promise that
  external plugins are supported.
