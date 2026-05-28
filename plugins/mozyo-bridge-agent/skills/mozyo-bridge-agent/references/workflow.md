# Workflow Reference

## Start Of Work

- Fetch the central preset rules named in `AGENTS.md` (`mozyo_bridge` uses `redmine-governed`; other repos may use `redmine`, `asana`, or `none`).
- Confirm the repository root and current `cwd`.
- Confirm the active ticket in the repo's ticket system: a Redmine issue / journal for Redmine-preset repos (including `mozyo_bridge`), an Asana task for Asana-preset repos. If the ticket does not exist, create it before implementation.
- Confirm the project notes / parent issue / parent task for `mozyo_bridge`.

## Ticket-ID Entrypoint

When the inbound is only a ticket ID, a ticket URL, or pane / chat text naming a ticket, fetch and reconcile the durable ticket record before acting. Pane- or chat-supplied framing does not substitute for the source of truth even when it looks fully framed.

- Identify the ticket system from the ID shape, URL host, or scaffold preset; if it cannot be identified, stop and ask.
- Fetch the ticket via the system's authoritative API, then extract purpose, target paths, artifacts, referenced rules, completion criteria, and prohibitions from the durable record. Reconcile any pane framing against the fetched record before acting.
- For per-system gate / comment semantics, follow the central preset for that ticket system; do not interchange Asana and Redmine vocabularies.
- If any required framing field is missing, ambiguous, or contradicts the parent ticket, do not start implementation. Record the gap in the ticket's durable log first.
- Imperative or request phrases from the user (such as "実行せよ", "対応して", "やって", "implement it") do not override the Codex / Claude role boundary defined below; the entrypoint still routes through the durable record.

## Ticket System Conventions

The active ticket system is whichever the repo's central preset selects. For `mozyo_bridge` itself this is Redmine; other adopting repos may use Asana. Do not interchange the two vocabularies — the per-system gate names, comment / journal semantics, and required fields live in the central preset, not here.

Common to both:

- The ticket is the execution queue, not chat.
- Treat tickets as executable units with purpose, target paths, output paths, references, done criteria, and prohibitions.
- Update the ticket when work completes, blocks, or materially changes scope.
- Split follow-up work into new tickets when scope expands.
- For a normal development completion entry, record a short audit trail:
  - central preset rules fetched;
  - `mozyo-bridge-agent` skill loaded;
  - active ticket and project notes confirmed;
  - any additional relevant rule or reference paths consulted.
- This audit trail is for reviewability. It does not require reading every reference file on every task.

System-specific entry points:

- **Redmine** (default for `mozyo_bridge`; preset `redmine-governed`): the durable work log is the Redmine issue and its journals. Gates such as Start / Implementation Done / Review Request / Review / Owner Close Approval / Close land as separate journal entries on the issue. The central preset (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-governed/agent-workflow.md`) defines the required fields per gate; this skill must not duplicate those tables.
- **Asana** (for Asana-preset repos): the durable work log is the Asana task and its comments / stories. Completion notes, audit comments, and follow-up scope changes land on the task itself. The Asana central preset (`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md`) is authoritative for that vocabulary.

## Local Documentation

- `AGENTS.md` and `CLAUDE.md` are routers.
- `vibes/docs/rules/` holds local working rules.
- `vibes/docs/specs/` holds project structure and specification notes.
- `vibes/docs/logics/` holds decision and release logic.
- `vibes/docs/temps/` holds reusable templates.

## Handoff Lifecycle

Use handoff only when the active project workflow or the user explicitly asks for another agent to participate.

1. The sender records or identifies the durable source of truth first.
2. The sender notifies the receiver through the high-level `mozyo-bridge` handoff primitive (`mozyo-bridge handoff send` / `mozyo-bridge handoff reply` / top-level alias `mozyo-bridge reply`). The primitive runs its own deterministic preflight; the caller does not assemble `mozyo-bridge read` + `mozyo-bridge message` shell choreography for normal handoff/reply. The `notify-*` wrappers (`notify-codex`, `notify-claude`, `notify-codex-review`, `notify-claude-review-result`) are compatibility entrypoints that route through the same primitive for standard Redmine-shaped notifications; `notify-*-legacy-task` remains a retired-queue cleanup wrapper only.
3. The receiver starts from the durable source of truth, not from pane text alone, and not from `mozyo-bridge status` / `doctor` / pane scrollback inference. Those surfaces are operator/debug aids; when a durable Asana / Redmine anchor is available, read the named task / comment / issue / journal.
4. The receiver records findings, blockers, completion notes, and verification in the durable source of truth.
5. The receiver sends a short result notification back to the sender through the same handoff primitive so the sender knows to read the durable record.
6. The sender resumes from the durable record and decides the next action.

Pane messages are notification edges in this lifecycle. They are not review passes, task completion, release approval, or the work log.

## Cross-Workspace Handoff

When the sender (Claude or Codex) needs to notify an agent that lives in another tmux session — for example, a different repo's workspace — the routing is constrained at the CLI as well as at the workflow level (Redmine #10332).

- Use `mozyo-bridge agents list` (optionally `--json`, `--session NAME`, `--agent claude|codex|unknown`) to enumerate the target workspace's sessions, windows, panes, processes, cwds, inferred repo roots, and agent kinds before naming a target. Discovery is read-only and is separate from `mozyo-bridge list` / `status`.
- Cross-session `mozyo-bridge handoff send --to claude` is rejected at the CLI with `blocked` / `cross_session_claude`. The origin agent must route through the target session's Codex window with `--to codex --target <target_session>:codex --mode standard` (or `--mode pending`) and ask that target Codex to perform the local Claude handoff. The reason: typing directly into a foreign workspace's Claude pane bypasses that workspace's audit boundary.
- Cross-session `--to codex` is the explicit gateway path, but only under `--mode standard` or `--mode pending`. The default `queue-enter` rail (since v0.4) rejects every cross-session target with `invalid_args` to keep its no-rollback contract bound to the sender's session. Omit `--mode` and the gateway send also fails; always pass an explicit mode for cross-workspace gateway sends.
- `--target-repo PATH` is an opt-in repo gate. When supplied, the target pane's cwd must walk up to that repo root or the handoff is rejected with `blocked` / `target_repo_mismatch`. Use it to harden against same-named sessions opened against different repos.
- The durable source of truth for the cross-workspace request stays on Redmine / Asana; the pane notification is still only the pointer. The target Codex reads the durable anchor and decides how to ingest the request in the target workspace.

The low-level `mozyo-bridge read`, `mozyo-bridge message`, `mozyo-bridge type`, and `mozyo-bridge keys` commands are operator/debug primitives (pane inspection, ad-hoc operator messages, raw typing, raw keys). They are not the standard handoff/reply path and must not be assembled by hand as a routine substitute for the primitive; the only sanctioned uses are the operator-driven `--no-submit` retry path in step 3 of the Retry Path Checklist (per-preset central rules) and explicit operator debugging.

A project-local rule that requires the sender to notify the receiver for every handoff of a given direction applies to every task in that scope, including audit-only, revalidation, and doc-only tasks; the "every" is not relaxed by how the task is framed, by the receiver's prior pickup-intent statement (for example "I will pull from the task record"), or by the sender's judgement that the receiver will read the durable record anyway. Skipping the notification on that basis is a sender-side rationalization, not a satisfied condition.

## Claude / Codex Role Boundary

- Claude owns implementation for normal development tasks.
- Codex does not directly implement normal development tasks in `mozyo_bridge`.
- Codex owns escalation handling, audit, user-facing clarification, and decisions that can be made from source of truth.
- When Codex receives a normal development task ID, the standard action is to convert it into a Claude handoff, not to implement it. Task size, urgency, implementation difficulty, user impatience, or the user writing directly into the Codex pane do not override this default.
- Imperative or request phrases from the user — for example "実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "go ahead", "please do it" — are not by themselves authorization for Codex to perform a direct edit. They express "I want this done", not "you may bypass Claude".
- The standard handoff is overridden only by an explicit Codex-direct-edit exception defined in the Policy / Skill Authoring Boundary section.
- When Codex receives a workflow-change verification task, Codex selects a valid normal development task, records the selection in the active ticket system (Redmine journal for `mozyo_bridge`; Asana comment for Asana-preset repos), and hands it off to Claude.
- The verification task only counts if Claude performs the normal development work and Codex performs the audit path.
- If Codex mistakenly implements a normal development task directly, that run does not count as the task's normal completion. If it occurred during a verification task, it also does not satisfy workflow-change verification.
- After such a mistake, reopen the affected ticket, record the mistake, the impact scope, and the follow-up decision (adopt, discard, reimplement) in the active ticket system as a correction (Redmine correction journal or Asana correction comment), then rerun the flow from Claude implementation through Codex audit. This correction flow applies to every normal development task, not only verification-target tasks.

## Policy / Skill Authoring Boundary

- For autonomous workflow, rules, skills, handoff, audit, or release/distribution gate changes, Codex owns policy framing, draft wording, user-facing clarification, and audit.
- Claude is the default implementer for repository file edits to those policies and skill references.
- Codex must not directly edit and commit policy or skill reference files during ordinary operation. The protected scope covers both implementation files (`src/**`, `tests/**`, `docs/**`, `vibes/docs/**`, `README.md`, release workflow, CLI behavior) AND the guardrail / docs / catalog surfaces (`AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, `.mozyo-bridge/docs/catalog.yaml`, `.codex/skills/**`, `.claude/skills/**`, `skills/mozyo-bridge-agent/**`, `plugins/mozyo-bridge-agent/**`, scaffold packaged preset / router templates under `src/mozyo_bridge/scaffold/presets/**`). A chat-level "ユーザーがガードレール変更を明示" is NOT by itself authorization for Codex to bypass Claude on these surfaces.
- A Codex direct edit is permitted only when one of the following narrow exceptions applies. Operate the exception conservatively; when in doubt, or when the user instruction admits more than one reading, fall back to the default and produce a Claude handoff.
  1. The user explicitly authorized a Codex direct edit using wording equivalent to `Codex direct edit`, "Codex が直接編集してよい", or "Codex に直接実装させてよい", scoped to a specific task or file. Generic imperative or request forms ("実行せよ", "対応して", "やって", "お願いします", "進めて", "implement it", "please do it") do not qualify.
  2. The change is the minimal record-keeping correction needed to capture an existing mistaken implementation, mistaken commit, or mistaken procedure in the active ticket system (Asana task / Redmine issue) or the repo.
  3. The change is a genuinely urgent small fix that would be damaged by handoff (for example, a one- or few-line fix needed within minutes to halt an in-progress release, publish, or CI run). Before invoking this exception Codex must stop implementation, record an "urgent direct-edit request" in the active ticket system (Redmine journal for `mozyo_bridge`; Asana comment for Asana-preset repos) with the situation, target files, intended change, and impact scope, and obtain user confirmation when possible. Do not apply this exception when the situation is ambiguous or when confirmation cannot be obtained.
- When Codex makes a direct edit under an exception, the durable record is **system-specific** and must exist BEFORE the edit lands:
  - Asana projects: record `Codex direct edit` in an Asana comment on the task with (a) which exception applied, (b) the verbatim or quoted user instruction, (c) the changed files, (d) the verification performed, and (e) whether follow-up verification is required.
  - Redmine projects (including the `mozyo_bridge` repo, which uses the `redmine-governed` preset): create a Redmine `codex_direct_edit` gate journal on the active issue with `role: 実装者`, `direct_edit: true`, `allowed_paths` (list every path Codex will touch), `reason`, and `follow_up_review`. The journal must exist on the issue before any Codex edit. A direct edit without the gate journal is itself a violation subject to correction.
- `.mozyo-bridge/docs/file_conventions.generated.yaml` and other catalog generator outputs are generator-only artifacts. Neither Claude nor Codex hand-edits them. Change `.mozyo-bridge/docs/catalog.yaml` and regenerate with `mozyo-bridge docs generate-file-conventions`; verify with `--check`.
- A direct edit missing any of these fields is itself subject to a follow-up correction. Past incident pattern: Codex-created repo diffs without a prior `codex_direct_edit` gate journal, or without a Review Gate-approved audit-owned commit path, must be recorded with correction journals and routed back through the governed implementation/review flow.
- A Codex direct edit to autonomous workflow or role boundaries does not waive the workflow-change verification requirement.

### Repo-Local Guardrail Autonomous Lane (mozyo-bridge product-wide policy)

The `redmine-governed` and `redmine-rails-governed` presets distribute a **Repo-Local Guardrail Autonomous Lane** that carves a narrow set of repo-local paths out of the `codex_direct_edit` gate. The lane is opt-in by virtue of the preset; any project that scaffolds with `mozyo-bridge scaffold apply redmine-governed` (or the Rails variant) inherits it. Default lane paths are `vibes/docs/rules/**`, `vibes/docs/logics/**`, `vibes/docs/specs/**`, and `.mozyo-bridge/docs/catalog.yaml`. Project-local additions may extend or restrict the lane but must not extend it to distributed / runtime / implementation surfaces (`AGENTS.md`, `CLAUDE.md`, `.mozyo-bridge/rules/**`, `.codex/skills/**`, `.claude/skills/**`, `skills/**`, `plugins/**`, `src/**`, `tests/**`, scaffold preset templates, generator outputs).

- Inside the lane, Codex may edit directly without a pre-edit `codex_direct_edit` gate journal. Generic imperative phrasing from the user is not the trigger; the lane is enabled by the preset, not by chat.
- Codex records a `codex_autonomous_edit` journal on the active Redmine issue with `lane: autonomous`, `changed_paths`, `intent`, `verification`, `commit_hash` (or `pending: staged-not-committed` until the commit lands), and `follow_up_review_required`. The journal may be written at the same time as the commit or immediately after; pre-approval is not required.
- Verification commands required before commit: `mozyo-bridge docs validate --repo .`, `mozyo-bridge docs validate --check-file-coverage --repo .`, `git diff --check`. When `.mozyo-bridge/docs/catalog.yaml` is touched, additionally `mozyo-bridge docs generate-file-conventions --check --repo .` and `mozyo-bridge docs audit-impact --all-changed --check-generated --repo .`. Drift triggers `mozyo-bridge docs generate-file-conventions --repo .` and re-commit.
- The lane is not entered when the change crosses into a non-lane path, touches credentials / authentication, contradicts prior product-owner instructions, or revisits a path that previously received `要修正` / `block` review on the same issue. In any of those cases Codex falls back to a Claude handoff or the standard `codex_direct_edit` gate.
- A lane commit without the `codex_autonomous_edit` journal is treated as a record-keeping correction (follow-up journal required); the change itself is not reverted on that basis alone.
- The lane policy itself is a workflow / guardrail change; any modification to the lane definition triggers the standard `Workflow Change Verification` flow.

## Audit-Owned Commit Authority

The default `mozyo_bridge` role split has Claude implement and Codex audit. After the durable audit record is captured (an audit / review comment on the Asana task, or a Review Gate journal on the Redmine issue), Codex is authorized to stage and commit *only the audit-approved diff*. This is a commit authority, not an implementation authority. The two are distinct boundaries:

- **Codex direct implementation edit** — restricted to the narrow exceptions in `Policy / Skill Authoring Boundary`. Producing new diffs.
- **Codex audit-owned commit** — allowed after the audit record exists. Committing diffs that Claude already produced and that the audit record approved.

Audit-owned commit does not waive the implementer / auditor boundary. Codex must not edit implementation files in order to "fix up" an audit-approved diff during staging. If the diff needs changes, that is a new implementation iteration that goes back to Claude.

Before an audit-owned commit, Codex must:

1. Confirm the durable audit record exists — an audit / review comment on the Asana task, or a Review Gate journal on the Redmine issue. The commit cannot land before this record exists.
2. Run `git status` and reconcile the dirty set against the implementation actor's recorded changed-paths list. If scope-outside dirty files exist, stash them, route them through a separately scoped task, or leave them untouched — never bundle them into the audit-owned commit.
3. Stage only the audit-approved paths. Avoid `git add -A` and `git add .` whenever the worktree carries anything beyond the approved diff.
4. Run `git diff --cached --stat`, and `git diff --cached` when content review is required. Reconcile the staged set with the implementation comment line by line.
5. Commit with a message that carries the per-system ticket reference defined in the project's central preset:
   - Asana projects: `Refs: Asana task <task_id>` plus `Audit: Asana comment <comment_id>` (the durable comment / story id of the approval).
   - Redmine projects: `Refs: Redmine #<issue_id>` plus `Journal: <journal_id>` (the Review Gate journal id).
6. Record the commit hash in the durable source of truth: a follow-up Asana comment on the same task, or a Close Gate / Progress Log journal on the Redmine issue. The hash must live in the durable record, not only in pane chat.
7. Mark the task complete or move the issue to closed only after both the audit record and the commit-hash record are present. Implementation done alone, or a commit landed without a recorded hash, is not completion.

On systems where the central preset distinguishes review approval from owner close approval (Redmine projects: see the Redmine preset's `Close Approval Separation`), both the review approval and the owner close approval must be recorded as separate durable journals before closing. Review approval alone is not close approval; the implementer must wait for the owner close approval journal before advancing to close.

This authority applies to normal development tasks and to guardrail / rule / workflow tasks alike whenever the project splits implementation and audit actors. It does not waive the `Workflow Change Verification` requirement for changes to autonomous workflow, skills, rules, or release / distribution gates — verification of a rule change is still a separate normal development task with the standard handoff.

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
- Record the verification result in the active ticket system (Redmine journal
  for `mozyo_bridge`; Asana comment for Asana-preset repos) and create
  follow-up tickets there for any gaps.
