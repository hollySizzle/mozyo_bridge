# Modular Config-Driven Refactor

Redmine #12179 / Version #221. This document is the formal project direction
for the v0.9 line after the v0.8 plugin-ready baseline.

## Purpose

v0.8 established the first internal seams for built-in providers, presentation
adapters, ticket records, and CLI family composition. v0.9 continues that work
by making the core more modular and configuration-aware without publishing an
external plugin API.

The goal is not "plugins now". The goal is to make built-in composition explicit:
which command families, providers, projections, and feature surfaces are present
should be described through narrow registries and configuration records instead
of hard-coded inline wiring.

## Scope

The v0.9 direction covers:

- module selection for built-in CLI command families;
- provider selection for built-in adapters and backends;
- feature flags for optional, non-authority-bearing surfaces;
- clearer boundaries between core authority and application/presentation
  mechanics;
- migration of remaining hard-coded composition points into explicit internal
  registries or configuration records.

This is a continuation of:

- `vibes/docs/logics/plugin-ready-adapter-boundary.md`;
- Redmine #11825 `Plugin / Adapter 境界設計`;
- Redmine #12155 `内部 module registry / configuration-aware CLI baseline を作る`;
- Version #221 `v0.9.0 modular config-driven refactor`.

## Allowed Configuration

Configuration may select or disable optional built-in surfaces when doing so
does not change project authority or safety semantics.

Allowed examples:

- enable or disable a non-mandatory CLI command family;
- select a built-in provider implementation for a supported category;
- choose a presentation projection or read-model surface;
- enable an experimental, read-only feature flag;
- define multiple output targets for an existing renderer kind when the schema
  already permits those targets.

Each allowed configuration path must have a typed schema, a default that
preserves current behavior, and a fail-closed response for unknown names or
unsupported values.

## Non-Configurable Authority

Configuration must not weaken or replace authority owned by the core workflow.
These decisions stay hard boundaries:

- workflow authority;
- owner approval;
- review authority;
- close approval;
- routing authority;
- handoff / send safety;
- credential, secret, auth, permission, billing, and destructive-operation
  boundaries;
- release / publish approval.

A module, provider, or feature family that participates in any of these
authorities is mandatory unless a dedicated compatibility-retirement issue
changes the authority model. A config file may not disable it, replace it with
arbitrary code, or grant the authority to a provider.

## External Plugin Boundary

v0.9 does not expose arbitrary external plugin loading.

Do not add:

- dynamic imports from user configuration;
- Python entry point loading for third-party providers;
- module paths or callable names resolved from repo-local config;
- public ABI promises for internal record shapes;
- a generic "plugin install" or "plugin run" command surface.

The registries remain internal built-in registries. Their purpose is to make
composition explicit and auditable before any future external plugin surface is
designed.

## Design Rules

1. Keep core small and hard.
2. Put mechanics in built-in modules, providers, or adapters.
3. Make composition explicit through registries or typed config records.
4. Preserve current behavior as the default composition.
5. Fail closed on unknown module/provider names or invalid config.
6. Do not let config change workflow, approval, routing, send-safety, or secret
   boundaries.
7. Keep move-only / behavior-preserving splits separate from behavior changes.
8. Record each boundary change in Redmine and cataloged docs before treating it
   as a stable direction.

## Acceptance Shape For v0.9 Work

A v0.9 modular/config-driven issue should state:

- which composition point is being made explicit;
- which registry or config record owns it;
- what stays mandatory and why;
- what the default behavior is and how it preserves current CLI/API output;
- which tests prove the default and fail-closed paths;
- whether the change is internal-only or moves toward a future public surface.

If a task cannot answer those points, it is not ready for implementation and
should first become a design consultation or documentation task.
