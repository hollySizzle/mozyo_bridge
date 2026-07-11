"""Application layer for conversational onboarding (#13498).

IO / orchestration that drives the pure domain: filesystem probing for the
preflight, the herdr binary resolution from the trusted environment, the typed
write-once config tool, and the apply/resume runner with the root-scoped lock.
"""
