# Safety Reference

## Secret Handling

- Do not commit or paste real PyPI/TestPyPI tokens, API keys, personal credentials, or personal information.
- `.env`, `.env.*`, and `.pypirc` are local-only secret surfaces and must stay ignored.
- Do not store secrets in any ticket-system entries (Asana task descriptions / comments, Redmine issue descriptions / journals), preset rule docs, knowledge base pages, or repository docs.

## Notification Safety

- `mozyo-bridge` is a notification transport.
- It is not the source of truth for review state, task completion, or release approval.
- The receiving agent must check Asana or the named source of truth before acting. Do not infer receiver state, task state, or gate state from `mozyo-bridge status` output, `mozyo-bridge doctor` output, or pane scrollback when a durable Asana / Redmine anchor is available; those surfaces are operator/debug aids, not the durable record. Read the named task / comment / issue / journal instead.
- The standard handoff/reply path is the high-level primitive: `mozyo-bridge handoff send` / `mozyo-bridge handoff reply` / top-level alias `mozyo-bridge reply`. The primitive resolves the receiver pane, runs the deterministic Layer B preflight, types the marker-prefixed notification, and either presses Enter (queue-enter / standard rails) or leaves it pending (`--mode pending`). The caller does not assemble `mozyo-bridge read` + `mozyo-bridge message` shell choreography for normal handoff/reply.
- The `notify-*` wrappers (`notify-codex`, `notify-claude`, `notify-codex-review`, `notify-claude-review-result`) are compatibility entrypoints that route internally through the same primitive for standard Redmine-shaped notifications and keep the same safety rails. `notify-*-legacy-task` is a retired-queue cleanup wrapper only and is not the standard path.
- The low-level `mozyo-bridge read`, `mozyo-bridge message`, `mozyo-bridge type`, and `mozyo-bridge keys` commands are operator/debug primitives (pane inspection, ad-hoc operator messages, raw typing, raw keys). They are not the standard handoff/reply path; do not assemble them by hand as a routine substitute for the primitive. The only sanctioned uses are the `--no-submit` operator/debug fallback in the per-preset Retry Path Checklist and explicit operator debugging.
- Raw tmux pane mutation is a prohibited agent operation for workflow delivery or recovery. An agent does not drive an agent pane with `tmux send-keys`, `tmux paste-buffer`, a direct Enter / `C-u`, or any other raw key injection; does not use low-level `mozyo-bridge type` / `mozyo-bridge keys` as a handoff substitute; and does not run `mozyo-bridge message --no-submit` and then issue a raw Enter itself to submit. Read-only inspection (`tmux capture-pane`, `mozyo-bridge read`) is allowed.
- Raw tmux mutation is operator/debug-only. It is permitted only when an explicit operator instruction at the moment of use is paired with a durable record (Redmine journal / Asana comment) of that instruction; an agent never performs it on its own judgement.
- When `mozyo-bridge handoff send` / `handoff reply` fails (for example `marker_timeout`), the agent records the `un-notified` state in the durable record and leaves the receiver to be reached through an approved high-level route. It does not self-repair the delivery by mutating the receiver pane.
- Two send rails exist; pick the right one rather than weakening either:
  - `--mode queue-enter` (v0.4 normative default for agent pane handoff — `mozyo-bridge handoff send` / `handoff reply` / `notify-*` standard variants targeting Claude / Codex panes; rejects `--force`): a deterministic Layer B preflight runs before any typing; if any of the following checks fails, the CLI dies with `blocked` and the corresponding `Reason` before `send-keys -l` is issued:
    - explicit `--target` must live in the receiver's tmux window (`Reason: invalid_args`),
    - target pane must live in the **sender's** tmux session, i.e. invoke from inside the same tmux session as the receiver (`Reason: invalid_args`),
    - target pane must be the **active split** of its window, **or** pass standard_target_admission (Redmine #12597): a registered inactive split that satisfies the minimal admission contract (live pane / strong role match / `workspace_id` present / unambiguous) is activated via `tmux select-pane` and delivered to — pane selection only, never raw `send-keys` / `paste-buffer` / low-level `type` / `keys` recovery — with the activation recorded in the durable record. An inactive split that is not admitted (e.g. no `workspace_id`), or `--no-target-activation`, stays fail-closed with `Reason: invalid_args` and the strict-rail recovery command,
    - foreground process must match the receiver's allowlist (`Reason: target_not_agent`): strong identity for literal `claude` (receiver=`claude`) and literal `codex` (receiver=`codex`); weak identity for literal `node` and versioned native binary basenames, which both Claude Code and Codex CLI legitimately use — Step 9 (window-name binding) plus operator discipline carry cross-binding protection in the weak case.
    When all checks pass, Enter is issued regardless of whether the landing marker was observed. Outcome is `sent` / `ok` when the marker was observed and `sent` / `queue_enter` when it was not — both are practical queued submission, not confirmed landing, so receivers still read the durable Asana task comment / Redmine journal as the source of truth. v0.4 default flip does not weaken the preflight; out-of-scope targets (`mozyo-bridge message`, non-agent panes) do not enter this rail.
  - `--mode standard` (strict explicit fallback, preserved from v0.1; `mozyo-bridge message --submit` default behavior): strict marker-observed Enter. Marker miss is fail-closed — a `C-u` rollback is issued, Enter is not sent, and the outcome is `blocked` / `marker_timeout` (the sender does not verify from tmux capture that the receiver composer was cleared). Select this rail explicitly when strict landing observation is required (regression check after a queue-enter rail change, brand-new pane where queue-pickup probability is unverified, observability test, audit requirement that demands strict landing evidence) or when the target falls outside the v0.4 default scope. Behavior is unchanged from v0.1 and not relaxed by the default flip; record the reason for selecting it in the durable record so an auditor can replay why the default was overridden.
- Whichever rail is used, the durable record (Asana task comment / Redmine journal) is still the source of truth. The pane notification is a pointer.
- `.agent_handoff/tasks.json` is a retired queue cleanup surface, not a standard notification fallback.

## Tool Error Parsing

When a `mozyo-bridge` command (or any other tool) dies with an `error: ...` message, parse the literal text of that error first, before pattern-matching the failure shape against past similar-looking errors. The literal text is the authoritative next-step source; a remembered "this kind of error is usually fatal / escape via X" pattern from prior sessions does not override it.

- If the error contains a literal next-action verb such as `read target again`, `retry`, `refresh`, `re-run`, or an explicit `Retry path:` / `Fallback path:` hint, follow that verb verbatim before considering any higher fallback. The verb is the authoritative next step.
- Only after the literal next-action verb has been followed and produced a fresh failure, or after the latest error demonstrably contains no next-action verb at all, may the agent escalate to a higher fallback. Skipping the literal verb in favor of a remembered escape path is a known regression mode (Asana task 1214779823377861).
- `mozyo-bridge message --no-submit` and `mozyo-bridge handoff send` marker-gate failures emit `hint:` trailers on stderr that name both the retry path and the per-preset `--no-submit` retry budget. Those trailers are part of the contract — read them and act on them, do not treat them as decoration.
- The `--no-submit` retry budget (cap 3) and the standard `handoff send` retry pool are **separate budgets**. Do not borrow attempts across them when judging whether the preset's `Notification fails` branch may fire.

## Sender Handoff Boundary

When asking another agent to work through `mozyo-bridge`, the sending agent should only verify the handoff itself:

- Confirm the target pane is the intended agent and repository context.
- Send the operator message.
- Submit the message when the delivery guard allows it. Default for agent pane handoff is `--mode queue-enter` (v0.4 normative default); use `--mode standard` only when strict landing observation is required (regression check, brand-new pane, observability test, strict-landing audit requirement) or when the target is outside the v0.4 default scope (`mozyo-bridge message`, non-agent pane).
- Optionally read the pane once immediately after delivery to catch obvious blockers such as a missing skill, missing Notion MCP access, or an unsubmitted prompt.

Do not keep polling or watching the target pane as standard practice. After the handoff is delivered, rely on the durable source of truth named in the request, normally Redmine journals (for `mozyo_bridge` and other Redmine-preset repos) or Asana task comments (for Asana-preset repos), repository changes, or an explicit completion notification from the target agent.

When the operator names the target by natural language ("人形使いへ返して", "あっちの Claude に渡して") rather than a pane id, resolve it through compact target discovery before sending. `mozyo-bridge agents targets` lists candidates; it does not select. Confirm a unique candidate against its `workspace` / `lane` / `role` / `pane_id` / `repo_short` columns, then send to the explicit `pane_id` with `--target-repo auto` (or an explicit repo root) — never trust the pane title alone for identity. If the natural name matches zero or more than one candidate, fail closed: present the candidates or ask the owner, do not silently pick one. This resolution convenience does not relax any boundary below — in particular cross-session `--to claude` stays rejected and must route through the target session's Codex gateway. The same gateway rule extends to lane boundaries inside one physical session: a handoff that crosses from a coordinator lane into a target lane (for example a cockpit session hosting several lanes) routes through the **target lane's Codex** pane rather than going straight to a uniquely-resolved Claude pane in that lane; direct Claude delivery is reserved for same-lane addressing. The full procedure lives in `references/workflow.md` under `## Natural-Name Target Handoff`.

When a project's central preset or rule mandates a sender notification for every handoff of a given direction, that requirement is not relaxed by audit-only, revalidation, or doc-only framing of the task, nor by the receiver's prior pickup-intent statement; the sender must attempt the notification on every such handoff. Recording an "un-notified" state without first attempting the standard-path notification is a sender-side rationalization, not a satisfied fallback condition.

## Result Notification Boundary

Writing the durable result is necessary but not always sufficient for a handoff. When work or audit began from another agent's request, notify that sender after recording the result so they know to read the durable source of truth.

The return notification should be short and should point back to the durable record. Do not use the return notification as a substitute for the Redmine journal (default for `mozyo_bridge`), Asana comment (for Asana-preset repos), repository change, or other named source of truth.

In a coordinator / sublane operating model (a main Codex coordinator lane dispatching work into implementation sublanes), this return boundary widens into a standing rule: when a sublane reaches any handoff-worthy state — blocked, implementation_done, review_request, review result, commit recorded, or owner close approval requested — it sends a concise callback to the coordinator lane's Codex with the durable anchor, so the work does not appear stalled from the coordinator cockpit. The callback crosses a lane boundary, so it goes Codex-to-Codex (never straight to another lane's Claude) even inside one physical cockpit session, and it stays a pointer to the Redmine journal, not a copy of it. The full procedure lives in `references/workflow.md` under `## Sublane Coordinator Callback`.

## Release Safety

- Prefer GitHub Actions OIDC Trusted Publishing.
- Do not make local token upload the standard production PyPI route.
- Confirm CI and TestPyPI install before production release.
