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
