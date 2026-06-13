# Cockpit Sublane Operating Model

## Purpose

This document records the operating philosophy that emerged from the
multi-lane cockpit PoC around Redmine #11850. It is a repo-local logic
document, not a private operating manual.

The goal is to keep the portable mozyo-bridge primitives clear while still
capturing the real workflow pressure that created them.

## Observed Context

The cockpit can now host multiple checkouts of the same workspace as distinct
lanes. In the active PoC:

- The main `mozyo_bridge` lane is the coordinator lane.
- Additional worktrees are appended as sublanes.
- Each lane has a Codex pane and a Claude pane.
- Redmine journals are the durable source of truth.
- Pane messages are only pointers to durable anchors.

This model became useful because several problems appeared only during actual
dogfooding:

- same workspace / multiple checkout identity needs `lane_id`;
- cockpit and normal `mozyo` sessions need a role resolver rather than
  window-name-only identity;
- sublane completion can look stalled if the result does not return to the
  coordinator lane;
- append geometry can become uneven even when identity is correct;
- installed CLI commands can lag behind repo-local source during active
  development;
- multiple related projects need a way to stay visually close without becoming
  one routing identity.

## Core Separation

The cockpit model separates four concerns that must not be collapsed:

- **Identity**: durable workspace / lane / role / pane facts.
- **Routing**: which agent is allowed to receive and act on a handoff.
- **Display**: how panes, windows, tabs, and iTerm/tmux views are arranged.
- **Governance**: which Redmine gate authorizes action and close.

Window layout can help humans see related work, but it is not a routing
source of truth. A pane being visible next to another pane does not authorize a
direct send across a lane or project boundary.

## Lane Roles

### Main Coordinator Lane

The main Codex pane is the coordinator, auditor, and owner-facing window.
It owns:

- owner questions and close approval collection;
- Redmine gate interpretation;
- review conclusions;
- release / push / CI coordination;
- sublane creation and retirement;
- capture of PoC findings into Redmine or repo-local docs.

The main Codex lane should be conservative with direct edits. It may use the
repo-local guardrail autonomous lane where the project rules allow it, but
ordinary implementation and distributed workflow surfaces still follow the
project's role boundary.

### Sublane Codex

The sublane Codex pane is the gateway for its lane. It should:

- read the durable Redmine anchor first;
- validate that the request belongs to its lane;
- decide whether local Claude should implement;
- route to local Claude with a durable journal anchor;
- report blocked / review-ready / owner-action-needed states back toward the
  coordinator lane.

The sublane Codex is not a second owner-facing coordinator unless the project
explicitly promotes it to that role.

### Sublane Claude

The sublane Claude pane is the implementation worker. It should:

- implement from Redmine journals, not from pane scrollback alone;
- record implementation_done and review_request gates;
- keep verification and residual risk replayable;
- avoid collecting owner close approval.

### Main Claude

The main Claude pane is useful, but it should not become a parallel
coordinator.

Safe uses include:

- scratch analysis;
- long-output or journal summarization;
- candidate extraction;
- draft wording;
- non-authoritative comparison of options;
- implementation only after the work is moved into a proper Redmine-gated lane.

Avoid using main Claude for:

- owner questions;
- close approval collection;
- Review Gate conclusions;
- durable routing decisions;
- silent edits to protected workflow, skill, source, or test surfaces.

Claude output in the main lane is input, not evidence. The coordinator Codex
must still check source files, Redmine journals, and command output before
turning it into a decision.

## Cross-Lane Routing Rule

A lane boundary is a governance boundary even when panes share one physical
tmux session.

If a request crosses from one lane to another, route it to the target lane's
Codex pane first. That Codex reads the durable anchor and then routes to its
local Claude if implementation is appropriate.

Direct Claude delivery is reserved for same-lane addressing. This preserves
the same principle as the cross-session Claude direct-send prohibition.

## Cockpit Groups

Related projects may need to be viewed together. The portable rule is:

- use named cockpit sessions as cockpit groups;
- keep `workspace_id` / `lane_id` / role / pane identity inside the group;
- treat iTerm windows, tabs, or tmux windows as display grouping only;
- use Codex gateway handoff for cross-project consultation.

Do not put unrelated project policy into the OSS default. Private cockpit
composition belongs in private operating policy, not in portable
mozyo-bridge defaults.

## Dogfooding Version Boundary

During active development, the installed `mozyo-bridge` CLI can lag behind the
repo-local source. When the workflow depends on just-landed commands, use the
repo-local invocation:

```bash
PYTHONPATH=src python3 -m mozyo_bridge ...
```

This is a dogfooding rule, not a public install contract. Public docs should
continue to describe installed commands after release.

## Reporting Back To Coordinator

Sublanes must report handoff-worthy state transitions back to the coordinator
lane through Redmine and a short pane pointer. Examples:

- blocked / needs clarification;
- implementation_done;
- review_request;
- review result;
- commit recorded;
- owner close approval requested.

This prevents work from being complete in a sublane while appearing stalled in
the cockpit coordinator view.

## What To Ticket

The PoC intentionally turns operational friction into child issues. Ticket a
finding when it is concrete, likely to recur, or independently fixable.

Examples already observed:

- cockpit append width rebalance;
- stale installed CLI during dogfooding;
- subject / description separation when creating Redmine tasks;
- Claude pane launch permission mode;
- main unit Claude role boundary.

Keep #11850 as the integration record. Do not let it become an unstructured
dump for problems that need their own fix path.

## Revision Principle

This document describes observed workflow risk and current operating judgment.
It is not a permanent claim about any model's quality.

Claude and Codex behavior will change over time. Revisit this document when
the tools change, but preserve the core separation unless there is evidence
that a safer simpler model exists:

- durable state in Redmine;
- identity at workspace / lane / pane level;
- routing through Codex gateways across boundaries;
- implementation in bounded lanes;
- owner-facing decisions in the coordinator lane.
