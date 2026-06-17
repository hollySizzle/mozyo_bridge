# Sublane Bandwidth / Admission Policy

## Purpose

This document defines the repo-local policy for deciding when the coordinator
should open another sublane, when it must stop dispatching and drain existing
lanes, and how lane retirement interacts with cockpit throughput.

It complements [[logic-cockpit-sublane-operating-model]],
[[logic-sublane-worktree-operating-runbook]], and
[[logic-worktree-lifecycle-boundary]]. Those documents define lane roles,
sequencing, and retirement authority. This document defines coordinator
bandwidth.

## Principle

Sublane bandwidth is coordinator attention, not CPU capacity.

A lane consumes bandwidth whenever the coordinator may need to read durable
state, route a decision, audit a result, collect owner approval, or retire local
state. A lane that is "waiting" can be more expensive than a lane that is still
implementing, because it can block review, close, release, or retirement.

Efficient parallel development is an explicit goal. The coordinator should use
sublanes aggressively when the durable state shows ready implementation work and
the admission checks below pass. Serializing all work into the main lane wastes
the cockpit model and should be treated as a throughput smell, not the default.

The coordinator still must not open work only because a pane or worktree can be
created. It may dispatch only when it can also receive callbacks, perform the
required audit, and retire completed lanes without losing durable state.

Conversely, the coordinator should intentionally serialize when parallel work
would increase total latency or risk. Examples include a blocking design
decision, overlapping files or invariants, pending review / owner decisions that
only the coordinator can drain, release / credential / destructive-operation
gates, or a callback backlog that would make another lane invisible.

## Lane State Classes

For bandwidth decisions, classify every lane into one of these classes from the
durable record. Do not infer state from pane layout alone.

- `implementing`: local Claude is working under a durable issue / journal.
- `callback_due`: dispatch occurred, but an expected callback or durable gate is
  missing.
- `review_waiting`: implementation_done / review_request exists and Codex audit
  is needed.
- `owner_waiting`: review or close flow needs owner approval through the main
  coordinator Codex.
- `close_waiting`: review / owner close approval / integration or no-commit
  close conditions are satisfied, but the Redmine issue status is still open.
- `blocked`: the lane recorded a blocker, design consultation, failed handoff, or
  unresolved dependency.
- `retire_ready`: work is integrated or patch-equivalent, issue scope is done,
  and no active gate is pending.
- `idle`: lane has no active durable work and can be reused or retired.

`callback_due`, `review_waiting`, `owner_waiting`, `close_waiting`, and
`blocked` all count as coordinator-blocking states. They must be drained before
opening optional new work. A close-ready issue left in `着手中` is not harmless
bookkeeping: it keeps the durable state inconsistent and can hide whether a
sublane is still active or ready to retire.

## Admission Rule

Before dispatching a new sublane, the coordinator records or verifies:

- target issue, target lane, branch/worktree identity, and durable dispatch
  anchor are known;
- the work is implementation-shaped and should not be performed by the main
  coordinator lane or main-unit Claude;
- no unread `review_request`, `owner_waiting`, `close_waiting`, `blocked`, or
  `callback_delivery_failed` item is waiting for coordinator action;
- the coordinator can perform the expected next review / owner aggregation /
  retirement step for the lane it is about to open;
- the work does not overlap materially with another active sublane's files,
  invariants, or release-critical surface unless the ordering / merge plan is
  recorded;
- the new work is not a lower-priority optional item while a production,
  release, credential, destructive-operation, or owner decision gate is active;
- any existing `retire_ready` lane above the local soft profile has been retired
  or a reason for keeping it is recorded.

If any check fails, do not dispatch another sublane. Record the blocking state
and drain it first.

If all checks pass and there is ready implementation work, dispatch is the
preferred action. A coordinator stop that leaves ready work undispatched should
record why serial execution is more efficient or safer for that specific state.

## Drain Order

When multiple lanes require attention, use this order unless the durable issue
records a stronger dependency:

1. production / release / credential / destructive-operation blockers;
2. `owner_waiting` items that only the coordinator can aggregate;
3. `review_waiting` items;
4. `close_waiting` items whose durable close gates are already satisfied;
5. `blocked` or `callback_due` items, including callback delivery failures;
6. `retire_ready` lanes that consume cockpit or worktree attention;
7. new dispatch.

This order is about coordinator bandwidth. It does not change Redmine gate
requirements, review quality, or owner close approval separation.

## Local Soft Profile

mozyo_bridge dogfooding uses the following repo-local soft profile:

- target: at most two active implementation sublanes plus the main coordinator;
- burst: a third active implementation sublane is allowed only when the
  coordinator records why existing review / owner / blocker / retirement queues
  will not be starved;
- stop: do not open a fourth active implementation sublane without explicit
  owner/operator decision recorded in the durable issue;
- cleanup: when the lane count is above target, retire `retire_ready` lanes
  before the next optional dispatch batch.

These numbers are not a portable mozyo-bridge core default. Downstream projects
may define a different private operating profile. The portable rule is the
admission / drain model above, plus the requirement to record any burst decision
in the durable ticket system.

## Retirement Cadence

Routine retirement remains coordinator-owned under
[[logic-worktree-lifecycle-boundary]].

For bandwidth control:

- retire a `retire_ready` lane immediately after close when lane count is above
  the local target;
- otherwise retire `retire_ready` lanes before opening the next dispatch batch;
- do not leave closed lanes in cockpit merely because the owner did not
  explicitly ask for cleanup, provided the lifecycle checks pass;
- do not retire lanes with unknown dirty state, unresolved callback, active
  review, owner wait, blocker, or identity ambiguity.

## Durable Record Template

When dispatching above target or stopping dispatch because bandwidth is full,
record a short journal:

```markdown
## Sublane bandwidth decision

- current_lanes:
  - <issue>: <state>
- coordinator_blocking_states: <none | list>
- admission_decision: <dispatch | stop_and_drain | burst_dispatch>
- reason: <why this decision is safe>
- next_drain_action: <review | owner aggregation | blocker | retirement | none>
```

The journal should contain issue IDs and state classes, not private paths or
operator-specific cockpit details.

## Non-Goals

- Do not add `git worktree add/remove` orchestration to mozyo-bridge core.
- Do not encode private cockpit layout, personal paths, or operator-specific
  staffing assumptions in OSS defaults.
- Do not let a bandwidth decision waive Redmine gates, Codex review, owner close
  approval, or the Codex gateway rule.
- Do not use the main coordinator lane or main-unit Claude as a substitute
  implementation lane merely because it is already open.

## Verification

- `mozyo-bridge docs validate --repo .`.
- `mozyo-bridge docs validate --check-file-coverage --repo .`.
- `mozyo-bridge docs generate-file-conventions --check --repo .`.
- `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`.
- `mozyo-bridge docs resolve vibes/docs/logics/sublane-bandwidth-policy.md --repo . --format text`.
