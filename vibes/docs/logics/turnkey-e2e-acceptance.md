# Turnkey E2E Acceptance

## Purpose

UserStory: an end user installs the `mozyo-bridge` package, scaffolds project routers, and runs Claude / Codex autonomous ticket-ID handoffs with no other repo-local setup. This document is the final acceptance test for that goal.

The acceptance target is **recovery from no repo-local router state**, using only artifacts a fresh user actually receives at a single pinned release: a TestPyPI / PyPI install plus the matching install scripts and skill tree from the same `hollySizzle/mozyo_bridge` git ref (release tag `v<X.Y.Z>` for tagged releases, or the recorded commit SHA for tagless ones). Mixing artifacts across refs — for example installing `mozyo-bridge==X.Y.Z` but fetching install scripts from `main` — is not a valid acceptance run. The `Install the published package` section below shows the pinned form. It is not a release publish, not a runtime feature change, and not the Enter-send fix.

## What this is NOT

- **Not** the `Beta Tester Install` smoke in `README.md`. That smoke runs against isolated `./tmp/mb-smoke-*` targets and protects this repository's tracked routers. The turnkey E2E acceptance below intentionally deletes this repository's own root `AGENTS.md` / `CLAUDE.md` to verify recovery from a no-router state.
- **Not** a TestPyPI / PyPI publish. Publish gating lives in `release-flow.md` and must already be cleared by the release owner before this acceptance test runs.
- **Not** a runtime feature change. If a step here is impossible with the current CLI, stop and file a follow-up task. Do not edit runtime behavior to make this doc executable.
- **Not** the Enter-send fix. The acceptance test uses the Enter-send issue (Asana task `1214673801774740`) as the subject ticket-ID handed to the autonomous agents, but resolving that bug is a separate task whose outcome the acceptance test observes.

## Responsibility split

| Doc | Concern | Destructive? |
| --- | --- | --- |
| `README.md` `Beta Tester Install (GitHub main)` | install + per-preset scaffold smoke in `./tmp/mb-smoke-*` | no — tracked routers stay intact |
| `vibes/docs/logics/scaffold-rules.md` `Beta Tester Verification` | `rules status` vs `scaffold status` responsibilities; doctor as bundle | no |
| `vibes/docs/logics/skill-distribution.md` `Beta Tester Verification` | Codex / Claude skill install paths and precedence | no |
| `vibes/docs/logics/release-flow.md` GA / patch / pre-release | TestPyPI / PyPI publish gates | no — publish is owner gated |
| **this doc** | final acceptance from no-router state using package artifacts | **yes — deletes root routers on a clean worktree** |

The first three docs verify "can I install and scaffold?" The publish doc gates "can I publish?" This doc verifies "if I install the published package and have no router state, can the autonomous handoff still come back online?"

## Prerequisites (read before starting)

The destructive step below removes the tracked root routers. Run the test only when **all** of the following hold:

1. `git rev-parse --is-inside-work-tree` returns `true`.
2. `git rev-parse --show-toplevel` matches the expected repository root (e.g. the `mozyo_bridge` clone).
3. `git status --porcelain` is empty. No untracked files, no modified files, no staged changes.
4. The current HEAD is on a known commit (tag or `main`), reachable from `origin` so `git checkout HEAD -- ...` actually restores known content.
5. Both `AGENTS.md` and `CLAUDE.md` exist at the repo root and are tracked.
6. The TestPyPI or production PyPI release under test has finished publishing per `release-flow.md`.

If any check fails, abort. Do not improvise — fix the prerequisite first or run the non-destructive `README.md` smoke instead.

## Install the published package

Use the artifact under test, not local source. The point of this acceptance test is that the installed package alone can rehydrate the autonomous handoff.

```bash
# Production PyPI:
pipx install --force mozyo-bridge==<X.Y.Z>

# Or TestPyPI (per release-flow.md):
pipx install --force --backend pip \
  --index-url https://test.pypi.org/simple/ \
  --pip-args "--extra-index-url https://pypi.org/simple/" \
  mozyo-bridge==<X.Y.Z[aN]>
```

`mozyo-bridge --version` alone cannot tell PyPI from GitHub `main` (same `pyproject.toml` string). The acceptance test does not depend on the version string; it depends on the installed CLI surface, which the doctor will check.

Install user-global rules and skills. Because this acceptance test verifies recovery from a specific release artifact, the skill source must be **pinned to the same release** the package install is using. Otherwise the test mixes "release X package" with "whatever is on GitHub `main` today" and stops measuring the published artifact.

Pin invariant: when the package install is `mozyo-bridge==<X.Y.Z>`, set `MOZYO_BRIDGE_SKILL_REF` to the matching git ref. The standard choice is the release tag `v<X.Y.Z>` for production publishes, the same tag for pre-releases (e.g. `v0.1.0a1`), or a specific commit SHA when the release has no tag yet (rare; record the SHA in the acceptance log).

```bash
export MOZYO_BRIDGE_SKILL_REF=v<X.Y.Z>   # matches the installed mozyo-bridge==<X.Y.Z>

mozyo-bridge rules install

# Codex skill, pinned to the same release:
curl -fsSL "https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/${MOZYO_BRIDGE_SKILL_REF}/scripts/install_codex_skill.sh" | sh

# Claude global skill, pinned to the same release:
curl -fsSL "https://raw.githubusercontent.com/hollySizzle/mozyo_bridge/${MOZYO_BRIDGE_SKILL_REF}/scripts/install_claude_skill.sh" | MOZYO_BRIDGE_CLAUDE_SCOPE=global sh
```

The pipe-then-env form is required for the Claude install line. `MOZYO_BRIDGE_CLAUDE_SCOPE=global curl ... | sh` delivers the env var to `curl`, not to the `sh` that runs the script, and silently falls back to `scope=project`. The `mozyo-bridge doctor` `next_action` enforces the correct form; this doc keeps the same shape.

The install scripts internally use `MOZYO_BRIDGE_SKILL_REF` (per `skill-distribution.md`) to fetch the matching `skills/mozyo-bridge-agent/` tree from the pinned ref. Verify by reading the installed `SKILL.md` after install and confirming its content matches the tag's tree.

Default unpinned form (`.../mozyo_bridge/main/scripts/...` without `MOZYO_BRIDGE_SKILL_REF`) is the documented Beta Tester smoke install. It is **not** valid for this acceptance test because it does not pin to the published release artifact.

## Pre-deletion baseline

```bash
mozyo-bridge doctor --target .
```

Expected: `cli` / `rules` / `codex_skill` / `claude_skill` sections all `ok`. `scaffold` may be `ok` (manifest matches) or `drifted` (router edited locally) — either is acceptable as a baseline as long as the routers exist.

Capture the output so the post-recovery state can be compared.

## Delete root routers (intentional destructive step)

```bash
rm AGENTS.md CLAUDE.md
git status --porcelain
```

Expected `git status --porcelain` output (exact):

```text
 D AGENTS.md
 D CLAUDE.md
```

If any other path appears, **stop**. The prerequisite check missed something. Restore with the command below and investigate before retrying.

Restore path (run any time before re-scaffolding to abort the test safely):

```bash
git checkout HEAD -- AGENTS.md CLAUDE.md
```

## Re-scaffold from the installed package

The acceptance test must verify recovery for **both** presets. Run the sweep below in order and confirm `result: clean` and `scaffold: ok` at each step. The acceptance test passes only when **both** presets recover cleanly.

### Sweep 1 — Asana recovery

```bash
mozyo-bridge scaffold apply asana --target .
mozyo-bridge scaffold status --target .
mozyo-bridge doctor --target .
```

Expected:

- `scaffold apply asana --target .` writes `AGENTS.md`, `CLAUDE.md`, and `.mozyo-bridge/scaffold.json`.
- `scaffold status --target .` reports `result: clean`, `central status: ok`, all router files `ok`.
- `doctor --target .` reports `scaffold: ok target=<repo>`, with `cli` / `rules` / `codex_skill` / `claude_skill` still `ok`. `tmux` may be `warning` / `skipped` depending on the session; that is informational, not a failure of the acceptance test.

### Restore between sweeps

Before running the Redmine sweep, restore the worktree so the Redmine scaffold starts from the same no-router baseline as the Asana sweep:

```bash
git checkout HEAD -- AGENTS.md CLAUDE.md
rm -rf .mozyo-bridge
rm AGENTS.md CLAUDE.md
git status --porcelain   # expected: ` D AGENTS.md` and ` D CLAUDE.md` only
```

This puts the tree back at "tracked routers deleted, no scaffold manifest", which is the only state where the Redmine sweep measures recovery rather than overwriting an existing Asana scaffold.

### Sweep 2 — Redmine recovery

```bash
mozyo-bridge scaffold apply redmine --target .
mozyo-bridge scaffold status --target .
mozyo-bridge doctor --target .
```

Same expectations as Sweep 1, with `preset=redmine` in the `scaffold status` output and `mozyo-bridge notify-` / `Redmine gate lifecycle` substrings in the generated `AGENTS.md`.

### Final state before agent restart

After both sweeps pass, set the repo back to the preset under which the subject ticket runs. The subject ticket below (`1214673801774740`) is an Asana task and this repository dogfoods on Asana, so the **final state before restarting agents must be the Asana scaffold**:

```bash
git checkout HEAD -- AGENTS.md CLAUDE.md
rm -rf .mozyo-bridge
rm AGENTS.md CLAUDE.md
mozyo-bridge scaffold apply asana --target .
mozyo-bridge scaffold status --target .   # expected: result: clean
```

Confirm the final state is Asana (and not Redmine left over from Sweep 2) by inspecting the generated `AGENTS.md` for the `Asana task state と task comment` substring and the absence of Redmine-only markers (`mozyo-bridge notify-`, `Redmine gate lifecycle`). If the subject ticket is hosted on a different ticket system in a future iteration of this acceptance test, swap the preset accordingly — the rule is always "the final scaffold preset must match the subject ticket's ticket system".

## Operator Gate

This is the boundary between the automatable preparation flow and the operator-driven acceptance flow. Everything **above** this gate (prerequisite checks, install, baseline doctor, deletion, Sweeps 1 and 2, final-state scaffold) is reproducible by a shell script or CI runner and produces observable artifacts: a `scaffold status` `result: clean`, a `doctor --target .` showing `scaffold: ok` on the matching preset, and a `git status --porcelain` that contains only the expected scaffold residue described below. Everything **below** this gate requires a live operator and live agent sessions; do not script past this point.

Note: `git status --porcelain` **will not be empty** at this point — the final-state scaffold deliberately leaves the recovered routers and a fresh `.mozyo-bridge/scaffold.json` on disk, and the prerequisite "clean worktree" requirement only applied to the entry state before the destructive step. The gate criterion is "no unexpected changes", not "no changes".

Before crossing the gate, the operator must confirm:

- `git status --porcelain` contains **only** the expected scaffold residue and nothing else. Expected entries are:
  - `?? .mozyo-bridge/` — the scaffold manifest directory, untracked because this repository does not track `.mozyo-bridge/`.
  - ` M AGENTS.md` and ` M CLAUDE.md` if the freshly scaffolded routers differ byte-for-byte from the tracked routers (this happens when the installed preset is newer or older than the preset that wrote the tracked routers). If the installed preset matches HEAD's preset version exactly, these two paths will not appear.
  - No other paths. Any additional path (other modified files, other untracked files, deleted files beyond the routers) means the preparation flow leaked outside its intended scope and the test must abort.
- `mozyo-bridge scaffold status --target .` returns `result: clean` and `central status: ok`.
- `mozyo-bridge doctor --target .` returns `scaffold: ok` with the final-state preset, and `cli` / `rules` / `codex_skill` / `claude_skill` all `ok` (the warning case is acceptable only when it documents a known dogfood condition — never a real degradation).
- The final-state `AGENTS.md` content matches the subject ticket's ticket system per the previous section.
- Both Claude Code and Codex are still running their **pre-acceptance** sessions — do not restart anything yet.

Once those confirmations are recorded (e.g. in an acceptance log or a separate Asana comment that is **not** the subject ticket), the operator owns every remaining step in this document. Automation must stop here; the agents themselves are the thing being measured below.

## Restart Claude Code and Codex (operator)

Past the Operator Gate. Skills, central preset, and router context are cached for the lifetime of each agent session, so the operator must restart both Claude Code and Codex by hand so they pick up:

- the freshly-written `AGENTS.md` / `CLAUDE.md` router pair,
- the `mozyo-bridge-agent` skill from the just-installed user-global location,
- the central preset under `${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/<preset>/`.

If either agent is left running through this transition, the test does not measure recovery — it measures whatever state was cached when the destructive step started. Do not script the restart; an automated kill / relaunch can race the cache and silently invalidate the acceptance result.

## Ticket-ID handoff — operator (the actual acceptance subject)

Past the Operator Gate. Working from a fresh Codex session, the operator hands a single normal-development ticket ID to Codex via the established notification path. Use the Enter-send issue (Asana task `1214673801774740`) as the subject:

```text
Asana task 1214673801774740 を実装してください。
```

That is the only input the operator should provide. From there, the acceptance criterion is that Codex and Claude:

1. Run the Ticket-ID Entrypoint per `vibes/docs/logics/autonomous-ticket-entrypoint.md`. Fetch the durable task record. Do not treat the pane / chat framing as authoritative.
2. Apply the Codex / Claude role boundary: Codex frames, hands off to Claude, audits; Claude implements.
3. Use `mozyo-bridge doctor` if any prerequisite looks off (skills missing, central preset drifted, router missing).
4. Record durable audit trail / changes / verification / Codex audit focus on the Asana task, with explicit receive method.
5. Commit only after Codex audit approval. Mark complete only after the audit comment is captured.

The Enter-send bug itself is **not** in scope for this acceptance test. The acceptance test only verifies that the autonomous handoff workflow runs end-to-end on the installed package. Whether the agents successfully fix the Enter-send bug, or correctly escalate / report inability, is the surface the acceptance test measures.

## Self-audit — operator

Past the Operator Gate. After the ticket flow reaches a natural pause (close, abort, or escalation), the operator hands each agent the UserStory and any hidden follow-up tasks and asks them to self-audit. This step must be operator-driven; do not script the prompt or paste it before the ticket flow has actually paused, because the agents need to see the real durable trail before they audit it.

```text
UserStory 1214673802242667 の acceptance を読んで、本 turnkey E2E run が
guardrails (router 削除→復旧 / Ticket-ID Entrypoint / Codex-Claude role / doctor /
durable audit trail) を満たしたか自己採点してください。
```

Each agent reports:

- Which UserStory acceptance criteria were satisfied, with evidence (Asana comment ids, doctor output, scaffold status).
- Which guardrails fired and which should have fired but did not.
- Any scope that leaked into chat-only or remained outside the durable record.
- Any place where the installed package's CLI surface was insufficient to follow this doc — these become follow-up tasks, not workarounds.

The acceptance test result is the operator's summary of those two self-audits plus their own observations.

## Restore the repo after the test

Once the acceptance test is complete (pass, fail, or aborted):

```bash
git checkout HEAD -- AGENTS.md CLAUDE.md
rm -rf .mozyo-bridge
git status --porcelain
```

Expected: empty. `.mozyo-bridge/scaffold.json` is not tracked in this repository, so the `rm -rf .mozyo-bridge` returns the worktree to its pre-test state. If `git status --porcelain` shows anything else, investigate before continuing other work — the test left residue that must be reconciled by hand.

## Prohibitions

- Do not run this acceptance test on a dirty worktree, an untracked clone, or a worktree where `git status --porcelain` is non-empty before the destructive step.
- Do not delete anything other than the root `AGENTS.md` / `CLAUDE.md` pair. Wider deletions are not part of this acceptance test.
- Do not publish a release as part of this test. Publishing is a separate, owner-gated flow in `release-flow.md`.
- Do not edit runtime behavior to make a doc step work. If a step is impossible with the installed CLI, stop and file a follow-up task with the exact command and error.
- Do not implement the Enter-send fix as a side effect. The fix is the subject of the autonomous run, not a deliverable of this acceptance test.
- Do not move this doc's content into root `AGENTS.md` / `CLAUDE.md`. Root files are routers and stay thin.

## Cross-references

- Fresh tester install + isolated smoke: `README.md` (`Beta Tester Install (GitHub main)`).
- Scaffold drift detection: `vibes/docs/logics/scaffold-rules.md`.
- Skill install scopes and precedence: `vibes/docs/logics/skill-distribution.md`.
- Ticket-ID entrypoint: `vibes/docs/logics/autonomous-ticket-entrypoint.md`.
- PyPI / TestPyPI publish gates: `vibes/docs/logics/release-flow.md`.
- Asana UserStory (acceptance subject): `1214673802242667`.
- Asana Enter-send issue (final E2E ticket): `1214673801774740`.
