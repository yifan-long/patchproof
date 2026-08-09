"""Hard, shared request/token/cost budgets for model evaluations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


class BudgetExceededError(RuntimeError):
    def __init__(self, reason: str, snapshot: dict[str, Any] | None = None):
        self.reason = reason
        self.snapshot = snapshot or {}
        super().__init__(reason)


@dataclass(frozen=True)
class BudgetLimits:
    max_requests: int = 40
    max_input_tokens: int = 100_000
    max_output_tokens: int = 32_768
    max_cost_usd: float = 2.0
    cost_per_million_tokens: float = 0.0
    reserve_output_tokens: int = 4096


@dataclass
class _Reservation:
    request_id: str
    input_tokens: int
    output_tokens: int
    reserved_cost_usd: float


@dataclass
class BudgetLedger:
    """A ledger shared by baseline and harness runs.

    ``reserve`` is called before every provider request and reserves the worst
    case output. ``commit`` replaces that reservation with observed usage.
    Thus a second variant cannot spend a budget already promised to the first.
    """

    limits: BudgetLimits = field(default_factory=BudgetLimits)
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    _reservations: dict[str, _Reservation] = field(default_factory=dict, repr=False)

    def reserve(
        self,
        *,
        input_tokens: int = 0,
        requested_output_tokens: int | None = None,
        request_id: str | None = None,
    ) -> str:
        request_id = request_id or uuid.uuid4().hex
        input_tokens = max(0, int(input_tokens))
        output_tokens = min(
            self.limits.max_output_tokens,
            max(1, int(requested_output_tokens or self.limits.reserve_output_tokens)),
        )
        pending = list(self._reservations.values())
        pending_requests = len(pending)
        pending_input = sum(item.input_tokens for item in pending)
        pending_output = sum(item.output_tokens for item in pending)
        pending_cost = sum(item.reserved_cost_usd for item in pending)
        reserved_cost = self._cost(input_tokens + output_tokens)
        if self.requests + pending_requests + 1 > self.limits.max_requests:
            raise BudgetExceeded("max_requests", self.snapshot())
        if self.input_tokens + pending_input + input_tokens > self.limits.max_input_tokens:
            raise BudgetExceeded("max_input_tokens", self.snapshot())
        if self.output_tokens + pending_output + output_tokens > self.limits.max_output_tokens:
            raise BudgetExceeded("max_output_tokens", self.snapshot())
        if self.cost_usd + pending_cost + reserved_cost > self.limits.max_cost_usd + 1e-12:
            raise BudgetExceeded("max_cost_usd", self.snapshot())
        self._reservations[request_id] = _Reservation(request_id, input_tokens, output_tokens, reserved_cost)
        return request_id

    def commit(
        self,
        request_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> dict[str, Any]:
        reservation = self._reservations.pop(request_id, None)
        if reservation is None:
            raise BudgetExceeded("unknown_or_already_committed_request", self.snapshot())
        observed_input = max(0, int(input_tokens))
        observed_output = max(0, int(output_tokens))
        next_requests = self.requests + 1
        next_input = self.input_tokens + observed_input
        next_output = self.output_tokens + observed_output
        next_cost = self.cost_usd + self._cost(observed_input + observed_output)
        if next_requests > self.limits.max_requests:
            raise BudgetExceeded("max_requests", self.snapshot())
        if next_input > self.limits.max_input_tokens:
            raise BudgetExceeded("max_input_tokens", self.snapshot())
        if next_output > self.limits.max_output_tokens:
            raise BudgetExceeded("max_output_tokens", self.snapshot())
        if next_cost > self.limits.max_cost_usd + 1e-12:
            raise BudgetExceeded("max_cost_usd", self.snapshot())
        self.requests = next_requests
        self.input_tokens = next_input
        self.output_tokens = next_output
        self.cost_usd = next_cost
        return self.snapshot()

    def cancel(self, request_id: str) -> None:
        self._reservations.pop(request_id, None)

    def can_reserve(self, *, input_tokens: int = 0, requested_output_tokens: int | None = None) -> bool:
        try:
            request_id = self.reserve(
                input_tokens=input_tokens,
                requested_output_tokens=requested_output_tokens,
            )
        except BudgetExceeded:
            return False
        self.cancel(request_id)
        return True

    def snapshot(self) -> dict[str, Any]:
        reserved = list(self._reservations.values())
        return {
            "limits": {
                "max_requests": self.limits.max_requests,
                "max_input_tokens": self.limits.max_input_tokens,
                "max_output_tokens": self.limits.max_output_tokens,
                "max_cost_usd": self.limits.max_cost_usd,
                "cost_per_million_tokens": self.limits.cost_per_million_tokens,
                "reserve_output_tokens": self.limits.reserve_output_tokens,
            },
            "used": {
                "requests": self.requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "cost_usd": round(self.cost_usd, 8),
            },
            "reserved": {
                "requests": len(reserved),
                "input_tokens": sum(item.input_tokens for item in reserved),
                "output_tokens": sum(item.output_tokens for item in reserved),
                "cost_usd": round(sum(item.reserved_cost_usd for item in reserved), 8),
            },
        }

    def _cost(self, tokens: int) -> float:
        return max(0.0, float(tokens)) * max(0.0, self.limits.cost_per_million_tokens) / 1_000_000


# ``SharedBudgetLedger`` is a legacy alias for the same ledger type.
BudgetExceeded = BudgetExceededError
SharedBudgetLedger = BudgetLedger
