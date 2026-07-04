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

## Implemented herdr Durable-Identity Mapping Seam (Redmine #13247)

The #13245 terminal-transport seam addresses a herdr target by a *handle* but
deferred *which* handle is durable. #13247 lands that: a **staged seam** mapping
a mozyo lane/workspace/role slot to a herdr **assigned name** — the handle the
#13175 PoC proved survives a restart (experiment E10: `agent rename` names
persist across `server stop` / restart, while `pane_id` / `terminal_id` are
regenerated and disposable). No runtime path changes; this is a pure naming
convention + mapping type + re-bind procedure for later US's to build on.

### Where it lives

- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/domain/herdr_identity.py`
  — **core**, pure. The deterministic name codec
  (`encode_assigned_name` / `decode_assigned_name` / `encode_field`), the
  pane/terminal-free `HerdrAgentIdentity` mapping type, the structured decode
  result (`HerdrNameDecode`) with a closed `DECODE_FAILURE_REASONS` vocabulary,
  and the fail-closed restart re-bind procedure (`rebind_by_name` ->
  `HerdrRebindResult`).

### Design decisions (enforced in code)

- **Assigned name is the sole durable handle; pane/terminal ids are never held.**
  `HerdrAgentIdentity` has only `(workspace_id, lane_id, role)` fields — there is
  structurally no `pane_id` / `terminal_id` slot — so a caller cannot persist a
  session-local locator as identity. This encodes PoC E10 directly.
- **Consistent with the route-identity ledger (#12553).** The stable slot is the
  same tuple the ledger uses `(workspace_id, lane_id, role)`, normalised the same
  way (empty lane -> `default`). The herdr assigned name is the durable analogue
  of the ledger's `pane_name`; pane/terminal ids are the disposable analogue of
  its `last_seen_pane_id` (cache, never authority). The two identity contracts do
  not drift.
- **Naming convention: deterministic, round-trippable, collision-free.** A name
  is `mzb1_<f(workspace)>_<f(role)>_<f(lane)>` where `_` is the sole delimiter and
  `f` percent-encodes each field with a letter escape (`Z<HH>`, self-escaping) so
  no field ever contains the delimiter or a non-`[A-Za-z0-9]` byte. Splitting on
  `_` always yields four parts and each field decodes independently, so the
  round-trip and injectivity hold for *arbitrary* component strings (including
  `_` and non-ASCII). The round-trip is a **normalized-slot roundtrip**:
  `encode_assigned_name` first trims each component and maps an empty lane to
  `default`, and `decode` recovers that normalized slot byte-for-byte — not the
  raw pre-normalization input. `encode`/`decode` signatures:
  `encode_assigned_name(workspace_id, role, lane_id="") -> str`,
  `decode_assigned_name(name) -> HerdrNameDecode`. Example:
  `encode_assigned_name("giken-3800-mozyo-bridge", "claude", "lane_13247")` ->
  `mzb1_gikenZ2D3800Z2DmozyoZ2Dbridge_claude_laneZ5F13247`.
- **Conservative `[A-Za-z0-9_]` alphabet, length-capped.** The output alphabet is
  the safe intersection of "what herdr accepted in the PoC (`poc_claude`)" and
  "cannot smuggle a shell/argv token", so a minted name is also a valid #13245
  transport target (`valid_target`). Names over `NAME_MAX_LENGTH` fail closed
  rather than truncate (truncation would break injectivity).
- **Fail-closed parse (structured, never raises).** `decode_assigned_name`
  returns a `HerdrNameDecode` with an explicit reason from the closed set (empty /
  illegal char / bad prefix / bad shape / bad escape / empty required / too long);
  it never raises. Construction of an identity from an *empty required* slot does
  raise `HerdrIdentityError`, matching the sibling domain errors.
- **Restart re-bind by name, not by cached locator.** `rebind_by_name(name,
  agents)` re-discovers the live target from an `agent list` snapshot by matching
  the durable name; the recovered pane locator is transient (labelled as such and
  omitted from `public_pointer`). Fails closed on invalid-name / not-found /
  ambiguous (duplicate names) / missing-locator (a single name match whose live
  row carries no usable pane locator — refuse to report success with a blank
  target).

### Scope (staged — kept explicit)

- **In scope:** the pure naming convention, the pane/terminal-free mapping type,
  and the name -> live re-bind procedure, all covered by determinism /
  round-trip / injectivity / fail-closed contract tests (no live binary).
- **Out of scope (later US's / gated):** conversation *session* resume after a
  restart (E10: sessions do not auto-revive without herdr's official integration
  hook — the #13249-gated extension), any live-herdr test, and any wiring into
  the live handoff / cockpit actuator.

### Non-goals (unchanged, restated)

- a herdr assigned name is a transport locator handle, not workflow authority: it
  never becomes owner approval, routing authority, or ticket-state truth (the
  durable work record stays Redmine).

## Implemented Terminal Runtime State Seam (Redmine #13246)

#13245 landed the transport half of the terminal runtime adapter (the send /
read port). #13246 lands the **state** half: a pure, fail-closed mapping from
the state herdr reports about a pane's agent onto a small mozyo-owned **runtime
receiver-state** vocabulary, plus the built-in herdr `agent get` / `agent list`
reader that fills it. Same feature package, same conventions (staged seam, pure
core + one built-in provider, fail-closed, default off, trusted-env binary,
injected-runner tests, no live binary). The existing tmux path is untouched.

### Where it lives

- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/domain/agent_state.py`
  — **core**, pure. The core-owned herdr observed-status vocabulary
  (`working` / `blocked` / `idle` / `done` / `unknown`, PoC E6 / E7 / E13 / E14),
  the mozyo runtime receiver-state vocabulary (`busy` / `blocked` /
  `awaiting_input` / `turn_ended` / `unknown`), the pure total mapping
  `map_agent_status`, and the fail-closed read-result records (`AgentStateResult`
  / `AgentStateListResult`, with an enforced ok/reason invariant and a
  failure-degrades-to-`unknown` invariant). It imports no provider.
- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/infrastructure/herdr_state.py`
  — the built-in **herdr CLI state reader** (`HerdrCliAgentStateReader`) plus the
  fail-closed selection resolver `resolve_agent_state_reader`. It wraps
  `agent get <target> --json` / `agent list --json` as an argv subprocess through
  an injectable runner, parses the JSON defensively for the `agent_status` token,
  and reuses the #13245 transport plumbing (`_resolve_binary`, `HERDR_BINARY_ENV`,
  the `Runner` shape, `COMMAND_TIMEOUT_SECONDS`, `_bounded_detail`) rather than
  duplicating it. Dependency points provider -> core.

No new config: the reader rides on the same default-off `terminal_transport.backend`
selection and trusted-env binary resolution as the transport (#13245).

### The mapping (enforced in code, `_STATUS_TO_RUNTIME`)

| herdr `agent_status` | mozyo runtime receiver-state | PoC evidence | note |
|---|---|---|---|
| `working` | `busy` | E7 (30s / 5355 samples all working) | actively producing a turn |
| `blocked` | `blocked` | E13 `generic_permission_prompt` / E14 `osc_title_blocked` | **runtime-observed** block (permission prompt on screen), *not* the durable-recorded `blocked` the attention model means |
| `idle` | `awaiting_input` | E7 (`✳` title → idle) | quiet, waiting for input; caller consults tmux liveness |
| `done` | `turn_ended` | E14 (`wait done` turn-end) | **assistant turn finished, NOT task completion / close gate** |
| `unknown` / unrecognised / non-string / parse failure | `unknown` | E6 (`agent_status: unknown`) | fail-closed; never raises |

### Design decisions (enforced in code)

- **A runtime observation vocabulary, not a workflow / attention state.** herdr
  `agent_status` is a layer-1 runtime receiver signal in the ACK / completion
  doctrine (`vibes/docs/logics/ack-completion-receiver-state.md`). The mapping
  target is deliberately a *different* vocabulary from the derived cockpit
  `attention_state` (`vibes/docs/logics/cockpit-attention-state.md`), so a
  runtime signal is never mistaken for workflow truth. This runtime state is only
  one *input* a caller may later feed into attention derivation; wiring that in is
  out of scope here.
- **`done` -> `turn_ended`, never `done`.** The single load-bearing fail-closed
  choice. herdr `done` means the assistant *turn* finished (a layer-2
  `assistant_turn_finished` signal per the doctrine), which must never be promoted
  to the attention model's `done` (`close_gate_satisfied`). A test pins that
  `turn_ended` is a distinct token from the attention `done` and that the latter
  is not in the runtime vocabulary.
- **Everything unknown fails closed to `unknown`.** A non-string, an unrecognised
  token, a non-JSON payload, or a missing status key all degrade to `unknown`
  (which callers treat as "consult tmux liveness", never death or completion),
  exactly like the OTel activity layer's `unknown`
  (`e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.agent_activity`).
  `map_agent_status` never raises.
- **Two failure modes, both fail-closed.** A *mechanical* read failure (bad
  target, missing binary, non-zero exit, timeout, OS error) yields
  `ok=False` + a `TRANSPORT_FAILURE_REASONS` reason + `state=unknown` (a failed
  read may never assert a confident state). A *soft* failure (an `agent get`
  command ran but its JSON carried no recognised status) is a *successful* read
  of `unknown` (`ok=True`, an observed-unknown) — distinct from could-not-observe,
  but both fail closed for a state-only caller. `AgentStateError` subclasses
  `TerminalTransportError`, so the whole seam shares one fail-closed error base.
- **`agent list` distinguishes "recognised empty" from "unrecognisable".** A
  recognised list payload — a bare JSON array, an object carrying the rows under
  `agents` / `panes` / `items`, or one of those wrapped one level under a
  `result` / `data` envelope — may legitimately be **empty** and reports
  `ok=True` with no rows. A payload that is *not* a recognisable list schema
  (non-JSON, a scalar JSON value, or an object with no recognised container)
  fails closed with `ok=False` + `reason=invalid_payload` (a new
  `TRANSPORT_FAILURE_REASONS` member) rather than pretending it observed an empty
  set — an unreadable list is not "no agents".
- **A malformed `agent list` row is skipped, not fatal.** Each row's handle is
  trimmed and validated with the same core `valid_target` guard used for an
  `agent get` target. A row that is not an object, or whose handle is missing /
  blank / malformed (a space-bearing string, a bare `--flag`, whitespace), is
  **skipped** so one broken row does not lose every good one; the count of
  skipped rows is recorded in the result `detail` so the loss stays observable.
  Contrast the payload-level `invalid_payload` failure above: a whole unreadable
  payload fails closed, but a single bad row inside a recognised payload only
  drops that row.

### Event-subscription semantics (issue requirement — documented, not built)

The issue requires the `wait agent-status` subscription semantics be pinned as a
**design premise** for the state reader, because they constrain how a later
caller composes a snapshot read with a wait. Established live in the PoC
(`vibes/docs/logics/herdr-poc-13175-experiment-log.md`):

- **`wait agent-status` waits for a *change* into a state, not the current
  state** (E9 c2): a `wait --status idle` issued while *already* idle times out
  rather than returning immediately. So a caller cannot use `wait` alone to learn
  the current state.
- **Therefore the reader is a snapshot / check-then-wait primitive.**
  `read_agent_state` reports the current runtime state at call time; the
  contract for a later turn-start caller is *read a current-state snapshot
  before arming a wait*, so a transition that lands between the read and the wait
  is not missed (E9's race). The snapshot API is **this US**; the wait rail —
  arming the wait, the Codex Enter-resend (E14), and the started / blocked /
  absent / turn-end 4-case harness — is **#13248**.
- **Subscribe-time event delivery is a fail-safe caveat for the wait rail
  (E14).** When a `wait done` was armed just after a `done` transition had
  already occurred, an event returned almost immediately (~11ms observed). That
  subscribe-time behaviour is to be confirmed against a live binary and handled
  fail-safe in #13248; it does not affect this snapshot read model, which never
  subscribes. Recorded here so the wait US does not re-derive it.

Live verification is not required for this US (staged seam): the mapping and the
reader are pinned through a pure fake / injected-runner, with no live herdr.

### Scope (staged — kept explicit)

- **In scope:** the observed-status + runtime receiver-state vocabularies, the
  pure fail-closed mapping, the fail-closed read-result records, and the herdr
  `agent get` / `agent list` reader + resolver, all covered by pure mapping tests
  and injected-runner reader tests (no live binary).
- **Out of scope (later US's):** `wait agent-status` turn-start / change
  semantics and the 4-case harness (#13248), durable identity naming (#13247),
  wiring this runtime state into the cockpit attention derivation, any live-herdr
  test, and any installer / distribution.

## Implemented Terminal Runtime Turn-Start Rail (Redmine #13248)

#13245 landed the transport port (send / read primitives) and #13246 the state
snapshot (`read_agent_state`). #13248 lands the **orchestration** layer that turns
"inject a message" into "confirm the receiver actually *started a turn*": the
`check-then-wait` rail the #13175 PoC established (E9 / E12–E14). Same feature
package, same conventions (staged seam, pure core + built-in provider, fail-closed,
default off, trusted-env binary, injected-dependency tests, no live binary). The
existing tmux path is untouched.

### Where it lives

- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/domain/turn_start_rail.py`
  — **core**, pure. The closed `TURN_START_OUTCOMES` vocabulary, the structured
  `TurnStartResult`, the injected wait-primitive *port* (`TurnStartWaitPort` /
  `ArmedWait`) and its `WaitResult` vocabulary (`changed` / `timeout` / `absent` /
  `error`), the pure `composer_retains_body` helper, the pure `HerdrTurnStartRail`
  orchestrator, and the redaction-safe `turn_start_rail_record_lines` renderer.
  `TurnStartRailError` subclasses `TerminalTransportError`, so the whole seam
  shares one fail-closed error base. It imports no provider.
- `src/mozyo_bridge/e_140_adapter_provider/f_130_terminal_runtime_provider/infrastructure/herdr_turn_start.py`
  — the built-in **herdr CLI wait primitive** (`HerdrCliWaitPrimitive`, a
  two-phase `arm` / `collect` over `wait agent-status <target> --status working
  --timeout <ms>` via an injectable `Popen` factory) plus the fail-closed
  `resolve_turn_start_rail` resolver that wires all three providers (transport
  #13245, reader #13246, this wait primitive) from the one trusted-env binary.
  Dependency points provider -> core.

No new config: the rail rides on the same default-off `terminal_transport.backend`
selection and trusted-env binary resolution as the transport (#13245).

### The check-then-wait procedure (enforced in `drive_turn_start`, j#72258)

1. **Pre-injection snapshot (check).** Read the current runtime state (#13246). If
   it is anything other than `awaiting_input` — `busy` / `blocked` / `turn_ended` /
   `unknown`, *including* an unreadable snapshot that degrades to `unknown` — the
   rail refuses to inject and fails closed to `precondition_not_idle`: a turn on an
   already-busy pane could not be *attributed* to this send, so injecting would
   make a later `started` unfalsifiable.
2. **Arm the wait first** (before injecting), so the `working` transition the
   injection triggers cannot land in the race window between the snapshot and the
   wait (E9 change-semantics; E12 proved arm-then-inject returns event-driven in
   ~0.36s).
3. **Inject** — `send_text` then `send_keys enter`. Any transport failure fails
   closed to `inject_failed` and cancels the armed wait.
4. **Collect the wait**, classify (see the outcome table), and on a timeout run the
   bounded Enter-resend rail.

### The six outcomes (`TURN_START_OUTCOMES`, closed set)

| outcome | when | PoC evidence |
|---|---|---|
| `started` | wait returned the `working` transition (exit 0) | E12 / E14 (event ~0.36s) |
| `delivered_not_started` | injected, wait timed out, re-snapshot not `blocked` (or an unclassifiable wait `error`) | E9 c1 / E13 (`working` times out) |
| `blocked` | injected, wait timed out, re-snapshot found a **runtime** block (permission prompt on screen) | E13 / E14 (`osc_title_blocked`) |
| `absent` | the target pane does not exist (pane-get error on the wait) | E9 c3 |
| `precondition_not_idle` | pre-injection snapshot was not `awaiting_input` (fail-closed, never injects) | E9 (check-then-wait constraint) |
| `inject_failed` | a `send_text` / `send_keys` transport step failed (fail-closed) | — |

### Codex Enter-resend rail (E14 — enforced in code)

E14 reproduced the long-known Codex TUI quirk over herdr: the injected text landed
in the composer but the first Enter was **not** submitted, so the turn never
started until Enter was re-sent. When the first wait times out, the rail reads the
pane (`read_pane`) and re-sends Enter **only if the injected body is still in the
composer** (`composer_retains_body`, whitespace-collapsed so a soft line-wrap does
not hide it) — never re-typing the body, only the Enter — up to
`max_enter_resends` times (config; default `1`, `0` disables it). Each resend
re-arms a fresh wait first (the same check-then-wait order). This is agent-kind-
agnostic bounded-retry, not Codex-special-cased. A pane read that fails or a
composer that no longer holds the body **stops** the rail (fail-closed: never
blindly re-Enter without confirming the stuck-composer precondition).

The E14 subscribe-time caveat (a wait armed just after the transition can return an
event in ~11ms) is handled fail-safe: **any** `changed` result (exit 0) is accepted
as `started`, so an immediate event never becomes a false timeout. The exact
subscribe-time delivery and the wait's non-zero stderr tokens (pane-get vs
timeout) are confirmed against a live binary at the cutover smoke (#13254); the
classifier's indicator set is defensive and the default is `error` (fail-closed).

### Equivalence to the #13166 codex-standard turn-start guard (documented proof)

The close requirement is a documented proof that this rail is equivalent-or-stronger
to the current #13166 guard
(`src/mozyo_bridge/application/turn_start_observation.py`, wired at
`src/mozyo_bridge/application/commands.py:2900`–`2985`). #13166 hardened the codex
`--mode standard` tmux rail against a false-positive `sent`: after observing the
landing marker and pressing Enter, it snapshots the receiver pane and polls it for
**new output activity** (`submit_activity_observed`,
`turn_start_observation.py:75`); confirmed activity resolves `sent` / `ok`, and no
activity within the window fails closed to `blocked` / `turn_start_unconfirmed`
(`commands.py:2964`–`2967`). It types the marker+body **once** and never
re-issues Enter or auto-resends (`turn_start_observation.py:20`–`22`).

The cases the #13166 guard distinguishes, mapped to this rail's outcomes:

| #13166 guard case (file:line) | #13166 signal | this rail's outcome | how the rail is equal-or-stronger |
|---|---|---|---|
| turn-start **confirmed** → `sent` / `ok` (`commands.py:2954`–`2963`) | pane-capture *advanced* past the pre-Enter snapshot (a heuristic proxy for "a turn started") | `started` | keys on herdr's **event** (`agent_status` → `working`, E12/E14), not a rendered-text diff — a positive runtime signal, so no "redraw churn looks like a turn" false positive and no "quiet turn looks like nothing" false negative (the `ack-completion-receiver-state.md` caveat that pane text is not the ACK source of truth) |
| turn-start **unconfirmed** → `blocked` / `turn_start_unconfirmed` (`commands.py:2964`–`2967`) | no new pane activity within the window | `delivered_not_started` **and** `blocked` (split) | the rail *re-snapshots* on timeout and separates a plain unstarted turn (`delivered_not_started`) from a runtime-observed permission block (`blocked`, E13/E14) — strictly **more** discrimination than #13166's single `turn_start_unconfirmed` |
| observation window off (`--landing-timeout 0`) → confirmed, 0 polls (`turn_start_observation.py:109`–`115`) | window disabled ⇒ do not hard-block | (no rail equivalent; the rail's wait window is always positive and its `precondition_not_idle` never injects) | the rail cannot be configured into a fail-**open** "confirm without observing" state; the closest control (`max_enter_resends 0`) only disables the *resend*, never the wait itself |
| (not distinguished by #13166) receiver already busy before send | — | `precondition_not_idle` | the rail **refuses to inject** onto a non-idle pane so a pre-existing turn is never mis-attributed as this send's start — a fail-closed guard #13166 has no analogue for (it always types+Enters) |
| (not distinguished by #13166) target pane absent | — (a tmux capture of a dead pane is empty, indistinguishable from "no activity") | `absent` | the wait's pane-get error (E9 c3) tells "gone" from "delivered but idle"; #13166 would report the same `turn_start_unconfirmed` for both |
| marker+body typed once, no auto-resend (`turn_start_observation.py:20`–`22`) | — | Enter-resend rail (bounded, body-in-composer-gated) | a **superset**: the rail also types the body once (`send_text` once) and re-sends **only Enter** under the E14 stuck-composer precondition — the recovery #13166 explicitly deferred (its candidate 2), added here without ever re-injecting the body |

Net: every case the #13166 guard resolves, this rail resolves at least as
precisely, using a positive event signal instead of pane-capture heuristics, and it
adds three fail-closed discriminations #13166 lacks (`precondition_not_idle`,
`absent`, and the `blocked` vs `delivered_not_started` split) plus the bounded
Enter-resend recovery. It is therefore equivalent-or-stronger and strictly more
fail-closed. Wiring it into the live send path in place of / alongside #13166 is
**#13253**, gated on the #13254 live smoke.

### The 4-case harness (the "formal harness", CI-ised)

The close requirement's "formal 4-case harness" is realised as the fake-driven
contract test `tests/unit/.../test_turn_start_rail.py`, which runs in CI with the
rest of the unit suite (no live herdr). It covers the four post-injection outcomes
(`started` / `delivered_not_started` / `blocked` / `absent`), the two pre-injection
fail-closed outcomes (`precondition_not_idle` / `inject_failed`), the check-then-
wait *ordering* (arm before inject, asserted on an event log), and the Enter-resend
rail (initial-timeout → resend → `started`; resend-cap → `delivered_not_started`;
resend skipped when the composer is cleared / the read fails / the cap is 0; the
subscribe-time immediate-`changed` fail-safe). The wait primitive itself is pinned
in `test_herdr_turn_start.py` through an injected `Popen` factory (argv, the two-
phase arm/collect, the double timeout, and the exit classification). **Live**
verification of the wait surface is deferred to the #13254 cutover smoke, per the
staged-seam posture.

### Scope (staged — kept explicit)

- **In scope:** the outcome / wait vocabularies, the structured `TurnStartResult`,
  the pure `HerdrTurnStartRail` + `composer_retains_body` + record renderer, the
  built-in herdr wait primitive + rail resolver, and the fake-driven 4-case +
  2-precondition + Enter-resend harness (no live binary).
- **Out of scope (later US's):** wiring this rail into the live handoff send path
  (#13253), the installer / pin config (#13249), live smoke verification of the
  wait surface (#13254), and re-deriving the subscribe-time / stderr tokens against
  a live binary.

## Implemented Terminal Runtime Live-Wiring Seam (Redmine #13253)

The #13245–#13248 US's landed the built-in **terminal runtime** parts behind a
default-off backend selection but left every one a *staged* seam — constructed and
fake-tested, never reached by a real send. #13253 is that wiring: it makes the live
handoff rail use the herdr transport when `terminal_transport.backend: herdr` is
selected, and it does so at a **single injection point** without rewriting the send
choreography.

### The seam (candidate C — a tmux-shaped binding behind the existing names)

The tmux physical exit is
`e_110_execution_platform/f_130_handoff_routing/infrastructure/tmux_client.py`
(`run_tmux` / `capture_pane`) and is **frozen** — #13253 does not touch it.
`application/commands.py` imports those two names, and `orchestrate_handoff` calls
them at four `send-keys` sites and five `capture` sites. Rather than edit those
call sites, #13253 keeps them and swaps *what the two names resolve to*:

- `e_140_adapter_provider/f_130_terminal_runtime_provider/application/transport_binding.py`
  defines `TransportBinding` (a `run_tmux`-shaped callable + a `capture_pane`-shaped
  callable + a `backend` name) and the pure resolver
  `resolve_runtime_transport_binding(config, *, tmux_run_tmux, tmux_capture_pane,
  env=…, runner=…, port=…)`. The tmux primitives are **injected** so this
  adapter-layer module never imports the tmux infrastructure package.
- `application/handoff_transport_wiring.py` holds the injection point:
  `resolve_handoff_transport_binding(args)` reads the repo-local
  `terminal_transport` selection **once**, and the `bind_runtime_transport`
  decorator on `orchestrate_handoff` — the *only* change to the send path, and
  **not** a change to `orchestrate_handoff`'s body — installs the binding.

### tmux transparency (byte-for-byte) and the monkeypatch seam

For the tmux backend (the default, an absent `terminal_transport` block, or a
broken config) the resolver returns the injected tmux callables **unchanged**
(identical objects), and the decorator installs **nothing** — it calls
`orchestrate_handoff` straight through. So the tmux path is byte-for-byte the prior
behaviour, and the `commands.run_tmux` / `commands.capture_pane` monkeypatch seam
(#12932) is untouched: every existing handoff/commands test stays green with no
edit. The transparency is pinned two ways — a resolver contract test asserts the
returned callables are the *same objects* passed in, and the entire existing
handoff suite runs (unchanged) under the default binding.

### herdr mapping (fail-closed, no silent fallback)

For the herdr backend the resolver builds a tmux-shaped shim over the #13245
`TerminalTransportPort` and the decorator swaps it in for the send, restoring the
tmux globals in a `finally`. The shim maps the exact tmux argv shapes the rail
reaches under the binding — that set was enumerated exhaustively (Redmine #13253
j#72361) from the send body (`commands.py` strict/queue-enter rail), the
target-activation tail (`handoff_target_activation_command.py`), and the
`wait_for_text` loop (`session_bootstrap_command.py` → `commands.capture_pane`):

| tmux op reached under the binding                  | classification | herdr handling                                  |
| -------------------------------------------------- | -------------- | ----------------------------------------------- |
| `send-keys -t T -l -- <text>`                      | map            | `port.send_text(T, text)` (composer inject)     |
| `send-keys -t T Enter`                             | map            | `port.send_keys(T, "enter")` (submit the turn)  |
| `send-keys -t T C-u`                               | map            | `port.send_keys(T, "C-u")` (composer rollback)  |
| `capture_pane(T, lines)`                           | map            | `port.read_pane(T, source="visible", lines=…)`  |
| `select-pane -t T` (activate + restore, #12597)    | no-op (target checked) | success, no port call — see below       |
| anything else                                      | fail-closed    | raise `TransportBindingError`                   |

The match is exact argv (never a prefix / substring guess): a tmux subcommand the
shim does not recognise **fails closed** with a raised `TransportBindingError`, and
a mapped primitive that reports `ok=False` raises the same — the herdr path never
returns a silent success and never drops a send. Selection is fail-closed too: a
herdr selection whose trusted-environment binary (`MOZYO_HERDR_BINARY`) is
unconfigured / unresolvable surfaces the #13245 `TerminalTransportError` as a clean
`die`, never a silent downgrade to tmux (j#72318).

**`select-pane` → target-checked no-op (finding-1 fix, j#72361).** The #12597
target-activation tail activates an admitted inactive split — and optionally
restores focus after delivery — via `run_tmux("select-pane","-t",T)`, resolved
through `commands.run_tmux` at call time. Because the decorator swaps the *whole*
`commands.run_tmux` for the shim, that `select-pane` reaches the shim; under the
default queue-enter rail an inactive admitted target always activates, so the
initial implementation crashed it with `TransportBindingError`. The fix maps
`select-pane` to a **no-op success**: pane *focus* is a tmux composer-landing
concern, and herdr lands text in a receiver's composer without focusing its pane
(every PoC #13175 injection, E8 / E12–E14, succeeded against a non-focused pane), so
there is nothing for herdr to do. It is a no-op rather than a tmux pass-through
because passing the handle to a tmux client would hand a herdr target to tmux. The
target is still checked for well-formedness (non-empty, no whitespace) so a garbage
handle fails closed — deliberately **not** the strict herdr-handle `valid_target`
guard, which rejects the tmux pane ids (`%N`) the activation tail passes and which,
for a no-op that spawns no subprocess, is unwarranted.

The check-then-wait event rail (`HerdrTurnStartRail`, #13248) is **not** wired here:
#13253 reuses the unchanged tmux-shaped send/capture choreography, so it binds only
the transport primitives. `orchestrate_handoff`'s existing codex-standard turn-start
observation (`turn_start_observation.py`) is capture-injected and therefore works
over herdr through the same `capture_pane` → `read_pane` mapping unchanged.

### Target translation: tmux `%N` → live herdr locator (j#72367; target-pane identity j#72373)

`orchestrate_handoff` resolves its send target through the **tmux** pane resolver,
so the target the rail hands the shim is a tmux pane id (`%N`). The live
`HerdrCliTransport` guards every primitive with the domain `valid_target` regex,
which rejects a leading `%` (`invalid_target`) — so an un-translated `%N` makes
*every* live herdr send fail before typing (a fake port without the guard hid this;
the regression fakes now carry the same guard). The shim therefore runs each
send/capture target through a target translator (using the #13247/#13246 durable
identity parts) before the port call:

- a target that is already herdr-valid (a `mzb1_…` assigned name or a `w1:p1` live
  locator — anything `valid_target` accepts) is **passed through** unchanged;
- a tmux `%N` is mapped to *that target pane's* live locator by resolving **the
  target pane's** durable assigned name and re-binding it against a fresh
  `agent list` snapshot.

The identity is derived from the **target pane**, not the sender / current-repo
context (Redmine #13253 j#72373 — a fixed pre-mint from the CLI-executing repo's
canonical session/lane mis-bound explicit-`%pane` + `--target-repo auto` /
cross-lane sends). It is resolved **lazily**, the first time the shim sees the
`%N` — i.e. *after* `orchestrate_handoff` has resolved the concrete target pane:

1. **Target-pane identity slot.** `pane_info(target)` → `project_preflight_target`
   (the rail's own projection: the #11822 role resolver + the pane's
   `(workspace_id, lane)`), giving the target pane's `(workspace_id, role, lane)`
   slot — normalised exactly as #13247 prescribes (`_normalize_lane_display` ==
   `_norm_lane`, empty → `default`). The identity is minted (#13247
   `encode_assigned_name`) **only when the pane strongly, non-ambiguously binds the
   receiver** — reusing the rail's own `PreflightTarget.binds_receiver` predicate
   (`role == receiver`, `confidence == strong`, `not ambiguous`, Redmine #13253
   j#72381). A merely *weakly*-inferred role (a bare `node` / process-basename
   signal, no `@mozyo_agent_role` option or agent window name), an ambiguous or
   cross-bound role, or a missing `workspace_id` (an unregistered pane) fails
   closed **before** any send.
2. **Live snapshot.** `agent list --json` via the same trusted-environment binary as
   the transport (the rows carry the durable `name` + the transient `pane` locator);
   the row extraction reuses the #13246 defensive parser.
3. **Re-bind.** #13247 `rebind_by_name(assigned_name, rows)` → the live locator. The
   result is memoised **per target**.

Fail-closed before typing (no silent send to a bad / wrong target): an
un-projectable target-pane identity, or a re-bind failure (`rebind_invalid_name` /
`…_not_found` / `…_ambiguous` / `…_missing_locator`), raises a `TransportBindingError`
**before** any port call — the send never lands on a guessed, blank, or
sender-context locator. `select-pane` is the sole target that is *not* translated:
it is a no-op that never reaches the port (only checked well-formed). The regression
(`test_herdr_transport_wiring.py`) drives the **real** resolver (repo config + a
trusted-env fake binary + a faked `subprocess.run`, no patch of the resolver) with a
cross-lane fixture where the agent list carries both the sender and the target rows,
and asserts delivery lands only on the target pane's locator (and fails closed when
only the sender's row exists).

### One-line cut-over / roll-back (j#72318)

Selecting or reverting the backend is a **single** `terminal_transport.backend`
line in `.mozyo-bridge/config.yaml` plus a process restart — there is no data
migration and no persisted binding to clear, because `resolve_handoff_transport_binding`
reads the selection fresh per process and holds no state.

### Scope (staged — kept explicit)

- **In scope:** the pure `config -> TransportBinding` resolver, the tmux
  passthrough binding, the tmux-shaped herdr shim (send-text / Enter / C-u /
  capture maps, the `select-pane` target-checked no-op, and fail-closed on any
  other subcommand or a failed primitive), the tmux-`%N` → live-herdr-locator
  target translation (identity mint + `agent list` re-bind, fail-closed before
  typing), the single-injection-point decorator, and the fake-port contract +
  orchestrate smoke + inactive-target-activation + translation tests (no live binary).
- **Out of scope (later US's):** switching a real workspace's config to herdr and
  the live cut-over smoke (#13254), the installer / pin config (#13249), any live
  herdr binary run, and wiring the richer event-based `HerdrTurnStartRail` (#13248)
  into the send — that rail integration was **split out of #13253 into the
  follow-up #13255** (j#72361).

## Follow-up Split

- #12002 should use this document when splitting `commands.py` / `cli.py`: separate core
  command orchestration from provider mechanics.
- #12003 should use this document when defining runtime observability: observer provider
  output is input, not truth.
- #11826 should treat this as the first architecture ledger for v0.8, not as a promise that
  external plugins are supported.
