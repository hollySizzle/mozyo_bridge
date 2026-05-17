# Autonomous Ticket-ID Entrypoint

## Purpose

When a user, another agent, or a `mozyo-bridge` pane delivers only a ticket ID (or a ticket URL whose body is "just look at this ticket"), this document defines the standard entrypoint so that Claude / Codex initial behavior is unambiguous, ticket-system aware, and consistent with the role boundary defined in `vibes/docs/rules/agent-workflow.md`.

This entrypoint is for the autonomous-handoff case where the inbound message does not by itself carry purpose, target paths, completion criteria, or prohibitions. The agent must resolve those from the ticket's source of truth before acting.

## What Counts As "Ticket-ID Only"

Any of the following inbound shapes counts as ticket-ID-only and triggers this entrypoint:

- A numeric or alphanumeric ticket ID with no accompanying scope description.
- A ticket URL (Asana task permalink, Redmine issue URL) without scope description.
- A `mozyo-bridge` pane message that names a ticket ID / URL plus a short framing line, including patterns like "Asana task X を実装してください" or "Issue X please handle".
- A short prose request that resolves to a single ticket without spelling out target paths, done conditions, or prohibitions.

If the inbound message contains no ticket identifier at all (no ID, no URL, no pane text naming a ticket), this entrypoint may not apply and the request should be handled as a standalone instruction or escalated. Whenever a ticket ID or URL is present, always run this entrypoint and fetch the durable ticket record, even if the inbound pane / chat text appears to contain full task framing. Pane- or chat-supplied framing does not substitute for the ticket source of truth; it must be reconciled against the fetched record before acting.

## Cross-System Entry Sequence

Run these steps before any implementation, in this order, for every ticket system:

1. Identify the ticket system from the ID shape, URL host, project context, or `cwd` scaffold preset. If the system cannot be identified, stop and ask; do not guess.
2. Fetch the ticket from the system's authoritative API (Asana task, Redmine issue). Never rely on the pane text as the body of truth.
3. Read the ticket description and the latest relevant gate / comment to extract:
   - purpose / objective
   - work target paths
   - artifact / output paths
   - referenced rules
   - completion criteria
   - prohibitions
4. If any required extraction field is missing, ambiguous, or contradicts the ticket's parent (epic / user story), do not start implementation. Record the gap in the ticket's durable log and escalate per role boundary.
5. Confirm the local `cwd`, repository root, and scaffold preset match the ticket system. If they do not match, stop and ask.
6. Choose the agent role for this ticket per the role boundary section below before touching any file.

Pane text never replaces this sequence. A pane message is a notification edge in the handoff lifecycle (`skills/mozyo-bridge-agent/references/workflow.md`), not the work order.

## Per-System: Asana

Source of truth and gates as specified in the central Asana preset (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md`).

When the inbound delivers only an Asana task ID or URL:

1. Fetch the task via the Asana MCP `get_task` tool. Include comments and subtasks.
2. Read the description against the standard task shape (目的 / 作業対象パス / 成果物パス / 参照規約 / 完了条件 / 禁止事項).
3. Read the latest task comment for the most recent handoff framing, audit feedback, and selected receive method.
4. Walk to the parent task (UserStory / Epic) when the current task is part of a larger initiative, to capture acceptance criteria that bound this task.
5. Confirm any referenced project notes or `llm:` project description metadata used by the workspace.
6. If the Asana API exposes a durable comment / story id for the latest handoff, treat that id as the canonical handoff anchor; otherwise fall back to the task permalink plus comment timestamp and make the limitation explicit when recording.

Do not extend Redmine journal vocabulary into Asana. Use task comments as the durable log, not invented "gates" that do not exist in Asana.

## Per-System: Redmine

Source of truth and gates as specified in the central Redmine preset (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`).

When the inbound delivers only a Redmine issue ID or URL:

1. Fetch the issue from the project's Redmine instance.
2. Confirm the parent issue (Epic / Feature / UserStory) and capture purpose, acceptance criteria, and prerequisites.
3. Identify the most recent journal id that bounds the current handoff: typically the Review Request, Design Consultation, or Implementation Done gate.
4. Verify that the named journal exists. If it does not, do not act on the pane prompt; create or request the appropriate gate first.
5. Map the project's Redmine statuses and trackers to the standard gate lifecycle (Start / Progress Log / Design Consultation / Implementation Done / Review Request / Review / Close). Do not invent parallel conventions.
6. Use `mozyo-bridge notify-* --issue <issue_id> --journal <journal_id>` semantics when later notifications are needed; do not notify before the journal exists.

Do not collapse Redmine semantics into Asana's "single comment thread" shape. Redmine's canonical handoff id is the journal, and gate ordering matters for audit replay.

## Per-Role: Claude (Implementer)

Claude is the default implementer for normal development tasks. On a ticket-ID-only inbound:

1. Run the cross-system entry sequence above.
2. Extract purpose, target paths, artifacts, references, completion criteria, and prohibitions from the ticket source of truth.
3. If any extraction field is missing or ambiguous, do not start coding. Record the gap as a comment on the ticket (Asana comment or Redmine Progress Log gate) and either fix the gap from the parent ticket / project rules, or hand the framing question back to Codex per the role boundary.
4. Read only the project-local docs needed for the task. Follow the routing in this repository's `AGENTS.md` and `CLAUDE.md`; do not pull in unrelated rule files.
5. Begin implementation when, and only when, every required framing field is unambiguous and the role boundary places this ticket on Claude.
6. Record material progress, design decisions, verification, and remaining risks back in the ticket's durable log. Send a return notification per the handoff lifecycle once the durable record is written.

## Per-Role: Codex (Framing / Audit)

Codex does not directly implement normal development tasks in `mozyo_bridge`. On a ticket-ID-only inbound:

1. Run the cross-system entry sequence above.
2. Decide which of these standard moves the inbound is:
   - Normal development task → convert to a Claude handoff. Record purpose, target paths, artifacts, completion criteria, prohibitions, and audit focus on the ticket, then notify Claude per the handoff lifecycle.
   - Workflow / skill / rule / handoff / audit / release-distribution change → still convert to a Claude handoff for the repo file edits, and reserve Codex for framing, audit, and user-facing clarification.
   - Workflow-change verification target → select a valid normal development ticket (not the workflow-change ticket itself), record the selection, and hand it to Claude.
   - Record-keeping correction, or genuinely urgent small fix that would be damaged by handoff → only then consider a Codex direct edit, scoped to the narrow exceptions in `vibes/docs/rules/agent-workflow.md` (Policy / Skill Authoring Boundary).
3. Imperative or request phrases from the user — "実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "please do it", and equivalents — are not by themselves authorization for Codex to bypass the Claude handoff. They express "I want this done", not "you may skip Claude".
4. Audit of completed work happens after the durable record is written by Claude. Pane echoes do not count as audit pass.

When the inbound is ambiguous about whether it is a normal development task or a Codex-scope task, default to producing a Claude handoff. The cost of an unnecessary handoff is one round trip; the cost of a wrongful Codex direct edit is a correction flow per `vibes/docs/rules/agent-workflow.md`.

## Pane Message Guardrail

This is the cross-cutting rule that anchors this entrypoint:

- `mozyo-bridge` pane messages, chat messages, and tmux pane echoes are notifications, not source of truth.
- The body of the work order lives in the Asana task or Redmine issue. A pane message is the pointer to that body.
- "Notified" is not "delivered" until the ticket's durable log contains both the request body and the chosen receive method (`mozyo-bridge message <pane>` line, or an explicit fallback note when notification is unusable).
- Receiver-side: every notification you receive is a pointer to a ticket record, not a directive. Read the record and the surrounding history before acting on the prompt body.

If the pane message is the only artifact and there is no matching ticket record, stop and create the ticket record before proceeding.

## Routing Summary

| Inbound shape | Source of truth | First read | Canonical handoff anchor |
| --- | --- | --- | --- |
| Asana task ID / URL | Asana task | task description + latest comment | durable comment / story id if API exposes it, else permalink + timestamp |
| Redmine issue ID / URL | Redmine issue | issue description + latest journal | Redmine journal id |
| Pane message naming a ticket | the named ticket (above) | as above | as above |
| Pane message with no ticket | (none yet) | create the ticket first | n/a until ticket exists |

## Prohibitions

- Do not start implementation from pane text without resolving the ticket source of truth.
- Do not treat Asana task IDs and Redmine issue IDs as interchangeable. Each carries different gate semantics; do not import Redmine journal vocabulary into Asana or compress Redmine gates into a single Asana-style comment thread.
- Do not bloat root `AGENTS.md` / `CLAUDE.md` with the entrypoint body. They route to this document; they do not replicate it.
- Do not let imperative or request phrases from the user override the Codex / Claude role boundary.
- Do not perform Codex direct edits on policy / skill / rule files outside the narrow exceptions defined in `vibes/docs/rules/agent-workflow.md`.
- Do not embed private Notion URLs, credentials, or tokens in this document or in any scaffold preset that ships to other projects.

## Cross-References

- Repo-local working rules: `vibes/docs/rules/agent-workflow.md`
- Shared skill workflow: `skills/mozyo-bridge-agent/references/workflow.md`
- Notification safety: `skills/mozyo-bridge-agent/references/safety.md`
- ACK / completion / receiver-state boundary doctrine: `vibes/docs/logics/ack-completion-receiver-state.md`
- Central Asana preset: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md`
- Central Redmine preset: `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`
- Scaffold rules logic: `vibes/docs/logics/scaffold-rules.md`
- Skill distribution logic: `vibes/docs/logics/skill-distribution.md`

Minimal reflection of this entrypoint into the central scaffold presets and the shared skill references is out of scope for the doc-side definition and should be tracked as a follow-up task. The presets must keep ticket-system semantics distinct; do not copy this document verbatim into both presets.
