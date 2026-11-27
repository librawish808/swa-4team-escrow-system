"""Controlled-clock QAS reproduction; carrier delays and decision timing are real."""

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from .adapters import MockBankPort, MockDeliveryPort
from .domain import Decision
from .invariants import InvariantChecker
from .repository import SqlTransactionRepository
from .service import TransactionService


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil((p / 100.0) * len(ordered)) - 1))
    return ordered[index]


async def run_qas01(runs=100, carrier_delay_seconds=3.0):
    with TemporaryDirectory(prefix="qas01-") as directory:
        repository = SqlTransactionRepository(str(Path(directory) / "qas01.sqlite3"))
        deadline = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)  # 18:00 KST
        now = deadline - timedelta(milliseconds=50)
        service = TransactionService(
            repository,
            MockDeliveryPort(carrier_delay_seconds),
            MockBankPort(repository),
            clock=lambda: now,
        )
        cases = []
        workers = []
        for _ in range(runs):
            transaction_id = repository.create_approved_return(
                deadline - timedelta(hours=72)
            )
            stale = repository.get(transaction_id)  # expiry read BEFORE receipt
            service.register_return_waybill(
                transaction_id, "buyer-demo", "race", "VALID-001"
            )
            cases.append((transaction_id, stale))
            receipt = repository.claim_receipt(now)
            workers.append(asyncio.create_task(service.verify_receipt(receipt)))
        await asyncio.sleep(0.05)
        now = deadline
        elapsed = []
        per_case_ms = []
        holds = 0
        try:
            deadline_started = perf_counter()
            for transaction_id, stale in cases:
                case_started = perf_counter()
                decision = service.evaluate(transaction_id, snapshot=stale)
                finished = perf_counter()
                # Include the earlier cases' processing time: all share one cutoff.
                elapsed.append((finished - deadline_started) * 1000)
                per_case_ms.append((finished - case_started) * 1000)
                holds += decision == Decision.HOLD
            await asyncio.gather(*workers)
            await service.dispatch_transfers()
            instructions = sum(
                o["kind"] == "PAYOUT"
                for transaction_id, _ in cases
                for o in repository.get(transaction_id)["operations"]
            )
            transfers = repository.transfer_count("PAYOUT")
            invariants = InvariantChecker.verify_database(repository)
            maximum = max(elapsed)
            return {
                "runs": runs,
                "payout_instructions": instructions,
                "payout_transfers": transfers,
                "hold_decisions": holds,
                "max_decision_ms": round(maximum, 3),
                "p50_ms": round(percentile(per_case_ms, 50), 3),
                "p95_ms": round(percentile(per_case_ms, 95), 3),
                "p99_ms": round(percentile(per_case_ms, 99), 3),
                "max_case_ms": round(max(per_case_ms), 3),
                "invariants_passed": invariants.passed,
                "invariant_violations": list(invariants.violations),
                "carrier_delay_seconds": carrier_delay_seconds,
                "clock_mode": "controlled",
                "measurement": "common deadline to each hold decision, wall time",
                "passed": instructions == transfers == 0
                and holds == runs
                and maximum <= 200
                and invariants.passed,
            }
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


async def run_qas01_stress(runs=1000, carrier_delay_seconds=3.0):
    """Hardened 1,000-case QAS-01 stress benchmark with Tail Latency & Invariant verification."""
    with TemporaryDirectory(prefix="qas01-stress-") as directory:
        repository = SqlTransactionRepository(str(Path(directory) / "stress.sqlite3"))
        deadline = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
        now = deadline - timedelta(milliseconds=50)
        service = TransactionService(
            repository,
            MockDeliveryPort(carrier_delay_seconds),
            MockBankPort(repository),
            clock=lambda: now,
        )
        cases = []
        workers = []
        for _ in range(runs):
            transaction_id = repository.create_approved_return(
                deadline - timedelta(hours=72)
            )
            stale = repository.get(transaction_id)
            service.register_return_waybill(
                transaction_id, "buyer-demo", "race", "VALID-001"
            )
            cases.append((transaction_id, stale))
            receipt = repository.claim_receipt(now)
            workers.append(asyncio.create_task(service.verify_receipt(receipt)))
        await asyncio.sleep(0.05)
        now = deadline
        per_case_ms = []
        holds = 0
        try:
            batch_started = perf_counter()
            for transaction_id, stale in cases:
                case_started = perf_counter()
                decision = service.evaluate(transaction_id, snapshot=stale)
                per_case_ms.append((perf_counter() - case_started) * 1000)
                holds += decision == Decision.HOLD
            total_batch_ms = (perf_counter() - batch_started) * 1000
            await asyncio.gather(*workers)
            await service.dispatch_transfers()
            instructions = sum(
                o["kind"] == "PAYOUT"
                for transaction_id, _ in cases
                for o in repository.get(transaction_id)["operations"]
            )
            transfers = repository.transfer_count("PAYOUT")
            invariants = InvariantChecker.verify_database(repository)
            p50 = percentile(per_case_ms, 50)
            p95 = percentile(per_case_ms, 95)
            p99 = percentile(per_case_ms, 99)
            max_case = max(per_case_ms)
            return {
                "runs": runs,
                "payout_instructions": instructions,
                "payout_transfers": transfers,
                "hold_decisions": holds,
                "p50_ms": round(p50, 3),
                "p95_ms": round(p95, 3),
                "p99_ms": round(p99, 3),
                "max_case_ms": round(max_case, 3),
                "total_batch_ms": round(total_batch_ms, 3),
                "invariants_passed": invariants.passed,
                "invariant_violations": list(invariants.violations),
                "carrier_delay_seconds": carrier_delay_seconds,
                "passed": instructions == transfers == 0
                and holds == runs
                and p95 <= 50
                and p99 <= 100
                and max_case <= 200
                and invariants.passed,
            }
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_qas01()), ensure_ascii=False, indent=2))
