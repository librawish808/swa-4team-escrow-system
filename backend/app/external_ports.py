"""Ports for the bank/carrier owner to implement. No real money or delivery calls."""

from enum import Enum
from threading import Lock
from typing import Protocol


class ExternalResult(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ExternalPort(Protocol):
    def execute(self, *, operation: str, transaction_id: int, amount: int,
                waybill_no: str | None, idempotency_key: str) -> ExternalResult: ...


class MockExternalPort:
    """Thread-safe fake. Reusing a key returns its previous outcome."""

    def __init__(self, result: ExternalResult = ExternalResult.SUCCESS):
        self.result = result
        self.calls = []
        self._results = {}
        self._lock = Lock()

    def execute(self, **request) -> ExternalResult:
        with self._lock:
            key = request["idempotency_key"]
            if key not in self._results:
                self.calls.append(dict(request))
                self._results[key] = self.result
            return self._results[key]
