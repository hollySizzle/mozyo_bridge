# No-Ticket Agent Workflow

## Source of Truth

- There is no external execution queue configured by this preset.
- Repository docs and explicit user instructions are the available source of truth.
- Pane messages are notifications only.

## Start of Work

1. Confirm the current project root.
2. Read the project-local docs needed for the task.
3. Confirm ambiguous scope with the user before making non-trivial changes.

## Handoff and Review

- Record durable decisions in repository docs when appropriate.
- Do not invent an implicit queue from chat messages, pane messages, or generated files.
- This preset is weaker than Asana or Redmine for auditability.

## Completion

Before treating work as complete:

1. Verify the requested work.
2. Summarize material changes, verification, blockers, and remaining risks to the user.

## Prohibitions

- Do not treat pane messages or chat messages as authoritative state.
- Do not store credentials, tokens, or personal data in repository files.
