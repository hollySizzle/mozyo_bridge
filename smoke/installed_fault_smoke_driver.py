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


def _run(cli: Path, argv: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([str(cli), *argv], capture_output=True, text=True, env=env)


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
    "callback_lease", "sublane_list", "recover_stale", "session_rollback", "callback_exactly_once",
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
    results["session_rollback"] = _drive_session_rollback(cli, tmp)
    results["callback_exactly_once"] = _drive_callback_exactly_once(cli, tmp)
    return results


def _drive_recover_stale(cli: Path, tmp: Path) -> bool:
    """F2 critical path installed: the exact stale worker is CLOSED once and the launch is owed."""
    from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleKey, LaneLifecycleStore
    from mozyo_bridge.core.state.replacement_transaction import DecisionPointer
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.stale_worker_recovery import (  # noqa: E501
        stale_worker_recovery_action_id,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        derive_lane_workspace_token,
    )
    from tests.support.agent_provider_binaries import FakeAgentBinaries

    lane = "issue_14097_smoke_worker"
    ws_id = f"fixture-14097-smoke-{lane}"
    repo = tmp / f"rs_repo_{lane}"
    _git_init(repo, branch=lane, ws_id=ws_id)
    home = tmp / "rs_home"
    home.mkdir(parents=True, exist_ok=True)
    lstore = LaneLifecycleStore(home=home)
    lkey = LaneLifecycleKey(ws_id, lane)
    lstore.declare_active(lkey, decision=DecisionPointer(source="redmine", issue_id="14097",
                          journal_id="79485"), issue_id="14097",
                          worktree_identity=derive_lane_workspace_token(str(repo)))
    lrec = lstore.get(lkey)
    name = encode_assigned_name(ws_id, "claude", lane)
    fake = FakeHerdr(read_text="idle\n> ")
    fws = fake.seed_workspace(cwd=str(repo))
    locator = fake.seed_agent(name, workspace_id=fws, provider="", status="unknown",
                              detected_agent="", revision="3", cwd=str(repo))
    action_id = stale_worker_recovery_action_id(lane_id=lane, role="claude", provider="claude",
                                                assigned_name=name, locator=locator)
    bins = FakeAgentBinaries(tmp / "rs_bins")
    env = _base_env(home, herdr_state=_write_state(fake, tmp, "rs_state.json"))
    env["PATH"] = str(bins.bin_dir) + os.pathsep + env.get("PATH", "")
    out = _run(cli, [
        "sublane", "recover-stale", "--issue", "14097", "--lane", lane, "--role", "claude",
        "--provider", "claude", "--assigned-name", name, "--locator", locator,
        "--worker-revision", "3", "--expected-gate", "implementation_request",
        "--next-semantic-action", "dispatch_once", "--action-id", action_id,
        "--journal", "79485", "--action-generation", "7",
        "--lane-revision", str(lrec.revision), "--lane-generation", str(lrec.lane_generation),
        "--execute", "--json", "--repo", str(repo),
    ], env)
    try:
        payload = json.loads(out.stdout)
        return bool(payload["closed_old_worker"]) and payload["status"] == "stopped"
    except (ValueError, KeyError):
        return False


def _drive_session_rollback(cli: Path, tmp: Path) -> bool:
    """F3 critical path installed: the fresh unhealthy launch is closed, then replay is idempotent."""
    from mozyo_bridge.core.state.startup_transaction_fence import (
        Participant, StartupTransactionFence, StartupUnit,
    )

    lane, ws_id = "issue_14097_smoke_nested", "fixture-14097-smoke-nested"
    home = tmp / "sr_home"
    home.mkdir(parents=True, exist_ok=True)
    fence = StartupTransactionFence(home=home)
    action = fence.reserve(StartupUnit(workspace_id=ws_id, lane_id=lane, providers=("claude",)), "n1")
    name = encode_assigned_name(ws_id, "claude", lane)
    fake = FakeHerdr(read_text="idle\n> ")
    fws = fake.seed_workspace(cwd=str(tmp))
    locator = fake.seed_agent(name, workspace_id=fws, provider="claude")
    fence.record_participant(action.action_id,
                             Participant(role="claude", assigned_name=name, locator=locator,
                                         receipt=locator))
    state = _write_state(fake, tmp, "sr_state.json")
    env = _base_env(home, herdr_state=state)
    execu = _run(cli, ["herdr", "session-rollback", "--action-id", action.action_id,
                       "--execute", "--json", "--repo", str(tmp)], env)
    replay = _run(cli, ["herdr", "session-rollback", "--action-id", action.action_id,
                        "--json", "--repo", str(tmp)], env)
    try:
        ex = json.loads(execu.stdout)
        rp = json.loads(replay.stdout)
        return (ex["state"] == "completed" and ex["participants"][0]["closed"]
                and rp["reason"] == "already_rolled_back")
    except (ValueError, KeyError, IndexError):
        return False


def _drive_callback_exactly_once(cli: Path, tmp: Path) -> bool:
    """F4 critical path installed: re-ingesting the same dispatch anchor is idempotent (dup 0)."""
    home = tmp / "cb_home"
    home.mkdir(parents=True, exist_ok=True)
    snap = tmp / "cb_issue.json"
    snap.write_text(json.dumps({"issue": {"id": "14097", "journals": [
        {"id": "84000", "notes": "gate [mozyo:workflow-event:gate=implementation_done]"}
    ]}}), encoding="utf-8")
    env = _base_env(home)
    common = ["--candidate", "14097:84000:coordinator:implementation_done",
              "--redmine-json", str(snap), "--workspace-id", "fixture-14097-smoke-cb",
              "--cursor", "84001", "--json"]
    first = _run(cli, ["workflow", "callbacks", "--ingest", *common], env)
    again = _run(cli, ["workflow", "callbacks", "--ingest", *common], env)
    try:
        return (json.loads(first.stdout)["enqueued"] == 1
                and json.loads(again.stdout)["duplicates"] == 1
                and json.loads(again.stdout)["enqueued"] == 0)
    except (ValueError, KeyError):
        return False


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
