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
- Internal beta publication must not require promoting public history first.
  The manual TestPyPI dispatch builds an exact reviewed candidate `source_sha`
  from a `main`-fixed workflow and does NOT require an `origin/main` push or a
  Redmine Version close as a precondition (Redmine #13601). This breaks the
  `Version close -> origin/main -> TestPyPI -> #13528/#13527 -> Version close`
  cycle: the `main`-only publication checkpoint still gates public-history
  promotion, but internal beta distribution is decoupled from it.
- The manual dispatch is gated fail-closed on the exact candidate: the 40-hex
  `source_sha`, its `expected_version` mirror match, a candidate
  `.github/workflows/test.yml` byte-identical to trusted `origin/main` (so a
  candidate cannot weaken its own Test workflow to fake a green run), a
  successful `Test` CI run for that SHA, an unused TestPyPI version (a payload
  lacking the `releases` object or an unreachable lookup fails closed), and an
  origin `source_ref` that resolves to exactly one named ref whose tip is the
  SHA. Trusted Publishing credentials (`id-token: write` + `environment:
  testpypi`) live only in the artifact-download+publish job, separate from
  checkout/build/verify.
- Order the internal-beta steps as #13528 (TestPyPI publish) then #13527 (exact
  install QA); the install QA runs against the published exact version, not a
  floating `main` install.

## Production Distribution

- Production PyPI distribution is a separate gate from internal beta.
- Do not create a GitHub Release for internal beta distribution.
- Creating a published GitHub Release triggers `.github/workflows/publish.yml`
  and must be treated as a production publish action.
- Production publish must be explicitly requested or approved before creating a
  GitHub Release.
- Production PyPI publish must use GitHub Actions Trusted Publishing, not local
  token upload, unless a separate emergency procedure is explicitly approved.
