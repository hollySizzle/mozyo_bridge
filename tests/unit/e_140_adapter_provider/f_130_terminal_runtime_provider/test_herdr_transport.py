"""Built-in herdr CLI transport adapter tests (Redmine #13245).

Pins the pure, fail-closed herdr CLI adapter and its default-off selection
resolver *without a live herdr binary*: argv construction for each primitive is
verified through an injected subprocess ``runner``, and the fail-closed paths
(malformed target, missing binary, non-zero exit, timeout) are simulated. The
resolver is pinned for every branch: tmux/off returns ``None``; herdr with no
trusted-env binary, an unresolvable binary, and a resolvable binary each behave
per the seam contract with no silent fallback to tmux. No subprocess spawns.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BINARY_SOURCE_ENV,
    BINARY_SOURCE_PATH,
    REASON_BINARY_AMBIGUOUS,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_BINARY_UNSAFE_PATH,
    REASON_INVALID_SOURCE,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    SOURCE_VISIBLE,
    HerdrBinaryResolution,
    TerminalTransportConfig,
    TerminalTransportError,
    TerminalTransportPort,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    HERDR_BINARY_ENV,
    HERDR_PATH_NAME,
    HerdrCliTransport,
    resolve_herdr_binary,
    resolve_terminal_transport,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.turn_start_rail import (
    composer_retains_body,
)


class RecordingRunner:
    """A ``subprocess.run``-shaped callable that records argv and replays a result."""

    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.calls: list = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout=self._stdout, stderr=self._stderr
        )


BIN = "/opt/herdr/bin/herdr"


class ArgvConstructionTest(unittest.TestCase):
    def test_send_text_argv(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.send_text("w1:p1", "hello world")
        self.assertTrue(result.ok)
        argv = runner.calls[0][0]
        self.assertEqual(argv, [BIN, "pane", "send-text", "w1:p1", "hello world"])

    def test_send_keys_argv(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.send_keys("w1:p1", "enter")
        self.assertTrue(result.ok)
        self.assertEqual(runner.calls[0][0], [BIN, "pane", "send-keys", "w1:p1", "enter"])

    def test_read_pane_argv_with_lines(self) -> None:
        runner = RecordingRunner(stdout="raw screen text")
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("poc_claude", source=SOURCE_VISIBLE, lines=30)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "raw screen text")
        self.assertEqual(
            runner.calls[0][0],
            [BIN, "agent", "read", "poc_claude", "--source", "visible", "--lines", "30"],
        )

    def test_read_pane_argv_without_lines(self) -> None:
        runner = RecordingRunner(stdout="x")
        transport = HerdrCliTransport(BIN, runner=runner)
        transport.read_pane("poc_claude")
        self.assertEqual(
            runner.calls[0][0], [BIN, "agent", "read", "poc_claude", "--source", "visible"]
        )

    def test_read_pane_parses_json_payload(self) -> None:
        runner = RecordingRunner(stdout='{"content": "hi there", "truncated": true}')
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("poc_claude")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "hi there")
        self.assertTrue(result.truncated)

    def test_read_pane_parses_live_nested_result_read_schema(self) -> None:
        # Redmine #13322: the live herdr CLI nests the rendered text under
        # `result.read`. The extractor must return the decoded `text` (real
        # newlines) — NOT the raw JSON envelope — so the Enter-resend composer
        # gate can whitespace-collapse and substring-match a wrapped body.
        payload = json.dumps(
            {
                "id": "cli:agent:read",
                "result": {
                    "read": {
                        "format": "text",
                        "pane_id": "w1:p8",
                        "source": "visible",
                        "text": "line one\nline two",
                        "truncated": True,
                        "workspace_id": "w1",
                    },
                    "type": "agent_read",
                },
            }
        )
        runner = RecordingRunner(stdout=payload)
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("w1:p8")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "line one\nline two")
        self.assertTrue(result.truncated)

    def test_live_nested_read_lets_resend_gate_match_wrapped_body(self) -> None:
        # Redmine #13322 end-to-end at the parse boundary: a body the composer
        # wrapped across lines (a real newline inside the JSON `text`) must still
        # be recognised as retained once decoded — this is what authorises the
        # bounded Enter-resend. Against the pre-fix raw-JSON fallback the escaped
        # \n broke the whitespace-collapse match and enter_resends stayed 0.
        body = "MARKER inject body that the composer wrapped onto two lines"
        rendered_text = "› MARKER inject body that the\ncomposer wrapped onto two lines"
        payload = json.dumps(
            {"result": {"read": {"source": "visible", "text": rendered_text, "truncated": False}}}
        )
        runner = RecordingRunner(stdout=payload)
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("w1:p8")
        self.assertTrue(result.ok)
        self.assertTrue(composer_retains_body(result.content, body))
        # And the pre-fix behaviour (searching the raw JSON envelope) would NOT
        # have matched, so this pins the regression the fix closes.
        self.assertFalse(composer_retains_body(payload, body))


class FailClosedTest(unittest.TestCase):
    def test_invalid_target_never_spawns(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.send_text("bad target", "x").reason, REASON_INVALID_TARGET)
        self.assertEqual(transport.send_keys("--flag", "enter").reason, REASON_INVALID_TARGET)
        self.assertEqual(transport.read_pane("a;b").reason, REASON_INVALID_TARGET)
        self.assertEqual(runner.calls, [])

    def test_invalid_source_never_spawns(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.read_pane("w1:p1", source="nope").reason, REASON_INVALID_SOURCE)
        self.assertEqual(runner.calls, [])

    def test_non_str_source_fails_closed_without_spawn(self) -> None:
        # Finding 1 (j#72296): an unhashable / non-str source must not raise a
        # TypeError from the membership test; it fails closed as invalid_source
        # and never spawns a subprocess.
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        for bad in ([], {}, 5, None, ("visible",)):
            with self.subTest(bad=bad):
                result = transport.read_pane("w1:p1", source=bad)
                self.assertFalse(result.ok)
                self.assertEqual(result.reason, REASON_INVALID_SOURCE)
        self.assertEqual(runner.calls, [])

    def test_bad_lines_never_spawns(self) -> None:
        runner = RecordingRunner()
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.read_pane("w1:p1", lines=0).reason, REASON_INVALID_TARGET)
        self.assertEqual(transport.read_pane("w1:p1", lines=True).reason, REASON_INVALID_TARGET)
        self.assertEqual(runner.calls, [])

    def test_nonzero_exit_is_transport_error(self) -> None:
        runner = RecordingRunner(returncode=1, stderr="no such pane")
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.send_text("w1:p1", "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)
        self.assertIn("no such pane", result.detail)

    def test_read_nonzero_exit_is_transport_error(self) -> None:
        runner = RecordingRunner(returncode=2, stderr="boom")
        transport = HerdrCliTransport(BIN, runner=runner)
        result = transport.read_pane("w1:p1")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, REASON_TRANSPORT_ERROR)

    def test_missing_binary_is_binary_not_found(self) -> None:
        runner = RecordingRunner(raises=FileNotFoundError())
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.send_text("w1:p1", "x").reason, REASON_BINARY_NOT_FOUND)
        self.assertEqual(transport.read_pane("w1:p1").reason, REASON_BINARY_NOT_FOUND)

    def test_timeout_is_transport_error(self) -> None:
        runner = RecordingRunner(raises=subprocess.TimeoutExpired(BIN, 10))
        transport = HerdrCliTransport(BIN, runner=runner)
        self.assertEqual(transport.send_keys("w1:p1", "enter").reason, REASON_TRANSPORT_ERROR)

    def test_empty_binary_rejected(self) -> None:
        with self.assertRaises(TerminalTransportError):
            HerdrCliTransport("")

    def test_transport_satisfies_protocol(self) -> None:
        self.assertIsInstance(HerdrCliTransport(BIN, runner=RecordingRunner()), TerminalTransportPort)


class ResolverTest(unittest.TestCase):
    def test_default_tmux_returns_none(self) -> None:
        self.assertIsNone(resolve_terminal_transport(TerminalTransportConfig.default(), env={}))

    def test_tmux_ignores_binary_env(self) -> None:
        self.assertIsNone(
            resolve_terminal_transport(
                TerminalTransportConfig(backend="tmux"), env={HERDR_BINARY_ENV: BIN}
            )
        )

    def test_herdr_without_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_terminal_transport(TerminalTransportConfig(backend=BACKEND_HERDR), env={})
        self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_herdr_with_unresolvable_binary_fails_closed(self) -> None:
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: "/nonexistent/path/to/herdr"},
            )
        self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)

    def test_herdr_with_resolvable_binary_returns_transport(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binpath = os.path.join(tmp, "herdr")
            with open(binpath, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IXUSR)
            transport = resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: binpath},
            )
            self.assertIsInstance(transport, HerdrCliTransport)
            self.assertEqual(transport.backend, BACKEND_HERDR)

    def test_none_config_defaults_to_off(self) -> None:
        self.assertIsNone(resolve_terminal_transport(None, env={}))

    def test_bare_name_resolves_on_trusted_env_path(self) -> None:
        # Finding 2 (j#72296): a bare binary name resolves against the *supplied
        # trusted env's* PATH, not the ambient process PATH.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            binpath = os.path.join(tmp, "herdr")
            with open(binpath, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(binpath, os.stat(binpath).st_mode | stat.S_IXUSR)
            transport = resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR),
                env={HERDR_BINARY_ENV: "herdr", "PATH": tmp},
            )
            self.assertIsInstance(transport, HerdrCliTransport)
            # Resolved to the executable inside the trusted-env PATH dir.
            self.assertEqual(transport._binary, binpath)

    def test_bare_name_not_on_trusted_env_path_fails_closed(self) -> None:
        # Finding 2 (j#72296): a bare name present only on the *ambient* PATH but
        # absent from the trusted-env PATH is NOT resolved — fail closed.
        import tempfile

        with tempfile.TemporaryDirectory() as ambient, tempfile.TemporaryDirectory() as trusted:
            # Put an executable ``herdr`` on the ambient PATH only.
            ambient_bin = os.path.join(ambient, "herdr")
            with open(ambient_bin, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(ambient_bin, os.stat(ambient_bin).st_mode | stat.S_IXUSR)
            prev_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ambient + os.pathsep + prev_path
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_terminal_transport(
                        TerminalTransportConfig(backend=BACKEND_HERDR),
                        # trusted PATH points at an empty dir (no herdr)
                        env={HERDR_BINARY_ENV: "herdr", "PATH": trusted},
                    )
                self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)
            finally:
                os.environ["PATH"] = prev_path

    def test_bare_name_env_without_path_fails_closed(self) -> None:
        # Finding 2 residual (j#72305): a supplied trusted env with NO ``PATH``
        # key must not fall back to the ambient ``PATH``. A bare name is
        # unresolvable against the empty path — fail closed.
        import tempfile

        with tempfile.TemporaryDirectory() as ambient:
            # Put an executable ``herdr`` on the ambient PATH only.
            ambient_bin = os.path.join(ambient, "herdr")
            with open(ambient_bin, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(ambient_bin, os.stat(ambient_bin).st_mode | stat.S_IXUSR)
            prev_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ambient + os.pathsep + prev_path
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_terminal_transport(
                        TerminalTransportConfig(backend=BACKEND_HERDR),
                        # trusted env carries no PATH key at all
                        env={HERDR_BINARY_ENV: "herdr"},
                    )
                self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)
            finally:
                os.environ["PATH"] = prev_path


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TrustedBinaryResolutionTest(unittest.TestCase):
    """The #13496 trusted-env resolution order: explicit env -> trusted PATH herdr.

    Pins the shared :func:`resolve_herdr_binary` — resolution order, structured
    provenance, realpath / executable verification, absolute injection, and the
    fail-closed cases (missing / non-executable / repo-local cwd rejection). No
    subprocess spawns; every binary is a temp file.
    """

    def test_env_unset_falls_back_to_trusted_path_herdr(self) -> None:
        # Redmine #13500 baseline bug: env WITHOUT MOZYO_HERDR_BINARY but with an
        # executable `herdr` on the trusted PATH now resolves (was binary_unconfigured).
        with tempfile.TemporaryDirectory() as tmp:
            binpath = Path(tmp) / HERDR_PATH_NAME
            _make_executable(binpath)
            resolution = resolve_herdr_binary({"PATH": tmp})
            self.assertIsInstance(resolution, HerdrBinaryResolution)
            self.assertEqual(resolution.source, BINARY_SOURCE_PATH)
            self.assertEqual(resolution.path, str(binpath))
            self.assertTrue(os.path.isabs(resolution.path))
            # And the transport resolver rides the same order end to end.
            transport = resolve_terminal_transport(
                TerminalTransportConfig(backend=BACKEND_HERDR), env={"PATH": tmp}
            )
            self.assertIsInstance(transport, HerdrCliTransport)
            self.assertEqual(transport.binary, str(binpath))

    def test_env_unset_and_no_path_key_is_unconfigured(self) -> None:
        # No explicit value and no PATH key at all -> nothing to resolve from either
        # trusted source. env={} must stay binary_unconfigured (all six sites rely
        # on this so `env={}` never silently resolves an ambient/cwd herdr).
        with self.assertRaises(TerminalTransportError) as ctx:
            resolve_herdr_binary({})
        self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_env_unset_trusted_path_without_herdr_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary({"PATH": tmp})
            self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_explicit_env_takes_precedence_over_trusted_path(self) -> None:
        # Both an explicit MOZYO_HERDR_BINARY and a trusted-PATH herdr are present
        # and DIFFERENT; resolution order is env-first (source=env).
        with tempfile.TemporaryDirectory() as env_dir, tempfile.TemporaryDirectory() as path_dir:
            explicit = Path(env_dir) / "explicit-herdr"
            _make_executable(explicit)
            path_herdr = Path(path_dir) / HERDR_PATH_NAME
            _make_executable(path_herdr)
            resolution = resolve_herdr_binary(
                {HERDR_BINARY_ENV: str(explicit), "PATH": path_dir}
            )
            self.assertEqual(resolution.source, BINARY_SOURCE_ENV)
            self.assertEqual(resolution.path, str(explicit))

    def test_explicit_env_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binpath = Path(tmp) / "fake-herdr"
            _make_executable(binpath)
            resolution = resolve_herdr_binary({HERDR_BINARY_ENV: str(binpath)})
            self.assertEqual(resolution.source, BINARY_SOURCE_ENV)
            self.assertEqual(resolution.path, str(binpath))

    def test_non_executable_trusted_path_file_fails_closed(self) -> None:
        # A `herdr` on the trusted PATH that is NOT executable is not resolved
        # (shutil.which requires X_OK) -> unconfigured, never a silent success.
        with tempfile.TemporaryDirectory() as tmp:
            not_exec = Path(tmp) / HERDR_PATH_NAME
            not_exec.write_text("#!/bin/sh\n", encoding="utf-8")  # no chmod +x
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary({"PATH": tmp})
            self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)

    def test_non_executable_env_binary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_exec = Path(tmp) / "fake-herdr"
            not_exec.write_text("#!/bin/sh\n", encoding="utf-8")  # no chmod +x
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary({HERDR_BINARY_ENV: str(not_exec)})
            self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)

    def test_symlink_env_binary_records_realpath(self) -> None:
        # realpath verification (#13496): an env value that is a symlink to a real
        # executable resolves; `path` is the symlink's absolute path (injected),
        # `realpath` is the symlink-resolved real executable that was verified.
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real-herdr"
            _make_executable(real)
            link = Path(tmp) / "link-herdr"
            os.symlink(real, link)
            resolution = resolve_herdr_binary({HERDR_BINARY_ENV: str(link)})
            # `path` is the symlink's own absolute path (what gets injected);
            # `realpath` is the symlink-resolved real executable that was verified.
            self.assertEqual(resolution.path, str(link))
            self.assertEqual(resolution.realpath, os.path.realpath(str(link)))
            self.assertEqual(resolution.realpath, os.path.realpath(str(real)))
            self.assertNotEqual(resolution.path, resolution.realpath)

    def test_dangling_symlink_env_binary_fails_closed(self) -> None:
        # A symlink whose target does not exist has no executable real file — the
        # realpath verify fails closed rather than trusting the dangling link.
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "link-herdr"
            os.symlink(Path(tmp) / "does-not-exist", link)
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary({HERDR_BINARY_ENV: str(link)})
            self.assertEqual(ctx.exception.reason, REASON_BINARY_NOT_FOUND)

    def test_repo_local_cwd_herdr_is_never_a_source(self) -> None:
        # Security boundary (#13502): with no explicit env value and no trusted PATH
        # key, an executable `herdr` sitting in the CURRENT WORKING DIRECTORY must
        # NOT be picked up — a hostile checkout cannot point the runtime at its own
        # binary. Fails closed as binary_unconfigured.
        with tempfile.TemporaryDirectory() as tmp:
            hostile = Path(tmp) / HERDR_PATH_NAME
            _make_executable(hostile)
            prev_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_herdr_binary({})
                self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)
                # Even a bare-name explicit env value only resolves on the trusted
                # PATH, never the cwd, so a cwd herdr stays unreachable.
                with self.assertRaises(TerminalTransportError) as ctx2:
                    resolve_herdr_binary({HERDR_BINARY_ENV: HERDR_PATH_NAME})
                self.assertEqual(ctx2.exception.reason, REASON_BINARY_NOT_FOUND)
            finally:
                os.chdir(prev_cwd)

    def test_ambient_path_is_not_a_source(self) -> None:
        # An executable `herdr` on the AMBIENT process PATH but absent from the
        # supplied trusted env is not resolved (the trusted env is the authority).
        with tempfile.TemporaryDirectory() as ambient:
            ambient_bin = Path(ambient) / HERDR_PATH_NAME
            _make_executable(ambient_bin)
            prev_path = os.environ.get("PATH", "")
            os.environ["PATH"] = ambient + os.pathsep + prev_path
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    # trusted env carries no PATH key at all
                    resolve_herdr_binary({})
                self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)
            finally:
                os.environ["PATH"] = prev_path


class TrustedPathSafetyTest(unittest.TestCase):
    """Review j#74773: an unsafe (empty/relative) PATH component fails closed as a
    WHOLE — never silently skipped so a later absolute candidate resolves."""

    def test_relative_path_component_dot_fails_closed(self) -> None:
        # A trusted PATH of `.` is cwd-dependent -> structured failure, not a search.
        with tempfile.TemporaryDirectory() as tmp:
            _make_executable(Path(tmp) / HERDR_PATH_NAME)
            prev_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_herdr_binary({"PATH": "."})
                self.assertEqual(ctx.exception.reason, REASON_BINARY_UNSAFE_PATH)
            finally:
                os.chdir(prev_cwd)

    def test_empty_path_components_fail_closed(self) -> None:
        # Leading / middle / trailing empty components (`:`) each mean "cwd" to a
        # POSIX shell; ANY of them rejects the whole trusted PATH.
        with tempfile.TemporaryDirectory() as safe:
            for path_val in (f":{safe}", f"{safe}:", f"{safe}::{safe}"):
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_herdr_binary({"PATH": path_val})
                self.assertEqual(
                    ctx.exception.reason, REASON_BINARY_UNSAFE_PATH, path_val
                )

    def test_unsafe_component_beside_safe_candidate_still_fails_closed(self) -> None:
        # The core of j#74773: even when a SAFE absolute component holds a real
        # herdr, an unsafe sibling component fails the whole resolution closed —
        # the resolver never silently drops the unsafe one and uses the safe one.
        with tempfile.TemporaryDirectory() as safe:
            _make_executable(Path(safe) / HERDR_PATH_NAME)
            for path_val in (f".:{safe}", f"{safe}:", f"{safe}::{safe}", f".{os.pathsep}{safe}"):
                with self.assertRaises(TerminalTransportError) as ctx:
                    resolve_herdr_binary({"PATH": path_val})
                self.assertEqual(
                    ctx.exception.reason, REASON_BINARY_UNSAFE_PATH, path_val
                )

    def test_relative_pathshaped_explicit_env_fails_closed(self) -> None:
        # A path-shaped but RELATIVE explicit MOZYO_HERDR_BINARY (e.g. `./herdr`) is
        # cwd-dependent, so it fails closed as unsafe rather than resolving.
        with tempfile.TemporaryDirectory() as tmp:
            _make_executable(Path(tmp) / HERDR_PATH_NAME)
            prev_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                for rel in (f".{os.sep}{HERDR_PATH_NAME}", f"sub{os.sep}herdr"):
                    with self.assertRaises(TerminalTransportError) as ctx:
                        resolve_herdr_binary({HERDR_BINARY_ENV: rel})
                    self.assertEqual(
                        ctx.exception.reason, REASON_BINARY_UNSAFE_PATH, rel
                    )
            finally:
                os.chdir(prev_cwd)

    def test_empty_path_string_is_unconfigured_not_unsafe(self) -> None:
        # A present-but-empty PATH string (and a missing PATH key) is "no search
        # dir", NOT an unsafe component -> unconfigured, so env={} is unchanged.
        for env in ({"PATH": ""}, {}):
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary(env)
            self.assertEqual(ctx.exception.reason, REASON_BINARY_UNCONFIGURED)


class TrustedPathAmbiguityTest(unittest.TestCase):
    """Review F2 (#13502 j#74764): >1 distinct trusted-PATH herdr fails closed."""

    def test_two_distinct_herdr_on_trusted_path_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _make_executable(Path(a) / HERDR_PATH_NAME)
            _make_executable(Path(b) / HERDR_PATH_NAME)
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary({"PATH": a + os.pathsep + b})
            self.assertEqual(ctx.exception.reason, REASON_BINARY_AMBIGUOUS)

    def test_ambiguity_also_detected_for_bare_name_env(self) -> None:
        # A bare-name explicit MOZYO_HERDR_BINARY resolved on a PATH with two
        # distinct herdr is equally ambiguous (same enumeration path).
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _make_executable(Path(a) / HERDR_PATH_NAME)
            _make_executable(Path(b) / HERDR_PATH_NAME)
            with self.assertRaises(TerminalTransportError) as ctx:
                resolve_herdr_binary(
                    {HERDR_BINARY_ENV: HERDR_PATH_NAME, "PATH": a + os.pathsep + b}
                )
            self.assertEqual(ctx.exception.reason, REASON_BINARY_AMBIGUOUS)

    def test_same_realpath_via_symlink_is_not_ambiguous(self) -> None:
        # Two PATH entries whose `herdr` resolve to the SAME realpath (a symlink to
        # the real file) are one binary, not an ambiguity — it resolves.
        with tempfile.TemporaryDirectory() as real_dir, tempfile.TemporaryDirectory() as link_dir:
            real = Path(real_dir) / HERDR_PATH_NAME
            _make_executable(real)
            os.symlink(real, Path(link_dir) / HERDR_PATH_NAME)
            resolution = resolve_herdr_binary(
                {"PATH": real_dir + os.pathsep + link_dir}
            )
            self.assertEqual(resolution.source, BINARY_SOURCE_PATH)
            self.assertEqual(resolution.realpath, os.path.realpath(str(real)))

    def test_duplicate_path_entry_is_not_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _make_executable(Path(tmp) / HERDR_PATH_NAME)
            resolution = resolve_herdr_binary({"PATH": tmp + os.pathsep + tmp})
            self.assertEqual(resolution.source, BINARY_SOURCE_PATH)


class HerdrBinaryResolutionRecordTest(unittest.TestCase):
    """The structured resolution record fails closed on a malformed construction."""

    def test_rejects_unknown_source(self) -> None:
        with self.assertRaises(TerminalTransportError):
            HerdrBinaryResolution(path="/x/herdr", realpath="/x/herdr", source="cwd")

    def test_rejects_none_source(self) -> None:
        # `none` is the fail-closed provenance; a *resolved* record may never carry it.
        with self.assertRaises(TerminalTransportError):
            HerdrBinaryResolution(path="/x/herdr", realpath="/x/herdr", source="none")

    def test_rejects_empty_path(self) -> None:
        with self.assertRaises(TerminalTransportError):
            HerdrBinaryResolution(path="", realpath="/x/herdr", source=BINARY_SOURCE_ENV)

    def test_accepts_valid_record(self) -> None:
        rec = HerdrBinaryResolution(
            path="/x/herdr", realpath="/real/herdr", source=BINARY_SOURCE_PATH
        )
        self.assertEqual(rec.source, BINARY_SOURCE_PATH)


if __name__ == "__main__":
    unittest.main()
