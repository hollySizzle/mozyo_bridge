# Workflow Reference

## Start Of Work

- Fetch the global Notion rules page from `AGENTS.md`.
- Confirm the repository root and current `cwd`.
- Confirm the active Asana task. If the task does not exist, create it before implementation.
- Confirm Asana project notes for `mozyo_bridge`.

## Ticket-ID Entrypoint

When the inbound is only a ticket ID, a ticket URL, or pane / chat text naming a ticket, fetch and reconcile the durable ticket record before acting. Pane- or chat-supplied framing does not substitute for the source of truth even when it looks fully framed.

- Identify the ticket system from the ID shape, URL host, or scaffold preset; if it cannot be identified, stop and ask.
- Fetch the ticket via the system's authoritative API, then extract purpose, target paths, artifacts, referenced rules, completion criteria, and prohibitions from the durable record. Reconcile any pane framing against the fetched record before acting.
- For per-system gate / comment semantics, follow the central preset for that ticket system; do not interchange Asana and Redmine vocabularies.
- If any required framing field is missing, ambiguous, or contradicts the parent ticket, do not start implementation. Record the gap in the ticket's durable log first.
- Imperative or request phrases from the user (such as "実行せよ", "対応して", "やって", "implement it") do not override the Codex / Claude role boundary defined below; the entrypoint still routes through the durable record.

## Asana

- Asana is the execution queue.
- Use tasks as executable units with purpose, target paths, output paths, references, done criteria, and prohibitions.
- Update the task when work is completed, blocked, or materially changes scope.
- Do not treat chat as the durable work log.
- Split follow-up work into new Asana tasks when scope expands.
- For a normal development completion comment, record a short audit trail:
  - global Notion rules fetched;
  - `mozyo-bridge-agent` skill loaded;
  - active Asana task and project notes confirmed;
  - any additional relevant rule or reference paths consulted.
- This audit trail is for reviewability. It does not require reading every reference file on every task.

## Local Documentation

- `AGENTS.md` and `CLAUDE.md` are routers.
- `vibes/docs/rules/` holds local working rules.
- `vibes/docs/specs/` holds project structure and specification notes.
- `vibes/docs/logics/` holds decision and release logic.
- `vibes/docs/temps/` holds reusable templates.

## Handoff Lifecycle

Use handoff only when the active project workflow or the user explicitly asks for another agent to participate.

1. The sender records or identifies the durable source of truth first.
2. The sender notifies the receiver through `mozyo-bridge` after the required read/guard step.
3. The receiver starts from the durable source of truth, not from pane text alone.
4. The receiver records findings, blockers, completion notes, and verification in the durable source of truth.
5. The receiver sends a short result notification back to the sender so the sender knows to read the durable record.
6. The sender resumes from the durable record and decides the next action.

Pane messages are notification edges in this lifecycle. They are not review passes, task completion, release approval, or the work log.

## Claude / Codex Role Boundary

- Claude owns implementation for normal development tasks.
- Codex does not directly implement normal development tasks in `mozyo_bridge`.
- Codex owns escalation handling, audit, user-facing clarification, and decisions that can be made from source of truth.
- When Codex receives a normal development task ID, the standard action is to convert it into a Claude handoff, not to implement it. Task size, urgency, implementation difficulty, user impatience, or the user writing directly into the Codex pane do not override this default.
- Imperative or request phrases from the user — for example "実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "go ahead", "please do it" — are not by themselves authorization for Codex to perform a direct edit. They express "I want this done", not "you may bypass Claude".
- The standard handoff is overridden only by an explicit Codex-direct-edit exception defined in the Policy / Skill Authoring Boundary section.
- When Codex receives a workflow-change verification task, Codex selects a valid normal development task, records the selection in Asana, and hands it off to Claude.
- The verification task only counts if Claude performs the normal development work and Codex performs the audit path.
- If Codex mistakenly implements a normal development task directly, that run does not count as the task's normal completion. If it occurred during a verification task, it also does not satisfy workflow-change verification.
- After such a mistake, reopen the affected task, record the mistake, the impact scope, and the follow-up decision (adopt, discard, reimplement) in Asana as a correction, then rerun the flow from Claude implementation through Codex audit. This correction flow applies to every normal development task, not only verification-target tasks.

## Policy / Skill Authoring Boundary

- For autonomous workflow, rules, skills, handoff, audit, or release/distribution gate changes, Codex owns policy framing, draft wording, user-facing clarification, and audit.
- Claude is the default implementer for repository file edits to those policies and skill references.
- Codex must not directly edit and commit policy or skill reference files during ordinary operation.
- A Codex direct edit is permitted only when one of the following narrow exceptions applies. Operate the exception conservatively; when in doubt, or when the user instruction admits more than one reading, fall back to the default and produce a Claude handoff.
  1. The user explicitly authorized a Codex direct edit using wording equivalent to `Codex direct edit`, "Codex が直接編集してよい", or "Codex に直接実装させてよい", scoped to a specific task or file. Generic imperative or request forms ("実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "please do it") do not qualify.
  2. The change is the minimal record-keeping correction needed to capture an existing mistaken implementation, mistaken commit, or mistaken procedure in Asana or the repo.
  3. The change is a genuinely urgent small fix that would be damaged by handoff (for example, a one- or few-line fix needed within minutes to halt an in-progress release, publish, or CI run). Before invoking this exception Codex must stop implementation, record an "urgent direct-edit request" in Asana with the situation, target files, intended change, and impact scope, and obtain user confirmation when possible. Do not apply this exception when the situation is ambiguous or when confirmation cannot be obtained.
- When Codex makes a direct edit under an exception, record `Codex direct edit` in Asana with (a) which exception applied, (b) the verbatim or quoted user instruction, (c) the changed files, (d) the verification performed, and (e) whether follow-up verification is required. A direct edit missing any of these fields is itself subject to a follow-up correction.
- A Codex direct edit to autonomous workflow or role boundaries does not waive the workflow-change verification requirement.

## Workflow Change Verification

- After changing autonomous workflow, skills, rules, handoff, escalation, or
  release/distribution gates, verify the change in a new session.
- Use a normal `mozyo_bridge` development task for that verification.
- Do not use a task that directly changes the workflow/rule/skill area being
  verified.
- Claude implements the normal development task. Codex handles task selection,
  handoff, and audit; Codex must not directly implement the verification target.
- Do not choose the verification target based on task size or production impact.
  The criterion is whether the task directly changes the workflow, skill, or
  gate under verification.
- Record the verification result in Asana and create follow-up tasks for any
  gaps.
