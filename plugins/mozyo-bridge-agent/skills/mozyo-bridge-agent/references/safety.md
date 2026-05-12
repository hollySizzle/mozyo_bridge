# Safety Reference

## Secret Handling

- Do not commit or paste real PyPI/TestPyPI tokens, API keys, personal credentials, or personal information.
- `.env`, `.env.*`, and `.pypirc` are local-only secret surfaces and must stay ignored.
- Do not store secrets in Asana task descriptions, Notion rules, Notion knowledge pages, or repository docs.

## Notification Safety

- `mozyo-bridge` is a notification transport.
- It is not the source of truth for review state, task completion, or release approval.
- The receiving agent must check Asana or the named source of truth before acting.
- Preserve marker-based safety behavior. Enter must not be sent before the marker is observed.
- `.agent_handoff/tasks.json` is a retired queue cleanup surface, not a standard notification fallback.

## Sender Handoff Boundary

When asking another agent to work through `mozyo-bridge`, the sending agent should only verify the handoff itself:

- Confirm the target pane is the intended agent and repository context.
- Send the operator message.
- Submit the message when the delivery guard allows it.
- Optionally read the pane once immediately after delivery to catch obvious blockers such as a missing skill, missing Notion MCP access, or an unsubmitted prompt.

Do not keep polling or watching the target pane as standard practice. After the handoff is delivered, rely on the durable source of truth named in the request, normally Asana task comments, repository changes, or an explicit completion notification from the target agent.

## Result Notification Boundary

Writing the durable result is necessary but not always sufficient for a handoff. When work or audit began from another agent's request, notify that sender after recording the result so they know to read the durable source of truth.

The return notification should be short and should point back to the durable record. Do not use the return notification as a substitute for the Asana comment, Redmine journal, repository change, or other named source of truth.

## Release Safety

- Prefer GitHub Actions OIDC Trusted Publishing.
- Do not make local token upload the standard production PyPI route.
- Confirm CI and TestPyPI install before production release.
