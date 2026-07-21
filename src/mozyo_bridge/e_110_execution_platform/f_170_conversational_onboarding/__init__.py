"""execution_platform Feature package `f_170_conversational_onboarding` (#13498).

Deterministic onboarding tools for non-engineer conversational adoption. The LLM
only translates natural language into closed tool calls; every filesystem /
config / scaffold / workspace / runtime mutation is a deterministic tool. Design
source of truth: ``vibes/docs/specs/conversational-onboarding-tool-contract.md``.

The first landed, independently-reviewed piece (#13508 task-level exception) is
the pure model-pre-launch path-safety classifier in ``domain.path_safety``. The
orchestrator surface (preflight assembly, closed ``OnboardingIntent`` schema,
drift-bound plan, apply/resume runner, typed write-once config, credential-free
receipt) lands after that classifier review, so it can wire to a reviewed base.
"""
