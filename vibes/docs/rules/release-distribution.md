# Release Distribution Rules

## Scope

This document defines mandatory gates for distributing `mozyo-bridge` artifacts.
Detailed command examples and rationale may live in `vibes/docs/logics/`, but the
completion criteria for beta and production distribution live here.

## Versioning Policy

- Package version follows semantic versioning. The segment meaning is fixed:
  - **patch** (`x.y.Z`): backward-compatible fixes that change no contract
    (CLI surface / API / preset).
  - **minor** (`x.Y.0`): feature additions or backend capability, backward
    compatible.
  - **major** (`X.0.0`): breaking contract changes to an existing CLI / API /
    preset contract.
- The next feature release enters the `0.10` line (a minor bump for feature
  work).
- A Redmine Version (e.g. `#308`) is a roadmap bucket, not a package version and
  not a release authority. Roadmap grouping never fixes the shipped package
  version; assigning an issue to a Redmine Version does not commit a specific
  package version.
- Feature work such as the herdr adapter is a minor-release candidate, but the
  actual package version is decided only at the release gate. Neither the
  roadmap nor this policy document may pre-fix a version number.
- Codifying this policy performs no version bump, tag, TestPyPI / PyPI
  publication, or GitHub Release. Version decisions, bumps, and publishes happen
  only at the release gates defined by the Internal Beta and Production
  Distribution sections below.

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
