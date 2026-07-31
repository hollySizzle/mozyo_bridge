"""Cross-process endpoint-gate negative proof for the disposable smoke (#14187).

The endpoint gate's counters live on :class:`~...disposable_herdr_instance.EndpointBoundHerdrRunner`,
which means ``fork`` hands every smoke worker its own copy and the parent's
``operator_endpoint_requests == 0`` says nothing about the children — where the real
workspace/agent traffic happens (review j#85841 F1).  These two value objects carry the
proof across that boundary: :class:`EndpointGateCounters` is the per-process snapshot a
worker returns in its receipt, and :class:`EndpointGateEvidence` folds the parent's
snapshot together with one per worker.

Split out of ``disposable_herdr_instance`` to keep that module under its module-health
baseline without an allowlist bump; the lifecycle module re-exports both names, so this
is a cohesion move and not an API change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class EndpointGateCounters:
    """One process's endpoint-gate counters — the unit of the cross-process proof.

    The counters live on :class:`EndpointBoundHerdrRunner`, so ``fork`` hands every
    smoke worker its own copy.  A worker returns this snapshot to the parent; absence
    of one is a fail-closed condition, never an implicit zero (review j#85841 F1).
    """

    dispatched_calls: int
    bound_calls: int
    escape_refusals: int
    operator_endpoint_requests: int
    refusal_reasons: tuple[str, ...] = ()

    @classmethod
    def snapshot(cls, runner) -> "EndpointGateCounters":
        return cls(
            dispatched_calls=int(runner.dispatched_calls),
            bound_calls=int(runner.bound_calls),
            escape_refusals=int(runner.escape_refusals),
            operator_endpoint_requests=int(runner.operator_endpoint_requests),
            refusal_reasons=tuple(sorted(runner.refusal_reasons)),
        )

    @property
    def consistent(self) -> bool:
        """Whether this snapshot satisfies the invariants the runner maintains.

        A snapshot that was truncated, hand-built or garbled in transit fails here, and
        an inconsistent snapshot is treated exactly like a missing one — the point of
        the aggregate is that only a *readable* receipt may lower the residual risk.
        """
        counts = (
            self.dispatched_calls,
            self.bound_calls,
            self.escape_refusals,
            self.operator_endpoint_requests,
        )
        if min(counts) < 0:
            return False
        if self.bound_calls > self.dispatched_calls:
            return False
        if self.operator_endpoint_requests > self.dispatched_calls:
            return False
        # A refusal count and a refusal vocabulary must corroborate each other.
        return (self.escape_refusals > 0) == bool(self.refusal_reasons)


@dataclass(frozen=True)
class EndpointGateEvidence:
    """The endpoint-gate negative proof over every process that held the capability.

    ``processes`` counts the snapshots that were actually folded in (the parent plus
    each worker that returned one).  ``receipts_missing`` counts the workers that did
    not, and any missing or inconsistent receipt makes :attr:`proven_zero_external`
    false no matter what the readable counters say: a run whose evidence cannot be
    collected has not proven it made zero external requests, it has merely failed to
    observe them (review j#85841 F1).
    """

    processes: int
    receipts_expected: int
    receipts_missing: int
    receipts_consistent: bool
    dispatched_calls: int
    bound_calls: int
    escape_refusals: int
    operator_endpoint_requests: int
    refusal_reasons: tuple[str, ...] = ()

    @classmethod
    def aggregate(
        cls,
        *,
        parent: EndpointGateCounters,
        worker_receipts: Sequence[Optional[EndpointGateCounters]],
    ) -> "EndpointGateEvidence":
        present = [receipt for receipt in worker_receipts if receipt is not None]
        folded = [parent, *present]
        reasons: set[str] = set()
        for snapshot in folded:
            reasons.update(snapshot.refusal_reasons)
        return cls(
            processes=len(folded),
            receipts_expected=len(worker_receipts),
            receipts_missing=len(worker_receipts) - len(present),
            receipts_consistent=all(snapshot.consistent for snapshot in folded),
            dispatched_calls=sum(s.dispatched_calls for s in folded),
            bound_calls=sum(s.bound_calls for s in folded),
            escape_refusals=sum(s.escape_refusals for s in folded),
            operator_endpoint_requests=sum(s.operator_endpoint_requests for s in folded),
            refusal_reasons=tuple(sorted(reasons)),
        )

    @classmethod
    def for_single_process(cls, runner) -> "EndpointGateEvidence":
        """The parent-scope view, for a lifecycle that forked no workers."""
        return cls.aggregate(parent=EndpointGateCounters.snapshot(runner), worker_receipts=())

    @property
    def receipts_complete(self) -> bool:
        return self.receipts_missing == 0

    @property
    def all_calls_bound(self) -> bool:
        """Every dispatched call across every process carried the owned socket."""
        return (
            self.dispatched_calls > 0
            and self.bound_calls == self.dispatched_calls
            and self.escape_refusals == 0
        )

    @property
    def operator_endpoint_connected(self) -> bool:
        return self.operator_endpoint_requests > 0

    @property
    def proven_zero_external(self) -> bool:
        """Zero operator-endpoint traffic, *proven* rather than merely unobserved."""
        return (
            self.receipts_complete
            and self.receipts_consistent
            and self.operator_endpoint_requests == 0
            and self.escape_refusals == 0
        )

    def as_evidence(self) -> dict[str, object]:
        return {
            "endpoint_bound": self.all_calls_bound,
            "operator_server_connected": self.operator_endpoint_connected,
            "operator_endpoint_requests": self.operator_endpoint_requests,
            "endpoint_escape_refusals": self.escape_refusals,
            "endpoint_gate_dispatched_calls": self.dispatched_calls,
            "endpoint_gate_bound_calls": self.bound_calls,
            "endpoint_gate_processes": self.processes,
            "endpoint_gate_receipts_expected": self.receipts_expected,
            "endpoint_gate_receipts_missing": self.receipts_missing,
            "endpoint_gate_receipts_complete": self.receipts_complete,
            "endpoint_gate_receipts_consistent": self.receipts_consistent,
            "endpoint_gate_proven_zero_external": self.proven_zero_external,
            "endpoint_refusal_reasons": list(self.refusal_reasons),
        }


__all__ = ("EndpointGateCounters", "EndpointGateEvidence")
