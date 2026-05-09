# Redmine Agent Workflow

## Source of Truth

- Redmine issue is the execution unit and source of truth.
- Redmine journal id is the canonical handoff and review gate.
- Notification payloads must point to the same issue and journal as the durable work record.
- Pane messages are notifications only.

## Start of Work

1. Confirm the current project root.
2. Confirm the active Redmine issue.
3. Confirm the relevant journal id for the current handoff or review gate.
4. Read only the project-local docs needed for the task.
5. If the issue or journal is missing, ambiguous, or inaccessible, stop and ask for the correct gate.

## Review Flow

- Create or confirm the Redmine journal before sending a pane notification.
- Use `mozyo-bridge notify-* --issue <id> --journal <id>` so the recipient can verify the same durable gate.
- Review requests and review results belong in Redmine journals, not in pane messages.
- Status and tracker conventions are project-specific and must be configured per Redmine project.

## Completion

Before treating work as complete:

1. Verify the requested work.
2. Record material changes, verification, blockers, and remaining risks in Redmine.
3. Update issue status only according to the project's Redmine workflow.

## Prohibitions

- Do not hard-code a fixed agent role split such as "Claude Code implements, Codex only audits".
- Do not treat pane messages or chat messages as authoritative state.
- Do not reintroduce `vibes/tools/mozyo_bridge` as a runtime path.
- Do not store credentials, tokens, or personal data in repository files or Redmine notes.
