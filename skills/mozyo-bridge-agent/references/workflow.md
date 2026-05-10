# Workflow Reference

## Start Of Work

- Fetch the global Notion rules page from `AGENTS.md`.
- Confirm the repository root and current `cwd`.
- Confirm the active Asana task. If the task does not exist, create it before implementation.
- Confirm Asana project notes for `mozyo_bridge`.

## Asana

- Asana is the execution queue.
- Use tasks as executable units with purpose, target paths, output paths, references, done criteria, and prohibitions.
- Update the task when work is completed, blocked, or materially changes scope.
- Do not treat chat as the durable work log.
- Split follow-up work into new Asana tasks when scope expands.

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
- When Codex receives a workflow-change verification task, Codex selects a valid normal development task, records the selection in Asana, and hands it off to Claude.
- The verification task only counts if Claude performs the normal development work and Codex performs the audit path.
- If Codex mistakenly implements the normal development task directly, that run does not satisfy workflow-change verification. Reopen or leave the verification task incomplete, record the correction in Asana, and rerun the flow from Claude implementation through Codex audit.

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
