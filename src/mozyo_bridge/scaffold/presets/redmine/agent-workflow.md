# Redmine Agent Workflow

## Source of Truth

- Redmine issue is the execution unit and source of truth.
- Redmine journal id is the canonical handoff and review gate. Every formal handoff between agents must be visible as a journal in the same issue.
- Implementation reports, design consultations, review requests, audit results, and close decisions live in Redmine, not in pane messages or chat reports.
- Pane and chat messages are notifications only. They never replace the Redmine gate they refer to. A pane notification is auditable only by the journal id it carries; without a matching journal the notification has no durable record.
- Status and tracker conventions are project-specific and must be configured per Redmine project. Map your project workflow to the gates described below rather than inventing parallel conventions.

## Start of Work

1. Confirm the current project root and the active Redmine issue.
2. Confirm the parent issue (Epic / Feature / UserStory) and capture purpose, acceptance criteria, and known prerequisites.
3. Confirm the relevant journal id for the current handoff or review gate, or create the gate before notifying anyone.
4. Read only the project-local docs needed for the current task. If the project provides a docs catalog or active-doc resolver, use it to find the rules that bind to the changed paths and read the actual rule body, not just titles.
5. If the issue, parent issue, or journal is missing, ambiguous, or inaccessible, stop and ask for the correct gate.

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
3. Pass through the Review Gate. Implementation Done alone is not completion.
4. Update issue status only according to the project's Redmine workflow. Owner approval governs final close.

## Prohibitions

- Do not hard-code a fixed agent role split such as "Claude Code implements, Codex only audits" into the project rules. The implementer/auditor split is project-configurable; the boundary applies only when the project has actually assigned the split.
- Do not treat pane messages, chat messages, or queue files as authoritative state.
- Do not rely on the retired `.agent_handoff/tasks.yaml` queue, `read-next --wait`, or Stop hook handoff waits for normal operation.
- Do not reintroduce `vibes/tools/mozyo_bridge` as a runtime path. Use the installed `mozyo-bridge` CLI.
- Do not embed source-project paths (Rails-specific app paths, custom resolver scripts, private docs catalogs, source-project file_conventions) as mandatory dependencies in the shared preset. Reference such things only with "if the project provides X, use it" wording.
- Do not store credentials, tokens, personal data, or private internal URLs in repository files, Redmine notes, or pane messages.
- Do not perform release tags, version bumps, or publish actions from inside a normal development task without an explicit, separately scoped release task.
