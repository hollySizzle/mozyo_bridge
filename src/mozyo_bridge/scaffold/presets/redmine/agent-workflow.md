# Redmine Agent Workflow

## Source of Truth

- Redmine issue is the execution unit and source of truth.
- Redmine journal id is the canonical handoff and review gate. Every formal handoff between agents must be visible as a journal in the same issue.
- Implementation reports, design consultations, review requests, audit results, and close decisions live in Redmine, not in pane messages or chat reports.
- Pane and chat messages are notifications only. They never replace the Redmine gate they refer to. A pane notification is auditable only by the journal id it carries; without a matching journal the notification has no durable record.
- Status and tracker conventions are project-specific and must be configured per Redmine project. Map your project workflow to the gates described below rather than inventing parallel conventions.

## Factual Posture

- Prioritize factual correctness over agreement. If your investigation contradicts the user's stated assumption, another agent's claim, or your own earlier statement, say so plainly with the evidence; do not soften the conclusion to be agreeable.
- Record disagreement, alternatives considered, and rejected options in the relevant Redmine gate, not in chat. Chat reports do not replace the durable record.
- "Implementation Done" is not "complete". Do not report a task as complete in chat or close the issue until the Review Gate is recorded in Redmine and the close conditions are met. An Implementation Done Gate plus a self-verification is review input, not completion; it still requires the Review Request, Review, and Close gates.
- When you are unsure, say "unconfirmed" and record what would resolve the uncertainty, rather than narrating a confident-sounding guess.

## Start of Work

1. Confirm the current project root and the active Redmine issue.
2. Confirm the parent issue (Epic / Feature / UserStory) and capture purpose, acceptance criteria, and known prerequisites.
3. Confirm the relevant journal id for the current handoff or review gate, or create the gate before notifying anyone.
4. Read only the project-local docs needed for the current task. If the project provides a docs catalog or active-doc resolver, use it to find the rules that bind to the changed paths and read the actual rule body, not just titles.
5. If the issue, parent issue, or journal is missing, ambiguous, or inaccessible, stop and ask for the correct gate.

## Ticket-ID Entrypoint

When the inbound is "ticket-ID only" — a Redmine issue ID, a Redmine issue URL, or pane / chat text naming an issue (for example "issue X please handle") — this entrypoint applies even when the pane / chat body looks fully framed. The pane carries a journal pointer; the Redmine issue is the source of truth.

Before acting:

1. Fetch the Redmine issue from the project's Redmine instance, including the most recent journals.
2. Confirm the parent issue (Epic / Feature / UserStory) and capture purpose, acceptance criteria, and known prerequisites.
3. Identify the journal id that bounds the current handoff: typically the Review Request, Design Consultation, or Implementation Done gate. If the named journal does not exist, create the appropriate gate before acting on the prompt body.
4. Map the project's Redmine statuses and trackers to the standard gate lifecycle (Start / Progress Log / Design Consultation / Implementation Done / Review Request / Review / Close); do not invent parallel conventions.
5. If any required framing field is missing, ambiguous, or contradicts the parent, do not start implementation. Record the gap in a Progress Log gate before notifying anyone.

Pane- or chat-supplied framing never substitutes for the durable issue record; it must be reconciled against the fetched issue even when the pane text looks like a complete work order. Do not collapse Redmine semantics into Asana's single-comment-thread shape — the canonical handoff id is the Redmine journal, and gate ordering matters for audit replay.

## Redmine Gate Lifecycle

The standard lifecycle for a normal development task. Each gate must be a durable Redmine record. Pane notifications carry the journal id that points back to the gate.

1. **Start Gate** — record purpose, parent issue, acceptance criteria, referenced docs, and known unknowns. If the project's Redmine workflow defines a status that maps to "work has started", update the issue to that status; otherwise leave the status alone and rely on the Start Gate journal as the durable record.
2. **Progress Log Gate** — record material progress, decisions taken, items deferred to owner decision, and the next concrete action. Use whenever scope, blockers, or assumptions shift, not only at the end.
3. **Design Consultation Gate** — record before implementation when the choice is hard to reverse: spec interpretation, scope of responsibility, persistence shape, automatic correction, UI flow, authorization, DB integrity, and similar. The gate must separate background, canonical references, options, pros/cons, the implementer's recommended option, the questions that need a decision, and remaining unknowns.
4. **Design Consultation Answer Gate** — recorded by the consulting agent (audit/design role). Must separate selected options, rationale, why rejected options were rejected, remaining unknowns, and items that still require owner decision.
5. **Implementation Done Gate** — record changed paths, intent, assumptions, unknowns, verification result, doc updates, and commit hash. Implementation Done is not completion; it is a stable input for review.
6. **Review Request Gate** — record the implementation task, parent user story or feature, audit/test issues if any, target commit, the explicit review focus, and known unknowns. Send a pane notification only after this gate exists.
7. **Review Gate** — recorded by the reviewer. Must separate target commit, matched rules, compliance judgement, findings ordered by severity with file/line references, unknowns, and whether re-review is required.
8. **Close Gate** — record acceptance result, findings disposition, remaining risks, audit result, Epic/Feature close basis if applicable, retired-queue stale check if applicable, and the close decision. Close only after owner approval and a passing review.

If the project uses different Redmine statuses or trackers, map them to these gates. Do not invent parallel conventions.

## Pane Notification

- Standard notification command: `mozyo-bridge notify-* --issue <issue_id> --journal <journal_id>`.
- Always create or confirm the Redmine journal before sending a pane notification. Notification before journal is order-dependent and breaks audit replay.
- Use `mozyo-bridge notify-codex-review` and `mozyo-bridge notify-claude-review-result` for the review handoff pair when both endpoints are tied to a single journal.
- Use `mozyo-bridge notify-codex --type design_consultation` and `mozyo-bridge notify-claude --type design_consultation_result` for design consultation pairs.
- Pane notification success is not a review record. Pane notification failure is not a review failure. The Redmine gate is the only record.
- The recipient must check the named gate before acting. Acting on the prompt body alone is unsafe.
- The retired `.agent_handoff/tasks.yaml` queue and any `read-next --wait` style fallback are not standard. They exist only to drain leftover state from the legacy local queue.

## Handoff Startup Decision

After recording a Redmine gate (Review Request, Design Consultation, etc.), the sender must choose one of the paths below and write the chosen receive method into the same Redmine record. A handoff is not "delivered" until the Redmine record contains both the gate and the receive method.

- **Standard path** — notify the receiver pane with `mozyo-bridge notify-* --issue <issue_id> --journal <journal_id>` and record in the Redmine gate the literal command line used (or its equivalent: "notified <agent> via mozyo-bridge journal <journal_id>"). The receiver picks up the journal id, opens the gate in Redmine, and acts from there.
- **Receiver pane unavailable** — record in the Redmine gate that the receiver must open the relevant agent terminal and run `mozyo-bridge init <agent>` before retry, plus the retry plan and any attempted command. `mozyo-bridge init <agent>` is the window-rename entrypoint: it renames the pane's tmux window to `<agent>` so the resolver can reach it via the agent-name path. Chat output is a notification only: one line stating that the handoff is pending operator action and naming the issue / gate. Do not restate the durable steps in chat, and do not fall back to the retired local queue.
- **Notification fails or is unusable** — record the un-notified state explicitly in the Redmine gate ("not yet notified; receiver must read the gate manually") along with what was attempted (literal command, observed error) and the required receiver action. Chat output is a notification only: one line stating that the handoff is un-notified and naming the issue / gate. Do not duplicate the gate body in chat, and do not fall back to `.agent_handoff/tasks.yaml`, `read-next --wait`, or Stop hook handoff waits to "auto-pick-up" the work.
- **Sync handoff between two locally available agents** — same as the standard path; do not skip the journal record because the agents share a host or session.

Chat surface boundary: chat is a notification, not a duplicated record. Keep "pending operator action" and "un-notified" chat reports to a short pointer (state plus issue / journal id); the receive method, retry plan, attempted commands, and operator instructions live in the Redmine gate so an auditor can replay them later.

A report that records the gate but stops at "next agent will pick up" without specifying how (the receive method that an auditor could replay tomorrow) is incomplete and must be amended before the handoff is treated as delivered.

Receiver-side: every notification you receive is a pointer to a Redmine gate, not a directive. Read the gate and the surrounding issue history before acting on the prompt body.

## Implementer / Auditor Role Boundary

If the project assigns one agent as the implementer and another as the auditor (or design-consultation responder), the constraints below apply. If a single agent owns both roles, scale the constraints to the agent that owns the action; the boundary still applies within that agent.

- The implementer owns code, schema, tests, and operational changes that satisfy the issue. The implementer also records Implementation Done and Review Request gates.
- The auditor owns review, design-consultation answers, rule interpretation, and recording the decision in Redmine.
- The auditor must not directly implement files for normal development tasks. If the auditor receives a normal development issue, the standard action is to hand the issue to the implementer, not to implement it.
- Imperative or request phrases from the user — for example "do it", "implement", "go ahead", "対応して", "実行せよ" — are not by themselves authorization for the auditor to bypass the implementer.
- Direct edits by the auditor are reserved for narrowly scoped exceptions: explicit scoped authorization quoting "direct edit" wording, the minimum record-keeping correction needed to log a mistake, or a genuinely urgent fix where handoff would damage an in-progress release. Any direct edit must record (a) which exception applied, (b) the user instruction quoted verbatim, (c) the changed files, (d) the verification, and (e) any required follow-up review.
- If the project does not split implementer and auditor roles, do not invent the split mid-task. Add the split deliberately, with both agents informed and the boundary recorded in the project rules.

## Decision Routing

Separate technical decisions from owner decisions before asking the owner.

- Technical, design, rule, existing-spec consistency, UI structure, route, DB, authorization, and spec/test methodology decisions are design-consultation candidates. Route them through the auditor (or a dedicated design-consultation pass) before asking the owner.
- Owner-only decisions are typically: rights and asset usage, legal wording, ongoing service or account continuity, brand judgement, business prioritization, deadlines, budgets, and release timing.
- Do not collapse a technical decision into an owner decision. The owner will accept whatever choice you bring; the cost of skipping the design consultation is hidden in later rework.

## Scope Integrity

- Difficulty splits work; it does not shrink the issue. Acceptance criteria remain intact unless the owner explicitly approves a change.
- Split work into child issues, tasks, or tests so the total scope still appears in Redmine. Do not silently drop work.
- Scope is more than UI. It typically includes DB, model, controller, route, authorization, specifications, manual verification, generated screenshots, seed data, existing URL or data compatibility, and operational flow.
- Implementation Done must not contain unfinished scope. Unfinished scope belongs in a Progress Log gate or a child issue.

## Verification Discipline

- Run the project's authoritative verification commands. If the project provides a docs catalog or generated file convention, follow it; do not skip in favor of ad-hoc grepping.
- For UI, screen, or operational flow changes, do not treat selector-level success as completion. Confirm the visible artifact (screenshot, generated capture, or visual review) shows the intended element, transition, and state change.
- For non-UI changes, record concrete verification: tests run, commands executed, observed output, and remaining risks. "Looks fine" is not a verification record.
- If verification fails or is impossible, record the reason in Redmine before notifying anyone.

## Stale and Retired Queue Handling

- The retired local queue (`.agent_handoff/tasks.yaml` and any `read-next --wait` fallback) is not a standard transport. Do not reintroduce it as a regular path.
- If retired queue residue is suspected after a session restart or before close, only then run the project's stale-list command and reconcile each remaining task against the Redmine gate. Treat Redmine as authoritative; close, fail, or discard the queue entry to match.
- Do not close an issue based on queue state alone. Match the close to a passing Review Gate and an explicit owner approval.

## Completion

Before treating work as complete:

1. Verify the requested work against the acceptance criteria, scope, and any design-consultation answers that bound the implementation.
2. Record material changes, verification, blockers, remaining risks, and findings disposition in Redmine.
3. Pass through the Review Gate. Implementation Done alone is not completion. Do not report "complete" or "done" in chat before the Review Gate is recorded in Redmine.
4. Update issue status only according to the project's Redmine workflow. Owner approval governs final close. When an audit-owned commit is required, the commit hash must be journaled in Redmine before the issue moves to closed. See `Audit-Owned Commit Authority`.
5. If anything from the original scope is still open at this point — child issues, manual verification, generated capture confirmation, data-compatibility checks, ops-flow checks — it is a Progress Log entry, not a completion. Record it as such and keep the parent issue out of the closed state.

## Audit-Owned Commit Authority

When the project splits implementation and audit between separate actors, the audit actor — not the implementation actor — is authorized to stage and commit the audit-approved diff. This is a commit authority, not an implementation authority. The audit actor is still prohibited from directly editing files for a normal development issue; that path is the narrow exception in `Implementer / Auditor Role Boundary`, not this section. This section governs *who lands the commit* after the Review Gate is recorded in Redmine.

Preconditions:

- A Review Gate journal recording approval exists on the issue, and the journal id is captured.
- A separate implementation actor produced the diff under review. If the audit actor itself produced the diff, this section does not apply; that path is a direct edit and must be journaled as such under the role-boundary exceptions.

Pre-commit checks (audit actor):

- Run `git status` and reconcile the dirty set against the Implementation Done Gate's changed-paths list.
- Stage only the files whose diff matches what the Review Gate approved. Do not use `git add -A` or `git add .` when the worktree contains scope-outside changes.
- Run `git diff --cached --stat` (and `git diff --cached` when content review is required) and reconcile the staged set against the Implementation Done Gate line by line.
- Unrelated dirty files must be excluded — stashed, committed under a separately scoped issue, or left untouched. Never bundle scope-outside changes into the audit-owned commit.

Commit message reference (Redmine):

- `Refs: Redmine #<issue_id>` (required)
- `Journal: <journal_id>` (required; the journal id of the Review Gate approval)
- The subject line is the normal short description. The references go in trailers or body lines so `git log` alone is replayable back to the Redmine issue and the approving Review Gate.

Post-commit recording:

- Record the commit hash in a Close Gate journal on the same issue (or in a Progress Log journal if the Close Gate is not yet ready). The hash must live in the durable Redmine record, not only in pane chat.
- The issue may move to its project's closed status only after the Review Gate journal and the commit-hash journal are both recorded. Implementation Done alone, or a commit landing without a journaled hash, is not closure.

Scope of this authority:

- Applies to normal development issues and to guardrail / rule / workflow issues alike whenever the project assigns separate implementation and audit actors.
- When the project does not split implementer and auditor roles, the boundary collapses; the commit reference format and the hash-journal requirement still apply.

## Prohibitions

- Do not hard-code a fixed agent role split such as "Claude Code implements, Codex only audits" into the project rules. The implementer/auditor split is project-configurable; the boundary applies only when the project has actually assigned the split.
- Do not treat pane messages, chat messages, or queue files as authoritative state.
- Do not rely on the retired `.agent_handoff/tasks.yaml` queue, `read-next --wait`, or Stop hook handoff waits for normal operation.
- Do not reintroduce `vibes/tools/mozyo_bridge` as a runtime path. Use the installed `mozyo-bridge` CLI.
- Do not embed source-project paths (Rails-specific app paths, custom resolver scripts, private docs catalogs, source-project file_conventions) as mandatory dependencies in the shared preset. Reference such things only with "if the project provides X, use it" wording.
- Do not store credentials, tokens, personal data, or private internal URLs in repository files, Redmine notes, or pane messages.
- Do not perform release tags, version bumps, or publish actions from inside a normal development task without an explicit, separately scoped release task.
