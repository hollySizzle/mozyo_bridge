# Asana Agent Workflow

## Source of Truth

- Asana task is the execution unit.
- Asana project is the work area.
- Task description defines purpose, work paths, artifacts, reference rules, completion criteria, and prohibitions.
- Task comments are the durable work log and handoff record.
- Pane messages are notifications only.

## Start of Work

1. Confirm the current project root.
2. Confirm the active Asana task.
3. Read the task description.
4. Read only the project-local docs needed for the task.
5. If the task is missing, ambiguous, or inaccessible, stop and ask for the correct task.

## Task Description Shape

```markdown
## 目的

## 作業対象パス

## 成果物パス

## 参照規約

## 完了条件

## 禁止事項
```

## Handoff and Review

- Use Asana comments for durable handoff notes, review requests, findings, blockers, and completion summaries.
- If a durable Asana story/comment id is available, include it in notifications.
- If a story/comment id is unavailable, use the task permalink plus timestamp or latest comment context.
- Project status updates are for project-level progress, not ordinary task handoffs.

## Completion

Before marking a task complete:

1. Verify the requested work.
2. Record material changes, verification, blockers, and remaining risks in an Asana comment.
3. Mark the task complete only when its completion criteria are satisfied.

## Prohibitions

- Do not treat pane messages or chat messages as authoritative state.
- Do not rely on Asana custom fields as the only MVP metadata path.
- Do not store private Notion URLs, credentials, tokens, or personal data in public templates or repository files.
