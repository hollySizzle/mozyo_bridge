# Release Distribution Rules

## Scope

This document defines mandatory gates for distributing `mozyo-bridge` artifacts.
Detailed command examples and rationale may live in `vibes/docs/logics/`, but the
completion criteria for beta and production distribution live here.

## Internal Beta Distribution

- Internal beta distribution uses TestPyPI, not production PyPI.
- Before calling an internal beta ready, install the package from TestPyPI with
  the same command given to beta testers.
- The verification install must not use a local checkout, editable install, or
  local wheel as a substitute for the tester path.
- Use `pipx` with the pip backend for TestPyPI verification so the tested route
  matches the documented beta command.
- Confirm both command entry points start: `mozyo-bridge --help` and
  `mozyo --help`.
- Confirm distributed scaffold/rule content that is material to the change is
  present inside the installed package.
- A stable-looking version such as `0.1.4` on TestPyPI is still an internal beta
  artifact unless production PyPI publish has explicitly been requested and
  completed.

## Production Distribution

- Production PyPI distribution is a separate gate from internal beta.
- Do not create a GitHub Release for internal beta distribution.
- Creating a published GitHub Release triggers `.github/workflows/publish.yml`
  and must be treated as a production publish action.
- Production publish must be explicitly requested or approved before creating a
  GitHub Release.
- Production PyPI publish must use GitHub Actions Trusted Publishing, not local
  token upload, unless a separate emergency procedure is explicitly approved.
