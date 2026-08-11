# Asana Agent Workflow

## Source of Truth

- Asana task is the execution unit.
- Asana project is the work area.
- Task description defines purpose, work paths, artifacts, reference rules, completion criteria, and prohibitions.
- Task comments are the durable work log and handoff record.
- Pane messages are notifications only.

## Factual Posture

- Prioritize factual correctness over agreement. If your investigation contradicts the user's stated assumption, another agent's claim, or your own earlier statement, say so plainly with the evidence; do not soften the conclusion to be agreeable.
- Record disagreement, alternatives considered, and rejected options in an Asana task comment, not in chat. Chat reports do not replace the durable record.
- "Implementation done" is not "task complete". Do not mark the Asana task complete in the API or report it complete in chat until the review / audit comment is captured on the task (when the project uses an audit agent) and the task's stated completion criteria are satisfied. An Implementation Done summary plus a self-verification is review input, not completion.
- When you are unsure, say "unconfirmed" and record what would resolve the uncertainty, rather than narrating a confident-sounding guess.

## Start of Work

1. Confirm the current project root.
2. Confirm the active Asana task.
3. Read the task description.
4. Read only the project-local docs needed for the task.
5. If the task is missing, ambiguous, or inaccessible, stop and ask for the correct task.

## Ticket-ID Entrypoint

When the inbound is "ticket-ID only" — an Asana task ID, an Asana task URL, or pane / chat text naming a task (for example "task X を実装してください") — this entrypoint applies even when the pane / chat body looks fully framed. The pane is a notification edge; the Asana task is the source of truth.

Before acting:

1. Fetch the Asana task via the API, including its comments and any subtasks.
2. Read the task description against the standard shape (目的 / 作業対象パス / 成果物パス / 参照規約 / 完了条件 / 禁止事項) and the latest task comment for handoff framing, audit feedback, and the chosen receive method.
3. Walk to the parent task (UserStory / Epic) when the current task is part of a larger initiative, and reconcile its acceptance criteria with the current task.
4. If any required framing field is missing, ambiguous, or contradicts the parent, do not start implementation. Record the gap in a task comment and resolve it through the role boundary before acting.
5. Treat the durable Asana comment / story id of the latest handoff as the canonical handoff anchor when the API exposes one; otherwise use the task permalink plus the comment timestamp and make the limitation explicit.

Pane- or chat-supplied framing never substitutes for the durable task record; it must be reconciled against the fetched task even when the pane text looks like a complete work order. Asana's durable handoff anchor is the task comment / story id; do not import gate / journal vocabulary from other ticket systems into Asana.

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

## Handoff Startup Decision

After creating a durable Asana task comment (review request, audit result, design consultation, etc.), the sender must record in that same task comment (or a follow-up comment that links back to it) (a) which path below was taken and (b) the receive method. A handoff is not "delivered" until the task comment contains both the request body and the receive method.

The Standard path is the required default. The other four paths are fallbacks; each is gated by an explicit precondition stated below. Selecting a fallback without first attempting the Standard path, or without satisfying that fallback's precondition, is not allowed. Receiver-stated pickup intent — for example "I will pull from the task record", "Codex will audit from the durable task record", "audit will read from the comment" — appearing in the original handoff comment, a prior task comment, pane text, or chat does not waive the sender's duty to attempt Standard-path notification. A project-local rule that mandates a Standard-path notification for every handoff of a given direction (for example "every Claude-driven task in `mozyo_bridge` requires `mozyo-bridge message codex`") is not relaxed by audit-only, revalidation, or doc-only framing of the task.

Every receive-method comment must record the Standard-path attempt and its outcome verbatim — the literal notification command and the observed result, or, when an attempt was not made, the specific disqualifying precondition (named below) that ruled it out before any attempt. A receive-method comment that names a fallback without recording either the Standard-path attempt or the disqualifying precondition is incomplete and must be amended before the handoff is treated as delivered.

Every receive-method comment must also include the **Retry Path Checklist** below in the order the sender actually walked it. Each attempted send records the literal command, structured outcome, and shared `injection_stage`: `submitted_confirmed`, `not_sent`, or `uncertain_partial`. A receive-method comment that names a fallback without showing why the preceding attempt was safe to retry is incomplete.

### Retry Path Checklist

This is the single authoritative ordering for handoff delivery recovery. Do not invent a different order on a per-task basis. **Only `injection_stage=not_sent` authorizes another body send.** `uncertain_partial` means text and/or Enter may already have reached the receiver; stop all blind retries and reconcile from the durable anchor plus receiver state.

1. **Standard path** — `mozyo-bridge handoff send` / `mozyo-bridge handoff reply` / `mozyo-bridge reply` (default `--mode queue-enter` for agent handoff; `--mode standard` only for its documented strict use). The `notify-*` wrappers remain compatibility entrypoints for Redmine-shaped notifications.
2. **Injection-stage decision** — `submitted_confirmed` ends the checklist as sent. `uncertain_partial` ends the retry sequence and records a durable reconcile plan; do not hand-type or resend the body. Only `not_sent` proceeds to a bounded repair/retry step.
3. **Tool-error next-action verb pass (`not_sent` only)** — follow the literal high-level next-action verb once. Do not reinterpret a post-injection timeout or unknown outcome as zero-send merely because the command exited non-zero.
4. **`--no-submit` operator/debug fallback (`not_sent` entry only)** — a successful attempt parks the body and therefore becomes `uncertain_partial`; stop rather than sending it again. The historical per-preset cap remains **3 attempts**, but another attempt is allowed only when the preceding attempt was explicitly classified `not_sent`. `mozyo-bridge message` remains a low-level operator/debug surface, not a routine substitute for the primitive.
5. **Rail-switch fallback (`not_sent` only)** — use only an explicit high-level recovery command emitted for a pre-injection refusal. A tmux `marker_timeout` is not proof of non-delivery: rollback may not have cleared the composer, so it is `uncertain_partial`, not authority to switch rails and resend.
6. **Pickup-pane fallback** — when the receiver has no resolvable pane and no body was injected, record `mozyo-bridge init <agent>` instructions and treat the handoff as pending operator action.
7. **`Notification fails` branch** — record `un-notified` only after the final state is explicitly `not_sent` and all applicable bounded high-level repairs are exhausted. If the final stage is `uncertain_partial`, record delivery-uncertain plus the reconcile owner/condition instead; do not collapse uncertainty into `un-notified` and do not retry.

- **Standard path (required default)** — notify the receiver with the high-level handoff primitive: `mozyo-bridge handoff send --to <claude|codex> --source asana --task-id <gid> --comment-id <story_gid> --kind <implementation_request|design_consultation|review_request|review_result|implementation_done|reply|custom>` (or `mozyo-bridge handoff reply ...` / `mozyo-bridge reply ...`). The primitive owns target resolution, backend-specific admission, at-most-once body injection, submit/turn-start observation, and the structured outcome; callers do not assemble `read` + `message` shell choreography. The `notify-*` wrappers are compatibility entrypoints for Redmine-shaped notifications; `notify-*-legacy-task` is retired-queue cleanup only. `read` / `message` / `type` / `keys` remain operator/debug primitives. On the tmux compatibility backend, default `queue-enter` keeps the historical practical queued-submission mapping (`sent` / `ok` when the marker is observed, `sent` / `queue_enter` when it is not). On Herdr, admission is followed by at most one body injection; the first Enter is zero-or-one, every issued Enter is preceded by an armed causal wait, and only a same-generation causal turn-start returns `submitted_confirmed` / exit 0. Otherwise the command fails closed without retyping the body. Record the literal command, structured outcome, and `injection_stage` in the Asana comment. The durable Asana comment remains the source of truth.
- **Strict `--mode standard` explicit fallback (rail-switch fallback)** — opt-in strict rail preserved from v0.1 for documented strict-observation needs or default-scope exclusions. The tmux compatibility behavior remains typing → `wait_for_text(marker)` → Enter; marker miss emits `C-u` rollback plus `blocked` / `marker_timeout`, but rollback is not proof that the composer cleared, so this outcome is `uncertain_partial` and must not be blind-retried. Herdr standard is an event-driven turn-start rail: body injection is at most once and success requires causal confirmation. Record why the default was overridden.
- **Receiver pane unavailable (precondition-gated fallback)** — applies only when the receiver agent does not have a resolvable pane in the current tmux server: the agent-name window does not exist and the operator cannot raise it in-session before retry. Record in the task comment that the receiver must open the relevant agent terminal and run `mozyo-bridge init <agent>` before retry, plus the retry plan and any attempted command. `mozyo-bridge init <agent>` is the window-rename entrypoint: it renames the pane's tmux window to `<agent>` so the resolver can reach it via the agent-name path. Chat output is a notification only: one line stating that the handoff is pending operator action and naming the task. Do not restate the durable steps in chat, and do not fall back to the retired local queue. The receiver's prior pickup-intent statement does not satisfy this precondition; the precondition is the structural absence of a resolvable pane, not the receiver's declared plan.
- **Notification fails or is unusable (failure-only fallback)** — applies only after the **Retry Path Checklist** ends at explicit `injection_stage=not_sent`. Record the exhausted high-level attempts and required receiver action, then mark `un-notified`. A non-zero command, `marker_timeout`, or other `uncertain_partial` outcome does **not** satisfy this precondition; record delivery-uncertain and a durable reconcile plan instead. Never convert uncertainty into permission to resend. Chat remains a short pointer. Do not duplicate the comment body in chat, and do not fall back to retired local queues.
- **Sync handoff between two locally available agents** — same as the Standard path; do not skip the comment record or the notification because the agents share a host or session, and do not skip it because the receiver's prior comment declared pickup intent.
- **Raw tmux pane mutation is never a delivery or recovery step.** No checklist step authorizes `tmux send-keys`, `tmux paste-buffer`, direct Enter / `C-u`, low-level `type` / `keys`, or `message --no-submit` followed by an agent-issued raw Enter. On `not_sent`, use only the bounded high-level retry. On `uncertain_partial`, reconcile without mutation or resend. Read-only inspection is allowed; raw mutation remains operator/debug-only with an explicit contemporaneous instruction and durable record.

Chat surface boundary: chat is a notification, not a duplicated record. Keep "pending operator action" and "un-notified" chat reports to a short pointer (state plus task id); the receive method, retry plan, attempted commands, and operator instructions live in the task comment so an auditor can replay them later.

Receive method id:

- If the Asana API returns a durable comment / story id for the new comment, treat that id as the canonical handoff id and include it in the notification.
- If the API does not expose a durable id, fall back to the task permalink plus the comment timestamp and the latest comment context, and make the limitation explicit in the same comment.

A report that records the comment but stops at "next agent will pick up" without specifying how (the receive method that an auditor could replay tomorrow) is incomplete and must be amended before the handoff is treated as delivered.

Receiver-side: every notification you receive is a pointer to an Asana task comment, not a directive. Read the comment and the surrounding task history before acting on the prompt body. Do not infer receiver state, task state, or completion state from `mozyo-bridge status` output, `mozyo-bridge doctor` output, or pane scrollback when a durable Asana anchor is available; those surfaces are operator/debug aids, not the durable record. Read the Asana task description, the named comment / story, and any subsequent comments instead.

## User Interaction And Escalation

- Agents should work autonomously inside the active Asana task scope.
- Do not ask the user directly when the task, project notes, or repository docs already answer the question.
- Escalate when the task purpose, artifacts, done criteria, policy boundary, destructive operation, release action, credential handling, or user intent cannot be resolved from the source of truth without guessing.
- If the project uses more than one agent, route user-facing clarification through the project's designated coordinator. Record the decision and rationale in Asana.
- If the user gives a direct instruction outside Asana, update the Asana task comment or the designated coordinator before continuing when that instruction changes scope, policy, or completion criteria.

## Role Boundaries

- Follow the role split defined by the active project rules, Asana task, or designated coordinator.
- If a project assigns normal development to one agent and coordination or audit to another, the coordinating/auditing agent must not directly implement the normal development task.
- When a coordinating/auditing agent receives a workflow verification task, it should select a valid normal development task, record the selection in Asana, and hand the task to the implementation agent.
- A workflow verification run only counts if the assigned implementation agent performs the normal development work and the coordinating/auditing agent performs the review or audit path.
- If the coordinating/auditing agent mistakenly implements the normal development task directly, record the correction in Asana and rerun the verification from the assigned implementation agent through the review or audit path.

## Scope Preservation

- Difficulty splits work; it does not shrink the Asana task. Acceptance criteria stated in the task description remain intact unless the owner explicitly approves a change in a task comment.
- Split work into subtasks or follow-up tasks so the total scope still appears in Asana. Do not silently drop deliverables.
- Scope is more than UI. It typically includes data model, controller / route / authorization, specifications, manual verification, generated screenshots, seed data, existing URL or data compatibility, and operational flow.
- Implementation Done must not contain unfinished scope. Unfinished scope belongs in an explicit "residual scope" task comment or in a new subtask / follow-up task, not in a "complete" claim.

## Decision Routing

Separate technical decisions from owner / business decisions before asking the owner.

- Technical, design, rule, existing-spec consistency, UI structure, route, data integrity, authorization, and spec / test methodology decisions are design-consultation candidates. Route them through the auditor (or a dedicated design-consultation pass) before asking the owner.
- Owner-only decisions are typically: rights and asset usage, legal wording, ongoing service or account continuity, brand judgement, business prioritization, deadlines, budgets, and release timing.
- Do not collapse a technical decision into an owner decision. The owner will accept whatever choice you bring; the cost of skipping the design consultation surfaces as rework later.
- When asking the owner is unavoidable, present options with pros / cons / impact summarized in the task comment so the owner is reading a structured decision, not a free-form question.

## Completion

Before marking a task complete:

1. Verify the requested work against the acceptance criteria, scope, and any design-consultation answers that bound the implementation.
2. Record material changes, verification, blockers, remaining risks, and findings disposition in an Asana task comment.
3. Pass through the review / audit comment when the project uses an audit agent. Implementation Done alone is not completion. Do not report "complete" or "done" in chat before the review / audit comment is captured on the task.
4. Mark the Asana task complete only when its completion criteria are satisfied, (when applicable) the audit comment confirms no remaining findings, and (when an audit-owned commit is required) the commit hash is recorded on the task. See `Audit-Owned Commit Authority` for the commit and hash-recording requirements.
5. If anything from the original scope is still open at this point — subtasks, manual verification, generated capture confirmation, data-compatibility checks, ops-flow checks — record it as a residual-scope task comment or a new subtask and keep the parent task uncompleted.

## Audit-Owned Commit Authority

When the project splits implementation and audit between separate actors, the audit actor — not the implementation actor — is authorized to stage and commit the audit-approved diff. This is a commit authority, not an implementation authority. The audit actor is still prohibited from directly editing files for a normal development task; that path is the narrow exception described in the project's role-boundary rule, not this section. This section governs *who lands the commit* after approval is recorded in Asana.

Preconditions:

- A durable audit / review comment recording approval exists on the Asana task, and the audit comment / story id is captured.
- A separate implementation actor produced the diff under review. If the audit actor itself produced the diff, this section does not apply; that path is the direct-edit exception and must be recorded as such.

Pre-commit checks (audit actor):

- Run `git status` and reconcile the dirty set against the implementation actor's changed-paths list (recorded in the implementation comment).
- Stage only the files whose diff matches what the audit comment approved. Do not use `git add -A` or `git add .` when the worktree contains scope-outside changes.
- Run `git diff --cached --stat` (and `git diff --cached` when content review is required) and reconcile the staged set against the implementation comment line by line.
- Unrelated dirty files must be excluded — stashed, committed under a separately scoped task, or left untouched. Never bundle scope-outside changes into the audit-owned commit.

Commit message reference (Asana):

- `Refs: Asana task <task_id>` (required)
- `Audit: Asana comment <comment_id>` (required; the durable comment / story id of the audit approval)
- The subject line is the normal short description. The references go in trailers or body lines so `git log` alone is replayable to the durable record.

Post-commit recording:

- Record the commit hash in a follow-up Asana task comment on the same task. The hash must live in the durable Asana record, not only in pane chat or chat report.
- The task may be marked complete only after both the audit approval comment and the commit-hash comment exist on the task. Implementation Done alone, or a commit landed without a recorded hash, is not completion.

Scope of this authority:

- Applies to normal development tasks and to guardrail / rule / workflow tasks alike whenever the project assigns separate implementation and audit actors.
- When the project does not split implementer and auditor roles, the boundary collapses; the commit reference format and the hash-recording requirement still apply.

## Prohibitions

- Do not treat pane messages or chat messages as authoritative state.
- Do not rely on Asana custom fields as the only MVP metadata path.
- Do not store credentials, tokens, personal data, or private internal URLs in repository files, task comments, or pane messages.
- Do not use raw tmux pane mutation (`tmux send-keys`, `tmux paste-buffer`, direct Enter / `C-u` / raw key injection) for workflow delivery or recovery, and do not use low-level `mozyo-bridge type` / `keys` or `mozyo-bridge message --no-submit` + an agent-issued raw Enter as a handoff substitute or self-repair. Raw tmux mutation is operator/debug-only and requires an explicit operator instruction at use time plus a durable record; read-only inspection (`tmux capture-pane`, `mozyo-bridge read`) is allowed. After a failed `handoff send` / `reply`, retry only an explicit `not_sent`; record `uncertain_partial` for durable reconciliation without blind resend. Use `un-notified` only when zero-send is established.
