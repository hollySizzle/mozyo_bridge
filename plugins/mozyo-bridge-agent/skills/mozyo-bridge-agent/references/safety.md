# Safety Reference

## Secret Handling

- Do not commit or paste real PyPI/TestPyPI tokens, API keys, personal credentials, or personal information.
- `.env`, `.env.*`, and `.pypirc` are local-only secret surfaces and must stay ignored.
- Do not store secrets in Asana task descriptions, Notion rules, Notion knowledge pages, or repository docs.

## Notification Safety

- `mozyo-bridge` is a notification transport.
- It is not the source of truth for review state, task completion, or release approval.
- The receiving agent must check Asana or the named source of truth before acting.
- Two send rails exist; pick the right one rather than weakening either:
  - `--mode queue-enter` (v0.4 normative default for agent pane handoff — `mozyo-bridge handoff send` / `handoff reply` / `notify-*` standard variants targeting Claude / Codex panes; rejects `--force`): a deterministic Layer B preflight runs before any typing; if any of the following checks fails, the CLI dies with `blocked` and the corresponding `Reason` before `send-keys -l` is issued:
    - explicit `--target` must live in the receiver's tmux window (`Reason: invalid_args`),
    - target pane must live in the **sender's** tmux session, i.e. invoke from inside the same tmux session as the receiver (`Reason: invalid_args`),
    - target pane must be the **active split** of its window (`Reason: invalid_args`),
    - foreground process must match the receiver's allowlist (`Reason: target_not_agent`): strong identity for literal `claude` (receiver=`claude`) and literal `codex` (receiver=`codex`); weak identity for literal `node` and versioned native binary basenames, which both Claude Code and Codex CLI legitimately use — Step 9 (window-name binding) plus operator discipline carry cross-binding protection in the weak case.
    When all checks pass, Enter is issued regardless of whether the landing marker was observed. Outcome is `sent` / `ok` when the marker was observed and `sent` / `queue_enter` when it was not — both are practical queued submission, not confirmed landing, so receivers still read the durable Asana task comment / Redmine journal as the source of truth. v0.4 default flip does not weaken the preflight; out-of-scope targets (`mozyo-bridge message`, non-agent panes) do not enter this rail.
  - `--mode standard` (strict explicit fallback, preserved from v0.1; `mozyo-bridge message --submit` default behavior): strict marker-observed Enter. Marker miss is fail-closed — input is cleared via `C-u`, Enter is not sent, and the outcome is `blocked` / `marker_timeout`. Select this rail explicitly when strict landing observation is required (regression check after a queue-enter rail change, brand-new pane where queue-pickup probability is unverified, observability test, audit requirement that demands strict landing evidence) or when the target falls outside the v0.4 default scope. Behavior is unchanged from v0.1 and not relaxed by the default flip; record the reason for selecting it in the durable record so an auditor can replay why the default was overridden.
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

Do not keep polling or watching the target pane as standard practice. After the handoff is delivered, rely on the durable source of truth named in the request, normally Asana task comments, repository changes, or an explicit completion notification from the target agent.

When a project's central preset or rule mandates a sender notification for every handoff of a given direction, that requirement is not relaxed by audit-only, revalidation, or doc-only framing of the task, nor by the receiver's prior pickup-intent statement; the sender must attempt the notification on every such handoff. Recording an "un-notified" state without first attempting the standard-path notification is a sender-side rationalization, not a satisfied fallback condition.

## Result Notification Boundary

Writing the durable result is necessary but not always sufficient for a handoff. When work or audit began from another agent's request, notify that sender after recording the result so they know to read the durable source of truth.

The return notification should be short and should point back to the durable record. Do not use the return notification as a substitute for the Asana comment, Redmine journal, repository change, or other named source of truth.

## Release Safety

- Prefer GitHub Actions OIDC Trusted Publishing.
- Do not make local token upload the standard production PyPI route.
- Confirm CI and TestPyPI install before production release.
