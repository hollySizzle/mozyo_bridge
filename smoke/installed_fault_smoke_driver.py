#!/usr/bin/env python3
"""Shell-heavy driver for the installed fault smoke (Redmine #14097).

Split from ``installed_fault_smoke.py`` so that file keeps a small PURE decision surface the
hermetic unittest exercises without a real subprocess. This module OWNS the real
installed-``mozyo-bridge`` subprocess drives: each fault shape's public entrypoint (proving the
built artifact dispatches it) and a representative success path per driveable shape, every one
under an isolated ``MOZYO_BRIDGE_HOME`` + a secret-free temp fake-herdr state served by the
canonical fake through ``smoke/support/fake_herdr_cli.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support.herdr_fake import (  # noqa: E402
    STATUS_WORKING,
    FakeHerdr,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    encode_assigned_name,
)

_FAKE_HERDR_CLI = _REPO_ROOT / "smoke" / "support" / "fake_herdr_cli.py"


def _base_env(home: Path, *, herdr_state: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "MOZYO_REPO")}
    env["MOZYO_BRIDGE_HOME"] = str(home)
    if herdr_state is not None:
        # The installed CLI shells out to this exact executable as its herdr binary (a directly
        # executable script via its shebang; the resolver verifies isfile + X_OK).
        env["MOZYO_HERDR_BINARY"] = str(_FAKE_HERDR_CLI)
        env["MOZYO_FAKE_HERDR_STATE"] = str(herdr_state)
    return env


def _run(cli: Path, argv: list[str], env: dict, *, cwd: "str | None" = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(cli), *argv], capture_output=True, text=True, env=env, cwd=cwd)


def drive_entrypoints(cli: Path, tmp: Path) -> dict[str, int]:
    """Run each fault shape's installed public entrypoint (``--help``); returns {shape: rc}."""
    from installed_fault_smoke import SHAPE_ENTRYPOINTS

    home = tmp / "entry_home"
    home.mkdir(parents=True, exist_ok=True)
    env = _base_env(home)
    return {shape: _run(cli, list(argv), env).returncode for shape, argv in SHAPE_ENTRYPOINTS}


def _herdr_repo(tmp: Path, ws_id: str) -> Path:
    repo = tmp / "herdr_repo"
    (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text(
        "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8"
    )
    (repo / ".mozyo-bridge" / "workspace-anchor.json").write_text(
        json.dumps({
            "schema_version": 1, "workspace_id": ws_id, "canonical_session": "fixture_14097_smoke",
            "project_name": "mozyo-bridge", "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    return repo


#: The fault-shape critical paths the installed layer MUST drive as a real subprocess (not
#: ``--help``). The pure summary fails closed if any is missing (Redmine #14097 review j#84441 F1).
REQUIRED_REPRESENTATIVE: tuple[str, ...] = (
    "callback_lease", "sublane_list", "recover_stale", "recover_stale_negative",
    "session_rollback", "callback_exactly_once",
)


def _write_state(fake: FakeHerdr, tmp: Path, name: str) -> Path:
    state = tmp / name
    state.write_text(json.dumps(fake.to_state()), encoding="utf-8")
    return state


def drive_representative(cli: Path, tmp: Path) -> dict[str, bool]:
    """Drive a representative CRITICAL path per fault shape through the installed artifact."""
    results: dict[str, bool] = {}

    # callback-lease: bootstrap the store, then a healthy status read (no herdr backend needed).
    cl_home = tmp / "cl_home"
    cl_home.mkdir(parents=True, exist_ok=True)
    env = _base_env(cl_home)
    boot = _run(cli, ["workflow", "callback-lease", "--bootstrap"], env)
    status = _run(cli, ["workflow", "callback-lease"], env)
    results["callback_lease"] = boot.returncode == 0 and status.returncode == 0

    # sublane list --json (#14063): the installed CLI reads the fake herdr inventory and must NOT
    # leak a locator-present shell-residue worker into a live pane.
    ws_id = "fixture-14097-smoke-workspace"
    repo = _herdr_repo(tmp, ws_id)
    home = tmp / "list_home"
    home.mkdir(parents=True, exist_ok=True)
    from mozyo_bridge.core.state.lane_metadata import record_lane_created

    record_lane_created(
        lane_workspace_token="issue_14097_smoke", repo_workspace_id=ws_id, issue_id="14097",
        lane_label="issue_14097_smoke", branch="issue_14097_smoke", lane_id="issue_14097_smoke",
        source_backend="herdr", home=home,
    )
    fake = FakeHerdr(read_text="idle\n> ")
    fws = fake.seed_workspace(cwd=str(repo))
    fake.seed_agent(encode_assigned_name(ws_id, "codex", "issue_14097_smoke"),
                    workspace_id=fws, provider="codex", status=STATUS_WORKING)
    fake.seed_agent(encode_assigned_name(ws_id, "claude", "issue_14097_smoke"),
                    workspace_id=fws, provider="", status="unknown", detected_agent="")
    out = _run(cli, ["sublane", "list", "--json", "--repo", str(repo)],
               _base_env(home, herdr_state=_write_state(fake, tmp, "list_state.json")))
    try:
        lane = next(la for la in json.loads(out.stdout)["sublanes"]
                    if la["lane_id"] == "issue_14097_smoke")
        results["sublane_list"] = (
            out.returncode == 0 and lane["state"] == "gateway_only"
            and lane["worker_pane"] is None and "worker_slot_stale" in lane["stale_hints"]
        )
    except (ValueError, StopIteration, KeyError):
        results["sublane_list"] = False

    results["recover_stale"] = _drive_recover_stale(cli, tmp)
    results["recover_stale_negative"] = _drive_recover_stale_negative(cli, tmp)
    results["session_rollback"] = _drive_session_rollback(cli, tmp)
    results["callback_exactly_once"] = _drive_callback_exactly_once(cli, tmp)
    return results


def _drive_recover_stale(cli: Path, tmp: Path) -> bool:
    """F2 positive installed: the recovery drives to the COMPLETED terminal through the artifact.

    Not a boolean resume flag (Redmine #14097 review j#85090 F2 — ``post_close_resume`` is true for
    an authority refusal / stopped launch / uncertain redispatch too). The built ``mozyo-bridge``
    binary, in an isolated home + fake herdr, must satisfy the SINGLE shared acceptance predicate
    :func:`installed_fault_smoke.recover_stale_accepts`: pass 1 closes the exact worker once and owns
    the launch (``in_progress``); pass 2 attests the fresh receiver and drives the post-close resume
    to completed / recovered / confirmed / fresh-attested, a single redispatch (exactly one exact-
    marker queue-enter delivery attempt, and it confirmed) with NO additional close.
    """
    from installed_fault_smoke import recover_stale_accepts

    return recover_stale_accepts(_recover_stale_outcome(cli, tmp, "rs"))


def _drive_recover_stale_negative(cli: Path, tmp: Path) -> bool:
    """F2 negative CONTROL installed: the SAME acceptance predicate must reject an injected fault.

    Seed the fresh receiver's attestation OUTSIDE the redispatch's durable landing window so the
    confirm fence rejects the send: the exact same drive then stops at ``redispatch_status=uncertain``
    and the ONE shared :func:`installed_fault_smoke.recover_stale_accepts` predicate must return False
    on it (Redmine #14097 review j#85253). Because positive and negative share that predicate,
    weakening any conjunct (e.g. the post_close_resume-only regression j#85090 flagged) flips this
    control green->red instead of passing silently. Guarded on the injection actually landing the
    uncertain fault, so a setup slip cannot vacuously satisfy the negation.
    """
    from installed_fault_smoke import recover_stale_accepts

    outcome = _recover_stale_outcome(cli, tmp, "rsneg", inject_uncertain=True)
    if outcome is None:
        return False
    injected_uncertain = (
        outcome["pass2"].get("redispatch_status") == "uncertain"
        and outcome["pass2"].get("status") != "completed"
    )
    return injected_uncertain and not recover_stale_accepts(outcome)


def _recover_stale_outcome(
    cli: Path, tmp: Path, tag: str, *, inject_uncertain: bool = False
) -> "tuple | None":
    """Drive the two-pass #13806 recovery through the installed artifact; return payloads + observables.

    Returns ``(pass1_json, pass2_json, agents_before, agents_after, redispatch_ok_count,
    fresh_locator, old_locator)`` or ``None`` on a setup / parse failure. Between passes it seeds
    the fresh receiver's startup identity attestation (the durable signal the relaunched worker came
    online), stamped inside the redispatch landing window (positive) or far in the future
    (``inject_uncertain`` — the confirm fence then rejects the send).
    """
    import datetime as _dt

    from mozyo_bridge.core.state.herdr_delivery_ledger import HerdrDeliveryLedger
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HerdrIdentityAttestationStore, IdentityAttestationRecord, VERDICT_PRESENT,
    )
    from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleKey, LaneLifecycleStore
    from mozyo_bridge.core.state.replacement_transaction import DecisionPointer
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
        stale_worker_recovery_action_id,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
    )
    from tests.support.agent_provider_binaries import FakeAgentBinaries

    lane = f"issue_14097_smoke_{tag}"
    ws_id = f"fixture-14097-smoke-{lane}"
    repo = tmp / f"rs_repo_{tag}"
    _git_init(repo, branch=lane, ws_id=ws_id)
    home = tmp / f"rs_home_{tag}"
    home.mkdir(parents=True, exist_ok=True)
    lstore = LaneLifecycleStore(home=home)
    lkey = LaneLifecycleKey(ws_id, lane)
    lstore.declare_active(lkey, decision=DecisionPointer(source="redmine", issue_id="14097",
                          journal_id="79485"), issue_id="14097",
                          worktree_identity=derive_lane_workspace_token(str(repo)))
    lrec = lstore.get(lkey)
    name = encode_assigned_name(ws_id, "claude", lane)
    fake = FakeHerdr(read_text="idle\n> ")
    # A live-composer echo so the queue-enter landing marker is observable in the fresh worker's
    # pane read — the signal that promotes the redispatch to reason=ok (the confirm oracle).
    fake.echo_composer = True
    fws = fake.seed_workspace(cwd=str(repo))
    locator = fake.seed_agent(name, workspace_id=fws, provider="", status="unknown",
                              detected_agent="", revision="3", cwd=str(repo))
    # The surviving gateway slot the heal adopts + pins the tab on (a heal never splits the pair).
    fake.seed_agent(encode_assigned_name(ws_id, "codex", lane), workspace_id=fws, provider="codex",
                    cwd=str(repo))
    action_id = stale_worker_recovery_action_id(lane_id=lane, role="claude", provider="claude",
                                                assigned_name=name, locator=locator)
    bins = FakeAgentBinaries(tmp / f"rs_bins_{tag}")
    state = tmp / f"rs_state_{tag}.json"
    state.write_text(json.dumps(fake.to_state()), encoding="utf-8")

    def env():
        e = _base_env(home, herdr_state=state)
        e["PATH"] = str(bins.bin_dir) + os.pathsep + e.get("PATH", "")
        e["MOZYO_AGENT_CLAUDE_BINARY"] = bins.path("claude")
        e["MOZYO_AGENT_CODEX_BINARY"] = bins.path("codex")
        # The recovery drives on the LANE worktree (``--repo`` == repo); its redispatch's nested
        # send anchors there, so it must attest AS this lane's identity (env == the repo's anchor
        # workspace) or the sender-anchor fence blocks the send (target_unavailable).
        e["MOZYO_WORKSPACE_ID"] = ws_id
        e["MOZYO_AGENT_ROLE"] = "codex"
        e["MOZYO_LANE_ID"] = lane
        return e

    argv = [
        "sublane", "recover-stale", "--issue", "14097", "--lane", lane, "--role", "claude",
        "--provider", "claude", "--assigned-name", name, "--locator", locator,
        "--worker-revision", "3", "--expected-gate", "implementation_request",
        "--next-semantic-action", "dispatch_once", "--action-id", action_id,
        "--journal", "79485", "--action-generation", "7",
        "--lane-revision", str(lrec.revision), "--lane-generation", str(lrec.lane_generation),
        "--execute", "--json", "--repo", str(repo),
    ]

    # Pass 1: close the exact stale worker once, own the launch (attestation still owed).
    first = _run(cli, argv, env(), cwd=str(repo))
    try:
        p1 = json.loads(first.stdout)
    except ValueError:
        return None

    # The heal-launched fresh worker (same assigned name, a new pane) and its attestation.
    st = json.loads(state.read_text(encoding="utf-8"))
    fresh_candidates = [a["pane_id"] for a in st["agents"]
                        if a["name"] == name and a["pane_id"] != locator]
    if not fresh_candidates:
        return None
    fresh = fresh_candidates[0]
    observed_at = (
        "2999-01-01T00:00:00+00:00" if inject_uncertain
        else _dt.datetime.now(_dt.timezone.utc).isoformat()
    )
    HerdrIdentityAttestationStore(home=home).upsert(IdentityAttestationRecord(
        assigned_name=name, workspace_id=ws_id, role="claude", lane_id=lane, locator=fresh,
        verdict=VERDICT_PRESENT, observed_at=observed_at, replacement_action_id=action_id))
    # Re-serialize the fake (EXEC1 mutated it) with the composer echo + the fresh worker's
    # turn-start armed, so the second pass observes a live, attested receiver.
    fake2 = FakeHerdr.from_state(json.loads(state.read_text(encoding="utf-8")))
    fake2.echo_composer = True
    fake2.arm_transition(fresh, STATUS_WORKING)
    agents_before = sorted((a["name"], a["pane_id"]) for a in fake2.agents)
    state.write_text(json.dumps(fake2.to_state()), encoding="utf-8")

    # Pass 2: the post-close resume driven to its terminal.
    second = _run(cli, argv, env(), cwd=str(repo))
    try:
        p2 = json.loads(second.stdout)
    except ValueError:
        return None
    agents_after = sorted(
        (a["name"], a["pane_id"])
        for a in FakeHerdr.from_state(json.loads(state.read_text(encoding="utf-8"))).agents
    )
    try:
        records = HerdrDeliveryLedger(home=home).records_for_issue("14097")
    except Exception:  # noqa: BLE001 - an unreadable ledger reads as zero confirmed sends
        records = []
    # Single-redispatch is measured on ALL exact-marker/target delivery attempts, not only the
    # ``reason=ok`` subset (review j#85253): "one confirmed send + one extra non-ok attempt" must
    # not read as a single redispatch. ``attempt_count`` counts every queue-enter delivery_outcome
    # to the fresh worker for this recovery's redispatch marker; ``ok_count`` its confirmed subset.
    redispatch_marker = _redispatch_marker(issue="14097", journal="79485")
    attempts = [
        r for r in records
        if (r.entry_kind == "delivery_outcome" and r.rail == "queue_enter_rail"
            and r.target == fresh and _norm(r.notification_marker) == redispatch_marker)
    ]
    ok_count = sum(1 for r in attempts if r.status == "sent" and r.reason == "ok")
    return {
        "pass1": p1, "pass2": p2,
        "fresh_locator": fresh, "old_locator": locator,
        "agents_unchanged": agents_before == agents_after,
        "redispatch_attempt_count": len(attempts),
        "redispatch_ok_count": ok_count,
    }


def _redispatch_marker(*, issue: str, journal: str) -> str:
    """The exact ``[mozyo:handoff:...]`` marker the recovery redispatch writes (worker provider).

    Rebuilt through the SAME canonical ``build_marker`` the live use case's ``_redispatch_marker``
    uses (Redmine #13806 R3-F1) so the ledger filter matches byte-for-byte and never drifts from
    what ``dispatch_to_worker`` records (Redmine #14097 review j#85253 single-redispatch). The
    redispatch gate kind is the fixed ``implementation_request`` and the worker provider ``claude``.
    """
    from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (
        RedmineAnchor,
        build_marker,
    )

    return build_marker(RedmineAnchor(issue=issue, journal=journal), "implementation_request", "claude")


def _norm(value: "str | None") -> str:
    return (value or "").strip()


def _seed_owed_rollback(fence, fake, ws_id: str, lane: str, nonce: str):
    """Reserve a startup action + record a fresh idle launch that owes a rollback; return action id."""
    from mozyo_bridge.core.state.startup_transaction_fence import Participant, StartupUnit

    action = fence.reserve(StartupUnit(workspace_id=ws_id, lane_id=lane, providers=("claude",)), nonce)
    name = encode_assigned_name(ws_id, "claude", lane)
    locator = fake.seed_agent(name, workspace_id=list(fake._workspaces)[0], provider="claude")
    fence.record_participant(action.action_id,
                             Participant(role="claude", assigned_name=name, locator=locator,
                                         receipt=locator))
    return action.action_id, locator


def _drive_session_rollback(cli: Path, tmp: Path) -> bool:
    """F3 critical path installed: after discharge, the SAME binding replays under a NEW action id.

    Action A is rolled back; then a fresh reservation of the SAME startup unit mints a NEW action
    id (A != B), B is a live fresh launch (``eligible``), and A stays terminally rolled back.
    """
    from mozyo_bridge.core.state.startup_transaction_fence import StartupTransactionFence

    lane, ws_id = "issue_14097_smoke_nested", "fixture-14097-smoke-nested"
    home = tmp / "sr_home"
    home.mkdir(parents=True, exist_ok=True)
    fence = StartupTransactionFence(home=home)
    fake = FakeHerdr(read_text="idle\n> ")
    fake.seed_workspace(cwd=str(tmp))
    action_a, _ = _seed_owed_rollback(fence, fake, ws_id, lane, "n1")
    state = tmp / "sr_state.json"
    state.write_text(json.dumps(fake.to_state()), encoding="utf-8")
    env = _base_env(home, herdr_state=state)

    discharge = _run(cli, ["herdr", "session-rollback", "--action-id", action_a,
                           "--execute", "--json", "--repo", str(tmp)], env)
    # The same binding replays: a fresh reservation of the same unit mints a NEW action id. Reload
    # the state the discharge just mutated (A's fresh launch closed) before seeding B's launch.
    fake = FakeHerdr.from_state(json.loads(state.read_text(encoding="utf-8")))
    action_b, _ = _seed_owed_rollback(fence, fake, ws_id, lane, "n2")
    state.write_text(json.dumps(fake.to_state()), encoding="utf-8")
    replay_b = _run(cli, ["herdr", "session-rollback", "--action-id", action_b,
                          "--json", "--repo", str(tmp)], env)
    replay_a = _run(cli, ["herdr", "session-rollback", "--action-id", action_a,
                          "--json", "--repo", str(tmp)], env)
    try:
        dis = json.loads(discharge.stdout)
        rb = json.loads(replay_b.stdout)
        ra = json.loads(replay_a.stdout)
        return (dis["state"] == "completed" and dis["participants"][0]["closed"]
                and action_b != action_a
                and rb["participants"][0]["verdict"] == "eligible"
                and ra["reason"] == "already_rolled_back")
    except (ValueError, KeyError, IndexError):
        return False


def _drive_callback_exactly_once(cli: Path, tmp: Path) -> bool:
    """F4 critical path installed: the same dispatch anchor is DELIVERED (sent) exactly once.

    Ingest the anchor, then ``--deliver`` it and re-``--deliver``: the send/recovery edge fires
    once (a delivered row is terminal, so the re-deliver sends nothing), and a post-delivery sweep
    does not amplify the pending / dead-letter backlog.
    """
    ws_id = "fixture-14097-smoke-cb"
    repo = _herdr_repo_named(tmp, ws_id, "cb_repo")
    home = tmp / "cb_home"
    home.mkdir(parents=True, exist_ok=True)
    snap = tmp / "cb_issue.json"
    snap.write_text(json.dumps({"issue": {"id": "14097", "journals": [
        {"id": "84000", "notes": "gate [mozyo:workflow-event:gate=implementation_done]"}
    ]}}), encoding="utf-8")
    # The coordinator target the delivered callback routes to: a live default-lane codex pane in
    # the sending workspace, so the isolated fake-herdr transport can land the one send.
    fake = FakeHerdr(read_text="idle\n> ")
    fake.echo_composer = True  # the queue-enter send observes the marker it typed (landing)
    fws = fake.seed_workspace(cwd=str(repo))
    # The coordinator target starts IDLE and its turn-start (a change INTO ``working``) is armed,
    # so the delivered callback's turn-start confirmation fires via the canonical fake's popen wait
    # seam (Design Consultation j#84712) -> a confirmed ``delivered`` terminal.
    coord = fake.seed_agent(encode_assigned_name(ws_id, "codex", "default"),
                            workspace_id=fws, provider="codex")
    fake.arm_transition(coord, "working")
    state = _write_state(fake, tmp, "cb_state.json")

    def env():
        e = _base_env(home, herdr_state=state)
        e["MOZYO_WORKSPACE_ID"] = ws_id
        return e

    common = ["--candidate", "14097:84000:coordinator:implementation_done",
              "--redmine-json", str(snap), "--workspace-id", ws_id, "--cursor", "84001", "--json"]
    # The deliver's nested `handoff send` attests the sender from the CWD anchor, so run under the
    # herdr repo whose anchor is this workspace (else env-vs-anchor workspace mismatch blocks it).
    _run(cli, ["workflow", "callbacks", "--ingest", *common], env(), cwd=str(repo))
    d1 = _run(cli, ["workflow", "callbacks", "--deliver", "--workspace-id", ws_id, "--json"], env(), cwd=str(repo))
    d2 = _run(cli, ["workflow", "callbacks", "--deliver", "--workspace-id", ws_id, "--json"], env(), cwd=str(repo))
    sw = _run(cli, ["workflow", "callbacks", "--sweep", "--workspace-id", ws_id, "--json"], env(), cwd=str(repo))
    try:
        p1, p2, sweep = json.loads(d1.stdout), json.loads(d2.stdout), json.loads(sw.stdout)
        # The dispatch anchor is DELIVERED (a confirmed turn-start terminal) exactly once: the
        # deliver claims + sends the row and the receiver's turn-start confirms it; the re-deliver
        # sends nothing (a delivered row is terminal — duplicate notification 0); and a post-delivery
        # sweep does not amplify the pending / dead-letter backlog.
        return (len(p1["delivered"]) == 1 and p1["delivered"][0]["send_outcome"] == "delivered"
                and p2["delivered"] == []
                and sweep["dead_letter"] == [] and len(sweep["pending"]) == 0)
    except (ValueError, KeyError, IndexError):
        return False


def _herdr_repo_named(tmp: Path, ws_id: str, name: str) -> Path:
    repo = tmp / name
    (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text(
        "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8")
    (repo / ".mozyo-bridge" / "workspace-anchor.json").write_text(json.dumps({
        "schema_version": 1, "workspace_id": ws_id, "canonical_session": "fixture_14097_smoke",
        "project_name": "mozyo-bridge", "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    return repo


def _git_init(repo: Path, *, branch: str, ws_id: str) -> None:
    import subprocess as _sp

    repo.mkdir(parents=True, exist_ok=True)
    run = _sp.run
    run(["git", "init", "-b", branch], cwd=repo, check=True, capture_output=True)
    run(["git", "config", "user.email", "h@example.invalid"], cwd=repo, check=True, capture_output=True)
    run(["git", "config", "user.name", "h"], cwd=repo, check=True, capture_output=True)
    (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
    (repo / ".mozyo-bridge" / "config.yaml").write_text(
        "version: 1\nterminal_transport:\n  backend: herdr\n", encoding="utf-8")
    (repo / ".mozyo-bridge" / "workspace-anchor.json").write_text(json.dumps({
        "schema_version": 1, "workspace_id": ws_id, "canonical_session": "fixture_14097_smoke",
        "project_name": "mozyo-bridge", "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
