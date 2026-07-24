# Project-Scoped Workspace Identity

Redmine #12656. This document defines the design boundary for treating a
project directory inside a monorepo as a cockpit-visible project unit without
pretending that the directory is an independent Git repository.

This is a design document, not an operation runbook. Concrete commands,
operator-local paths, and one-off smoke steps belong in Redmine journals or
runbooks, not here.

## Why

Some organizations operate at three visible levels:

- department or umbrella workspace
- individual project
- implementation lane

For a monorepo such as `gk-3500-it-operations`, the Git repository root is the
department workspace, while `projects/cloud-drive-management/` is a
business project. If cockpit identity only follows the Git root, the project
level disappears from the operator experience. That is not just cosmetic: it
blurs who routed the consultation, which project gateway accepted it, and which
implementation lane is doing the work.

At the same time, the project directory must not be treated as a fake Git root.
Branches, commits, worktrees, and dirty-state checks still belong to the real
repository root. Project identity is a routing and presentation scope layered
under the workspace, not a replacement for workspace identity.

## Core Model

```text
Workspace        = Git repository / registry identity
Project scope    = self-describing project directory inside the workspace
Lane             = implementation or coordinator execution lane
Target           = live pane endpoint after role / repo / lane preflight
Projection       = cockpit / UI display of those identities
```

For a monorepo project, the target model should carry both identities:

```text
repo_root        = /path/to/gk-3500-it-operations
workspace_id     = stable identity for gk-3500-it-operations
project_scope    = cloud-drive-management
project_path     = projects/cloud-drive-management
project_label    = クラウドドライブ管理
lane_id          = default or issue lane
role             = codex / claude
```

`repo_root` remains the authority for Git operations. `project_scope` tells the
agent, cockpit, and handoff surfaces which business project the unit represents.

## Shared Root Resolver Is Git-Root-First

The `Workspace = Git repository / registry identity` invariant is not only a
cockpit concern; it is a property of the shared identity resolver every
entrypoint bottoms out at. When a Git worktree root is reachable above the cwd,
that Git root is the workspace root — even when a monorepo project subdirectory
carries its own `.mozyo-bridge/scaffold.json`. A nested workspace marker must not
shadow the Git root's `.mozyo-bridge/config.yaml`; otherwise bare `mozyo` reads
the (usually absent) subtree config and silently falls to the default tmux
backend instead of the Git root's declared `terminal_transport.backend` (Redmine
#13641).

Concretely, the shared `find_repo_root` resolver (and therefore `resolve_repo_root`
and the repo-local config load) prefer `infer_git_worktree_root`; the
marker walk is the fallback ONLY when no Git root is reachable — a genuinely
non-git scaffolded workspace (#11301), a registry-anchored non-git workspace
(#11429), or a config-only adopted root (#13379) still resolve to their marker
root. This mirrors the cockpit resolver `resolve_workspace_root`, so both paths
now enforce the same invariant. Resolving to a Git root is an *identity*
decision, not an *adoption* decision: `workspace_adoption_marker` still gates
whether the resolved root launches a real agent, so an unadopted Git root does
not start one (#13379). Explicit `--repo` / `MOZYO_REPO` overrides are unchanged.

## Discovery Philosophy

The preferred model is self-describing project directories plus a generated
root discovery cache.

Each project owns its local `project.env` as the source of truth for project
metadata that belongs to the project itself. The root workspace may keep an
index, but that index should not become a second hand-maintained source of
truth for the same fields.

`project.env` uses Docker/Compose env-file syntax deliberately. Project
discovery and the DevContainer consume the same tracked, non-secret file; a
generated or hand-maintained YAML mirror is not permitted.

Discovery therefore has two distinct outputs:

- **discovered candidates**: project directories found under the repository
  with valid project metadata
- **adopted project identities**: candidates that explicitly opt into runtime
  identity and can be used for cockpit / handoff routing surfaces

Scanning is useful because it avoids manual root registry drift. Adoption is
still explicit because pane routing and implementation handoff are higher risk
than IDE-style project hints.

## Generated Root Cache

A root-level project index may be updated from scan results, but generated data
must be visibly separated from human-owned policy.

This is intentionally a write-back cache. A scanner may refresh it so agents and
humans can review the current project map without paying the full discovery
cost every time. That cache write does not make the root index the authority for
project-owned fields; it records what the scanner derived from project-owned
metadata.

Recommended shape:

```yaml
projects:
  # Human-owned routing policy, overrides, aliases, and exceptions.

discovery_cache:
  generated_by: mozyo-bridge project discovery
  generated_at: "<timestamp>"
  entries:
    - cache_key: "project:cloud-drive-management@projects/cloud-drive-management"
      source: "projects/cloud-drive-management/project.env"
      path: "projects/cloud-drive-management"
      redmine_project: "cloud-drive-management"
      display_label: "クラウドドライブ管理"
      runtime_identity_enabled: true
      fingerprint: "sha256:<project-env-fingerprint>"
```

The cache is an acceleration and review aid. It is not allowed to silently
override the local `project.env`. If cache and source disagree, the runtime
must surface drift instead of choosing whichever value is convenient.

Generated cache fields are generator-owned. Operators should edit the local
project metadata or human-owned root policy, then regenerate the cache. This
keeps the root from becoming a second source of truth while still giving AI
agents a cheap, reviewable index.

Cache key requirements:

- stable across machines for the same repository layout
- includes the project identifier and repository-relative path
- does not include absolute private paths
- changes only when the project identity or path changes

## Runtime Identity Marker

A project directory should not become a routable project unit merely because a
file named `project.env` exists. Runtime identity needs an explicit marker.

Conceptual fields:

```dotenv
PROJECT_SCHEMA=mozyo.project/v1
PROJECT_REDMINE_PROJECT=cloud-drive-management
PROJECT_RUNTIME_IDENTITY_ENABLED=true
PROJECT_RUNTIME_IDENTITY_KIND=project_scope
PROJECT_DISPLAY_LABEL=クラウドドライブ管理
PROJECT_PARENT_WORKSPACE=gk-3500-it-operations
PROJECT_WORKDIR=.
```

The descriptor accepts only single-line `KEY=VALUE` assignments, blank lines,
and whole-line comments. Keys are unique portable environment-variable names.
Interpolation, `export`, multiline values, and inline comments are rejected so
Docker and discovery cannot derive different identities. Self-description is
local to the project, and cockpit adoption is explicit.

## Pane And Target Projection

Project-scoped panes should stamp project scope as projection metadata in
addition to existing workspace / lane / role markers.

Conceptual pane metadata:

```text
@mozyo_workspace_id
@mozyo_lane_id
@mozyo_agent_role
@mozyo_project_scope
@mozyo_project_path
@mozyo_project_label
```

The project fields are not a substitute for repo preflight. A handoff target
that claims `project_scope=cloud-drive-management` but is not inside the
expected Git repository must fail closed. A target inside the correct Git
repository but outside the expected project path must also fail closed when a
project scope gate is requested.

## Consultation And Implementation Boundary

Project-scoped identity supports the consultation model from #12656:

- ancestor to parent: consultation by default
- parent to child project gateway: consultation by default
- child project gateway: decides whether to create a Redmine work item
- child to implementation lane: implementation, with durable issue / journal
  anchor required

The project gateway can decline to create a work item. That is expected for
consultation. Once implementation is dispatched, the normal Redmine-governed
workflow applies and a durable work anchor is mandatory.

This boundary prevents two failures:

- creating Redmine work items too early, which contaminates autonomous routing
  with issue metadata and related history
- forcing a department-level coordinator to understand a child project's full
  backlog before it can ask the child project gateway for advice

## Non-Goals

- Do not make project subdirectories fake Git repositories.
- Do not move Git worktree lifecycle authority from the repository root to a
  project subdirectory.
- Do not let scan results bypass explicit runtime identity adoption.
- Do not use cockpit labels as routing authority.
- Do not remove the Redmine anchor requirement for implementation lanes.
- Do not encode private operator layout policy as OSS defaults.

## Relation To Existing Models

This design extends `unit-target-model.md` by adding project scope as a unit
identity component below workspace identity. It extends
`pane-centric-cockpit-semantics.md` by defining project scope as pane projection
metadata, not as live geometry. It extends
`delegated-coordinator-cockpit-display.md` by making the department / project /
implementation hierarchy visible without weakening routing or governance
invariants.
