"""AF_UNIX endpoint-path budget preflight tests (Redmine #14657).

The defect these pin (#14185 R3 live smoke j#91992): the disposable endpoint path was
216 bytes, over the host's ``sun_path`` capacity, so the *server child's* own ``bind()``
raised ``OSError: AF_UNIX path too long`` into ``stderr=DEVNULL`` and the run reported
the generic ``did not become ready within the bounded startup window``.  An operator
could not tell an unbindable path from a slow server.

Two things therefore have to be true, and each is asserted with its counterpart so
neither can pass vacuously:

* the budget is **measured on this host**, not read from a per-platform length table —
  so :class:`HostBudgetProbeTests` proves the measured number is the exact boundary a
  real ``bind()`` draws, and that an unanswerable probe reports "unmeasured" rather than
  a fabricated length;
* the verdict is **fail-closed and value-free** — over budget and unmeasured are
  distinct blockers, an empty derivation is refused rather than answered, and no path
  reaches the evidence or the message.

Nothing here starts a Herdr server or reads ``HERDR_SOCKET_PATH``; every socket bound is
bound inside a scratch directory the test owns and is unlinked again.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    af_unix_endpoint_budget as budget_module,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402,E501
    disposable_shared_space_smoke as driver_module,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.af_unix_endpoint_budget import (  # noqa: E402,E501
    BUDGET_UNMEASURED,
    CLIENT_SOCKET_NAME,
    ENDPOINT_PATH_BUDGET_UNMEASURED,
    ENDPOINT_PATH_OK,
    ENDPOINT_PATH_TOO_LONG,
    MAX_PROBE_NAME_BYTES,
    SERVER_SOCKET_NAME,
    SmokeEndpointPathBudgetError,
    derived_endpoint_paths,
    disposable_instance_root,
    endpoint_path_budget_for_isolated_home,
    endpoint_path_refusal,
    evaluate_endpoint_paths,
    host_af_unix_path_budget,
    path_bytes,
    probe_af_unix_path_budget,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.disposable_herdr_instance import (  # noqa: E402,E501
    DisposableHerdrInstance,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.shared_space_smoke_observation import (  # noqa: E402,E501
    SharedSpaceSmokeError,
)
from tests.support.private_path_fixtures import macos_home_path  # noqa: E402


def _over_budget_root(base: Path, budget: int) -> Path:
    """A real directory deep enough that its derived endpoint path exceeds ``budget``."""
    root = base.resolve()
    while path_bytes(root / CLIENT_SOCKET_NAME) <= budget:
        root = root / ("d" * 32)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _directory_over_budget(base: Path, budget: int) -> Path:
    """A real directory whose OWN path already exceeds ``budget``.

    Stronger than :func:`_over_budget_root`: no name at all can be appended within the
    budget, which is the case where the probe cannot establish a figure.
    """
    directory = base.resolve()
    while path_bytes(directory) <= budget:
        directory = directory / ("d" * 32)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class HostBudgetProbeTests(unittest.TestCase):
    """The number must come from the runtime that binds, not from a length table."""

    def test_the_measured_budget_is_the_exact_boundary_a_real_bind_draws(self) -> None:
        """The whole premise: ``budget`` binds and ``budget + 1`` does not.

        Were the budget a hardcoded 104/108 guess, this would be the assertion that
        broke first on a host whose runtime draws the NUL-byte line elsewhere.
        """
        budget = probe_af_unix_path_budget()
        self.assertGreater(
            budget, 0, "the probe must answer on a host that can bind AF_UNIX at all"
        )
        with tempfile.TemporaryDirectory() as tmp:
            room = budget - path_bytes(tmp) - 1
            self.assertGreaterEqual(room, 1, "premise: the scratch dir leaves room")
            self.assertLessEqual(room, MAX_PROBE_NAME_BYTES)

            fits = Path(tmp) / ("a" * room)
            self.assertEqual(path_bytes(fits), budget)
            budget_module._bind_and_unlink(fits)  # must not raise
            self.assertFalse(
                fits.exists(), "the probe unlinks every node it binds"
            )

            over = Path(tmp) / ("a" * (room + 1))
            with self.assertRaises(OSError) as caught:
                budget_module._bind_and_unlink(over)
            self.assertTrue(
                budget_module._is_over_budget(caught.exception),
                "one byte past the measured budget must be classed as over-long",
            )
            self.assertFalse(over.exists())

    def test_a_scratch_directory_over_the_budget_is_unmeasured_not_zero(self) -> None:
        """"Not even a one-byte name fits" is unknown, never a budget of zero."""
        budget = probe_af_unix_path_budget()
        self.assertGreater(budget, 0)
        with tempfile.TemporaryDirectory() as tmp:
            deep = _directory_over_budget(Path(tmp), budget)
            self.assertGreater(
                path_bytes(deep), budget, "premise: no name fits inside this directory"
            )
            self.assertEqual(
                probe_af_unix_path_budget(scratch_dir=deep), BUDGET_UNMEASURED
            )

    def test_a_probe_that_fails_for_another_reason_is_unmeasured(self) -> None:
        """A permission / sandbox failure is not a length answer."""

        def denied(path):
            raise PermissionError(errno.EACCES, "injected")

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                probe_af_unix_path_budget(scratch_dir=tmp, binder=denied),
                BUDGET_UNMEASURED,
            )
            self.assertEqual(
                sorted(os.listdir(tmp)), [], "a refused probe creates nothing"
            )

    def test_the_over_budget_classification_reads_the_runtime_contract(self) -> None:
        """``errno``-based, not message-based; every other ``OSError`` is not a length."""
        self.assertTrue(budget_module._is_over_budget(OSError("AF_UNIX path too long")))
        self.assertTrue(
            budget_module._is_over_budget(OSError(errno.ENAMETOOLONG, "too long"))
        )
        for other in (errno.EACCES, errno.ENOENT, errno.EADDRINUSE, errno.EPERM):
            with self.subTest(errno=other):
                self.assertFalse(
                    budget_module._is_over_budget(OSError(other, "unrelated"))
                )

    def test_the_probe_leaves_no_socket_and_no_scratch_tree_behind(self) -> None:
        bound: list = []
        real = budget_module._bind_and_unlink

        def recording(path):
            bound.append(Path(path))
            real(path)

        with tempfile.TemporaryDirectory() as tmp:
            self.assertGreater(
                probe_af_unix_path_budget(scratch_dir=tmp, binder=recording), 0
            )
            self.assertTrue(bound, "premise: the probe really bound something")
            for path in bound:
                self.assertEqual(
                    path.parent,
                    Path(tmp),
                    "the probe may only bind inside the scratch directory it was given",
                )
            self.assertEqual(sorted(os.listdir(tmp)), [], "no bound socket may survive")

        created: list = []
        real_mkdtemp = budget_module.tempfile.mkdtemp

        def recording_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created.append(Path(path))
            return path

        with mock.patch.object(
            budget_module.tempfile, "mkdtemp", recording_mkdtemp
        ):
            self.assertGreater(probe_af_unix_path_budget(), 0)
        self.assertEqual(len(created), 1)
        self.assertFalse(
            created[0].exists(), "the scratch tree the probe made is removed again"
        )

    def test_the_probe_touches_no_operator_endpoint_and_no_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            poison = Path(tmp) / "operator-poison.sock"
            with mock.patch.dict(
                os.environ, {"HERDR_SOCKET_PATH": str(poison)}, clear=False
            ):
                before = dict(os.environ)
                self.assertGreater(probe_af_unix_path_budget(), 0)
                self.assertEqual(dict(os.environ), before)
            self.assertFalse(
                poison.exists(), "the probe must never create an operator endpoint"
            )

    def test_the_host_budget_is_probed_once_and_matches_a_fresh_probe(self) -> None:
        calls: list = []
        real = budget_module.probe_af_unix_path_budget

        def counting(**kwargs):
            calls.append(1)
            return real(**kwargs)

        with mock.patch.object(budget_module, "_HOST_BUDGET", None):
            with mock.patch.object(
                budget_module, "probe_af_unix_path_budget", counting
            ):
                first = host_af_unix_path_budget()
                second = host_af_unix_path_budget()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1, "a host property is not re-measured per call")
        self.assertEqual(first, real())


class EndpointPathEvaluationTests(unittest.TestCase):
    """The verdict itself: boundary-inclusive, fail-closed, value-free."""

    def _paths(self, root: str = "/tmp/short") -> tuple:
        return derived_endpoint_paths(Path(root))

    def test_exactly_at_the_budget_is_accepted_and_one_byte_over_is_refused(self) -> None:
        paths = self._paths()
        longest = max(path_bytes(path) for path in paths)

        fits = evaluate_endpoint_paths(paths, budget_bytes=longest)
        self.assertTrue(fits.within_budget)
        self.assertEqual(fits.blocker, ENDPOINT_PATH_OK)
        self.assertEqual(fits.path_bytes, longest)

        over = evaluate_endpoint_paths(paths, budget_bytes=longest - 1)
        self.assertFalse(over.within_budget)
        self.assertEqual(over.blocker, ENDPOINT_PATH_TOO_LONG)

    def test_the_longest_derived_path_is_the_one_measured(self) -> None:
        """Measuring only the server socket would admit an over-budget client socket."""
        server, client = self._paths()
        self.assertEqual(server.name, SERVER_SOCKET_NAME)
        self.assertEqual(client.name, CLIENT_SOCKET_NAME)
        self.assertGreater(
            path_bytes(client), path_bytes(server), "premise: the names differ in length"
        )
        verdict = evaluate_endpoint_paths(
            self._paths(), budget_bytes=path_bytes(server)
        )
        self.assertEqual(verdict.path_bytes, path_bytes(client))
        self.assertEqual(verdict.blocker, ENDPOINT_PATH_TOO_LONG)

    def test_an_unmeasured_budget_is_its_own_blocker(self) -> None:
        verdict = evaluate_endpoint_paths(
            self._paths(), budget_bytes=BUDGET_UNMEASURED
        )
        self.assertEqual(verdict.blocker, ENDPOINT_PATH_BUDGET_UNMEASURED)
        self.assertFalse(
            verdict.within_budget,
            "a budget we could not measure promises nothing about the path",
        )

    def test_an_empty_derivation_is_refused_rather_than_vacuously_ok(self) -> None:
        """A caller that lost its derivation must not be answered "fits"."""
        with self.assertRaises(SharedSpaceSmokeError):
            evaluate_endpoint_paths([], budget_bytes=4096)

    def test_the_length_is_counted_in_bytes_not_characters(self) -> None:
        root = Path("/tmp") / ("あ" * 12)
        verdict = evaluate_endpoint_paths(
            derived_endpoint_paths(root), budget_bytes=4096
        )
        self.assertEqual(
            verdict.path_bytes, path_bytes(root / CLIENT_SOCKET_NAME)
        )
        self.assertGreater(
            verdict.path_bytes,
            len(str(root / CLIENT_SOCKET_NAME)),
            "sun_path bounds bytes, so a multi-byte home must not be under-counted",
        )

    def test_raise_if_blocked_is_typed_and_names_the_constraint_and_resolution(self) -> None:
        blocked = evaluate_endpoint_paths(self._paths(), budget_bytes=4)
        with self.assertRaises(SmokeEndpointPathBudgetError) as caught:
            blocked.raise_if_blocked()
        message = str(caught.exception)
        self.assertEqual(caught.exception.blocker, ENDPOINT_PATH_TOO_LONG)
        self.assertIn(ENDPOINT_PATH_TOO_LONG, message)
        self.assertIn("endpoint_path_budget_bytes=4", message)
        self.assertIn(f"endpoint_path_bytes={blocked.path_bytes}", message)
        self.assertIn("--isolated-home", message, "the resolution must be named")
        self.assertIsInstance(caught.exception, SharedSpaceSmokeError)

    def test_a_within_budget_verdict_raises_nothing(self) -> None:
        """Baseline: the fence must not be a blanket refusal."""
        evaluate_endpoint_paths(self._paths(), budget_bytes=4096).raise_if_blocked()

    def test_no_path_reaches_the_evidence_or_the_message(self) -> None:
        root = Path(macos_home_path("someone", "secret-project", "deep", "tree"))
        blocked = evaluate_endpoint_paths(derived_endpoint_paths(root), budget_bytes=4)
        rendered = repr(blocked.as_evidence()) + endpoint_path_refusal(blocked)
        for leak in (str(root), "someone", "secret-project", macos_home_path("")):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, rendered)
        self.assertGreater(
            blocked.path_bytes, 0, "the byte count itself is still reported"
        )

    def test_the_evidence_key_set_does_not_depend_on_the_verdict(self) -> None:
        fits = evaluate_endpoint_paths(self._paths(), budget_bytes=4096).as_evidence()
        blocked = evaluate_endpoint_paths(self._paths(), budget_bytes=4).as_evidence()
        self.assertEqual(sorted(fits), sorted(blocked))
        self.assertEqual(
            sorted(fits),
            [
                "endpoint_path_blocker",
                "endpoint_path_budget_bytes",
                "endpoint_path_bytes",
                "endpoint_path_within_budget",
            ],
        )
        self.assertIsInstance(fits["endpoint_path_within_budget"], bool)
        self.assertIsInstance(fits["endpoint_path_bytes"], int)
        self.assertEqual(fits["endpoint_path_blocker"], ENDPOINT_PATH_OK)


class DerivationDriftTests(unittest.TestCase):
    """The measured paths must be the paths that actually get bound."""

    def _instance(self, root: Path, **kwargs) -> DisposableHerdrInstance:
        return DisposableHerdrInstance(
            binary="/bin/true",
            root=root,
            base_env={"HOME": str(root.parent / "operator")},
            runner=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "[]", ""),
            popen_factory=lambda argv, **k: None,
            sleeper=lambda _s: None,
            ambient_env={},
            **kwargs,
        )

    def test_the_derived_paths_are_the_ones_the_lifecycle_binds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = self._instance(Path(tmp) / "instance")
            self.assertEqual(
                set(derived_endpoint_paths(instance.root)),
                {instance.binding.socket_path, instance.binding.client_socket_path},
            )

    def test_the_isolated_home_derivation_matches_the_lifecycle_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "iso"
            instance = self._instance(disposable_instance_root(home))
            self.assertEqual(instance.root, disposable_instance_root(home))
            self.assertEqual(
                endpoint_path_budget_for_isolated_home(home).path_bytes,
                instance.endpoint_path_budget.path_bytes,
            )


class DriverRefusalTests(unittest.TestCase):
    """The driver refuses before the binary, the home or any server exists (#14657)."""

    def test_an_over_budget_home_refuses_before_the_binary_is_resolved(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure import (  # noqa: E501
            herdr_transport,
        )

        resolved: list = []

        def refuse_to_resolve(env):
            resolved.append(1)
            raise AssertionError("the binary must not be resolved on a blocked path")

        budget = host_af_unix_path_budget()
        with tempfile.TemporaryDirectory() as tmp:
            home = _over_budget_root(Path(tmp), budget)
            with mock.patch.object(
                herdr_transport, "resolve_herdr_binary", refuse_to_resolve
            ):
                with self.assertRaises(SmokeEndpointPathBudgetError) as caught:
                    driver_module.run_disposable_shared_space_smoke(home, env={})
            self.assertEqual(caught.exception.blocker, ENDPOINT_PATH_TOO_LONG)
            self.assertEqual(resolved, [], "zero actuation: nothing was resolved")
            self.assertFalse(
                disposable_instance_root(home).exists(),
                "no instance tree may be created by a refused run",
            )

    def test_the_driver_refusal_uses_the_same_derivation_as_the_preflight(self) -> None:
        """Baseline / drift guard: a short home is admitted by both."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "iso"
            self.assertTrue(
                endpoint_path_budget_for_isolated_home(home).within_budget,
                "premise: a short isolated home is bindable, so the fence is not blanket",
            )


if __name__ == "__main__":
    unittest.main()
