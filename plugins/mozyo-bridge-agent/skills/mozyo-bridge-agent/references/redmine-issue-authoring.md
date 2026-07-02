# Redmine Issue Authoring And Version Operation Reference

How to write Epic / Feature / UserStory / leaf issues and how to operate Redmine Versions so an LLM can author tickets and pick dispatch candidates without guessing (Redmine #13024). This reference carries the portable authoring / planning guideline; the gate vocabulary, required journal fields, and close conditions stay in the central preset, and the subject / description mechanics stay in `references/workflow.md` `### Issue Subject / Description Separation`. Where the two would overlap, this document points instead of restating.

## Hierarchy Granularity Decision Table

Pick the tracker by what the record is *for*, not by how big the work feels:

| Level | What it is | Completion semantics | Author it when |
| --- | --- | --- | --- |
| **Epic** | Long-lived investment area in the product portfolio | Not a work-completion unit; normally stays open for years | A durable product / governance area needs a portfolio node |
| **Feature** | Continuing capability category under an Epic | Not a work-completion unit; normally stays open | A capability inside an Epic will accumulate UserStories over time |
| **UserStory** | The standard unit of work and acceptance — roughly `1 US = 1 branch / 1 worktree / 1 PR` | Closes via review / owner close approval / Close Gate (central preset `## Completion`) | Work is being planned, dispatched, or accepted |
| **Task / Test / Bug (leaf)** | Breakdown, verification, or defect *inside* a US scope | Closes on a replayable implementation_done journal + commit record (task_close) | A US needs an auditable sub-record, or a preset-listed task-level exception applies |

- **Epic / Feature are a catalog, not a queue.** They express "this area is still part of the product". Do not close them because the current UserStories finished, and do not dispatch them as implementation units without an explicit owner / operator decision (see the work-unit granularity contract distributed with the governed preset).
- **The UserStory is the standard work unit** (`1US = 1作業単位`). Plan it so one implementer can carry its child Task / Test / Bug issues end to end in one lane: one branch / worktree / PR-equivalent of scope. If a US cannot fit that shape, split it into multiple USes rather than growing an umbrella.
- **Leaf issues are subordinate.** Create them to structure a US, not as free-standing work; a standalone leaf dispatch is the preset's task-level exception, and the reason must land in the dispatch decision journal.
- **Ordering prefix convention (recommended).** Epic and Feature numbering prefixes are each an independent sequence, and a 10-step prefix (`110`, `120`, `130`, ...) expresses workflow reading order — the order an agent should scan the catalog — not priority or progress. The gaps leave room to insert areas later without renumbering. Adopting projects may use another ordering convention; whatever is chosen, keep it a display/reading order, never an identity or routing key.

## Owner Utterance And Normalized Intent

Ticket descriptions that originate from owner conversation keep two separated sections instead of one blended summary:

- **原文要点 (owner utterance digest)** — a lightly condensed record of what the owner actually said. Summarizing is fine; dropping a thought, policy, or concern is not. When later work seems to contradict the ticket, this section is what arbitration reads.
- **Normalized intent** — the actionable restatement: what the work is, in scope terms an implementer can execute.

Scope / Close conditions / Non-goals then follow from the normalized intent. Never overwrite the utterance digest to match a later reinterpretation; append instead.

## UserStory Close Conditions And Acceptance Notes

Write close conditions so a conversation-driven flow can verify them without re-asking the owner:

- Each close condition is an observable statement about the repo, docs, or durable record ("X is documented in Y", "Z passes"), not a feeling ("works well").
- Keep the list short (roughly 3–6 items); if it grows past that, the US is probably two USes.
- Non-goals are part of acceptance: state what the US deliberately does not do, so audit does not read missing work as a gap.
- Boundary references beat duplication: when a sibling US owns part of the topic, name it ("placement is #NNNN's scope") instead of restating its rules.

## Version Operation

A Redmine Version is a **planning / release-readiness / lane-inventory bucket** — the primary candidate range for release planning, sprint-like grouping, readiness windows, and lane dispatch. It is **not** the package version authority: tags, package metadata, release notes, and the release journal own the shipped version number, and Version names must not pre-encode future package numbers.

Sizing and lifecycle:

- **Target roughly 10–20 UserStories per Version.** A Version that is too small starves sublane ticket inventory; one that is too large stops being a readiness window. Split or merge on that signal.
- **Create Versions around existing candidates, not ahead of them.** Do not pre-create an empty Version as a placeholder; create or select one when candidate USes exist to fill it.
- **Prefer existing related Versions for follow-ups.** A follow-up US goes into the Version whose theme it continues; do not mint a new Version per follow-up wave.

Dispatch candidate selection:

1. Prefer ready UserStories in the current Version — that is what the Version-as-inventory model is for.
2. When the current Version's ready inventory runs dry, refill from the related Feature's USes or an adjacent (theme-continuing) Version, and record the refill reason in the dispatch decision journal.
3. Version membership never overrides the durable-record gates: a US is dispatchable because its record is ready, not because of which bucket it sits in, and sharing (or not sharing) a Version is by itself neither a serialization nor a parallelization reason.

## Boundaries

- Gate names, required fields, review / close semantics: central preset (this document adds no gate vocabulary).
- Subject / description authoring mechanics and the explicit-subject-on-create rule: `references/workflow.md` `### Issue Subject / Description Separation`.
- Where this guideline itself lives (distributed body vs an adopting repo's local docs): `references/workflow.md` `## Workflow Docs Source-Of-Truth Boundary` (Redmine #13025). Repo-specific Version names, concrete Epic / Feature catalogs, and workspace numbering adoptions are repo-local facts and stay out of this distributed body.
- Keep operator-specific policy out of OSS defaults: concrete inventory thresholds beyond the 10–20 guide, private prioritization, and named business domains belong to the operator's runbook, per the public / private boundary rule.
