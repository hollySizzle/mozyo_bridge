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

## Release Safety

- Prefer GitHub Actions OIDC Trusted Publishing.
- Do not make local token upload the standard production PyPI route.
- Confirm CI and TestPyPI install before production release.
