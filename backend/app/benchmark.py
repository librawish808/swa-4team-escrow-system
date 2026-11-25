"""Controlled-clock QAS reproduction; carrier delays and decision timing are real."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from .adapters import MockBankPort, MockDeliveryPort
from .domain import Decision
from .repository import SqlTransactionRepository
from .service import TransactionService


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
        holds = 0
        try:
            deadline_started = perf_counter()
            for transaction_id, stale in cases:
                decision = service.evaluate(transaction_id, snapshot=stale)
                # Include the earlier cases' processing time: all share one cutoff.
                elapsed.append((perf_counter() - deadline_started) * 1000)
                holds += decision == Decision.HOLD
            await asyncio.gather(*workers)
            await service.dispatch_transfers()
            instructions = sum(
                o["kind"] == "PAYOUT"
                for transaction_id, _ in cases
                for o in repository.get(transaction_id)["operations"]
            )
            transfers = repository.transfer_count("PAYOUT")
            maximum = max(elapsed)
            return {
                "runs": runs,
                "payout_instructions": instructions,
                "payout_transfers": transfers,
                "hold_decisions": holds,
                "max_decision_ms": round(maximum, 3),
                "carrier_delay_seconds": carrier_delay_seconds,
                "clock_mode": "controlled",
                "measurement": "common deadline to each hold decision, wall time",
                "passed": instructions == transfers == 0
                and holds == runs
                and maximum <= 200,
            }
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_qas01()), ensure_ascii=False, indent=2))
