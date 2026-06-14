# Local / Remote Cockpit Host Boundary

Redmine #11817。local host と remote SSH host の両方で `mozyo cockpit` を
使う時の generic requirement を定義する。

## 結論

local cockpit と remote SSH cockpit は **物理的に統合しない**。それぞれの host の
tmux server / filesystem / registry / runtime cache を対象に動く。mozyo-bridge が
提供すべきなのは、host-aware な target discovery / event projection / handoff
gateway であって、local tmux と remote tmux を一つの cockpit session に混ぜる
ことではない。

```text
local terminal window     -> local host tmux server     -> local cockpit group
ssh terminal window       -> remote host tmux server    -> remote cockpit group
shared operator view      -> projection / event / docs  -> host-aware grouping
```

## Source-of-truth boundary

### Host

Host is an execution boundary.

- tmux server is per host.
- filesystem paths are per host.
- home registry / SQLite files are per host unless explicitly synced by an
  operator outside mozyo-bridge.
- pane ids are only meaningful inside that host's tmux server.

Host label may be exposed in discovery / events, but portable docs must not
record private hostnames or personal absolute paths.

### Cockpit session

`mozyo cockpit --session <name>` creates or adopts a named cockpit session on the
host where the command runs.

- running locally affects local tmux only.
- running over SSH affects remote tmux only.
- same session name on two hosts does not mean same cockpit.

The tuple is effectively:

```text
host + tmux_server + session + window + pane
```

not just:

```text
session + window + pane
```

### Workspace / lane

`workspace_id` and `lane_id` remain governance / checkout identity fields. They
do not collapse host boundaries. The same workspace id can appear on more than
one host, but handoff safety must still bind to the target host's pane / repo
preflight.

## Requirements

### Target discovery

`agents targets` and future discovery surfaces should expose host-aware facts
without leaking private values.

Minimum portable fields:

- `host.kind`: `local` / `ssh` / future provider class
- `host.label`: redacted or operator-defined display label
- `runtime.provider`: `tmux`
- `runtime.session`
- `runtime.window`
- `runtime.pane_id`
- `identity.workspace_id`
- `identity.lane_id`
- `identity.role`
- `repo.label`
- `view.kind`

Discovery may show a short host label for operator clarity. It must not use a
private hostname as a durable public identity in tracked docs or Redmine
journals.

### Handoff

Cross-host handoff is a governance boundary crossing.

Rules:

- do not send directly to a remote Claude pane from a local coordinator.
- route through the target host / target workspace Codex gateway.
- require explicit target selection and repo identity preflight.
- durable anchor remains Redmine / Asana; pane delivery is a pointer.

If the transport cannot verify the target repo / host boundary, fail closed and
ask for an explicit operator action.

### Event timeline

Event timeline / consumer feed may aggregate local and remote observations, but
the aggregation is projection only.

- workflow state remains Redmine / Asana.
- liveness remains live tmux on each host.
- events are cache / observation, not source of truth.
- host must be part of the event source identity.

### Cockpit UI / grouping

A UI can place local and remote groups near each other for oversight. That is
display grouping only.

Allowed:

- local cockpit window next to SSH cockpit window.
- host-aware event feed showing both groups.
- private operator dashboard that groups host labels.

Not allowed in public defaults:

- private host composition.
- private project-to-host policy.
- direct-send shortcuts that bypass target Codex.
- assumptions that local and remote pane ids share a namespace.

## Smoke checklist

For local-only cockpit:

```bash
PYTHONPATH=src python3 -m mozyo_bridge agents targets --session <local-cockpit-session>
```

Expected:

- every row belongs to the local tmux server.
- same workspace / lane / role are distinguishable by pane id and repo facts.
- `AMBIG=0` before handoff.

For remote SSH cockpit, run the same command **on the remote host**:

```bash
ssh <remote-host-label>
cd <project-root>
PYTHONPATH=<mozyo_bridge_repo>/src python3 -m mozyo_bridge agents targets --session <remote-cockpit-session>
```

Expected:

- rows describe the remote tmux server only.
- local pane ids are not assumed to exist.
- target project Codex is the gateway for cross-host work.

For aggregated views:

- every row/event has a host field or an equivalent source-layer tag.
- same `session` name on different hosts stays distinguishable.
- handoff command still targets a concrete pane under the correct host context.

## Follow-up issue triggers

Create a follow-up issue when any of these appear:

- `agents targets` cannot distinguish same session / pane naming across hosts.
- event feed drops host/source-layer identity.
- handoff accepts a remote Claude direct send across host boundary.
- target repo preflight cannot be expressed for the remote pane.
- private host names or paths are needed to explain a public feature.
- operator wants a unified GUI that mixes local and remote groups; that belongs
  to a presentation-plane issue, not core identity.

## Non-goals

- Do not implement a single tmux session spanning local and remote hosts.
- Do not sync home registry databases between hosts in core.
- Do not encode private hostnames or SSH topology in public defaults.
- Do not use host label as workspace identity.

## Acceptance mapping

- OSS generalization: requirement is expressed as host / tmux / projection
  primitives, not private topology.
- private data: no real hostnames, paths, credentials, or operator policy.
- physical boundary: local and remote tmux sessions are explicitly separate.
- future decisions: target discovery, event timeline, and handoff gateway
  requirements are listed with follow-up triggers.
