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
  - `--mode standard` (default for `handoff send` / `notify-*` / `mozyo-bridge message --submit`): strict marker-observed Enter. Marker miss is fail-closed — input is cleared via `C-u`, Enter is not sent, and the outcome is `blocked` / `marker_timeout`. Do not send blind Enter to recover; record the failure in the durable record and let the receiver read the anchor manually.
  - `--mode queue-enter` (opt-in, `mozyo-bridge handoff send` only): Claude / Codex agent panes only; rejects `--force`; rejects an explicit `--target` whose tmux window is not the receiver's window. Marker miss does NOT roll back — Enter is sent and the outcome is `sent` / `queue_enter` (a distinct durable wording, not a silent strict success). Use this rail only when the receiver TUI is known to wrap-shape the marker (currently codex TUI; tracked under Asana `1214749106025548` / `1214765093829972`).
- Whichever rail is used, the durable record (Asana task comment / Redmine journal) is still the source of truth. The pane notification is a pointer.
- `.agent_handoff/tasks.json` is a retired queue cleanup surface, not a standard notification fallback.

## Sender Handoff Boundary

When asking another agent to work through `mozyo-bridge`, the sending agent should only verify the handoff itself:

- Confirm the target pane is the intended agent and repository context.
- Send the operator message.
- Submit the message when the delivery guard allows it. Default is the strict `--mode standard` rail; switch to `--mode queue-enter` only when the receiver TUI is known to wrap-shape the marker and the target is the receiver's own agent pane.
- Optionally read the pane once immediately after delivery to catch obvious blockers such as a missing skill, missing Notion MCP access, or an unsubmitted prompt.

Do not keep polling or watching the target pane as standard practice. After the handoff is delivered, rely on the durable source of truth named in the request, normally Asana task comments, repository changes, or an explicit completion notification from the target agent.

## Result Notification Boundary

Writing the durable result is necessary but not always sufficient for a handoff. When work or audit began from another agent's request, notify that sender after recording the result so they know to read the durable source of truth.

The return notification should be short and should point back to the durable record. Do not use the return notification as a substitute for the Asana comment, Redmine journal, repository change, or other named source of truth.

## Release Safety

- Prefer GitHub Actions OIDC Trusted Publishing.
- Do not make local token upload the standard production PyPI route.
- Confirm CI and TestPyPI install before production release.
